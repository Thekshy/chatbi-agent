"""T9 评测集校验器：dataset.yaml 结构校验 + 标准 SQL 连库执行 + 期望值写回。

评测纪律（SPEC §8.2 / T9）：期望结论必须由标准 SQL 对真实 mock 库执行得出，
不手拍数值——本脚本是 expected 值的唯一写入口。

用法：
    uv run python -m eval.validate_dataset            # 校验 + 实测值写回 expected
    uv run python -m eval.validate_dataset --check    # 只校验不写回（pytest / CI 用）

standard_sql 结果形态约定（与 dataset.yaml 头注一致）：
- 标量题（single_metric / compare / dirty）：恰好 1 行，第 1 列 = expected.value，
  其余列按列别名写入 expected.extras；
- drilldown：单行 = 因子分解（第 1 列为主变化率，各因子进 extras）；
  多行 = 维度贡献，列约定 (dim, cur, prev, delta, share) 且按 |delta| 降序，
  value 写回逐行 (dim, delta, share)，extras 附 overall_change / top_dim；
- topn：多行按名次排序，列约定 (dim, metric[, share])，value 写回逐行，
  extras 附 top1 摘要。
direction 写回规则：compare / drilldown 按主变化率符号（up / down / flat，阈值 5e-4）；
single_metric / topn / dirty 恒为 none。
"""

from __future__ import annotations

import argparse
import math
import os
import sys
from collections import Counter
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import psycopg
import yaml
from dotenv import load_dotenv

DATASET_PATH = Path(__file__).resolve().parent / "dataset.yaml"

TYPES = ("single_metric", "compare", "drilldown", "topn", "dirty")
LAYER_COUNTS: dict[str, int] = {
    "single_metric": 15, "compare": 10, "drilldown": 12, "topn": 8, "dirty": 5,
}
TYPE_PREFIX = {"single_metric": "sm", "compare": "cmp", "drilldown": "dr",
               "topn": "tn", "dirty": "dt"}
REQUIRED_FIELDS = ("id", "question", "type", "standard_sql", "expected",
                   "expected_path", "notes", "value_desc")
PATH_VOCAB = {"plan", "get_schema", "text2sql", "validate_sql", "run_query",
              "reflect", "replan", "report", "decompose"}
OUTCOMES = {"answered", "unanswerable"}
DIRECTION_EPS = 5e-4
NUM_CMP = {"rel_tol": 1e-9, "abs_tol": 1e-9}

HEADER = """\
# ChatBI v1 离线评测集（SPEC §8.2 · T9）。期望值由 validate_dataset.py 连库实测写回，勿手改。
# 分层：single_metric×15 / compare×10 / drilldown×12 / topn×8 / dirty×5，共 50 题。
# 金额单位=元；比率=小数（如 0.0697 即 +6.97 个百分点）。口径一律 SPEC §5.2：
# GMV/支付订单数/客单价取 status∈{paid,shipped,completed}，时间锚 paid_at，窗口 [起, 止)。
# 相对时间（最近 N 天 / 上个月）以数据末日 2026-09-13 为「今天」；数据仅一年，
# 同比（对上年同月）不可出题，compare 层由月环比 + 周对周（周一起始）构成。
# standard_sql 结果形态：标量题=1 行（第 1 列为主数值）；drilldown=单行因子分解
# 或多行 (dim,cur,prev,delta,share)；topn=多行 (dim,metric[,share])，顺序即名次。
"""


class _Dumper(yaml.SafeDumper):
    """standard_sql 等多行字符串用字面量块风格写回，保持可读。"""


def _repr_str(dumper: yaml.Dumper, data: str) -> yaml.ScalarNode:
    if "\n" in data:
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", data)


_Dumper.add_representer(str, _repr_str)


def load_dataset(path: Path = DATASET_PATH) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def lint_sql(sql: str | None) -> str | None:
    """轻量静态检查：非空、单条语句、SELECT/WITH 开头（不触库）。"""
    s = (sql or "").strip()
    if not s:
        return "SQL 为空"
    body = s.rstrip(";").strip()
    if ";" in body:
        return "含多条语句"
    if body.split(None, 1)[0].upper() not in ("SELECT", "WITH"):
        return "非 SELECT/WITH 开头"
    return None


def validate_schema(data: dict) -> list[str]:
    """字段完整性 + 分层计数 + id 前缀/唯一 + expected_path 词表 + SQL lint。"""
    errors: list[str] = []
    qs = data.get("questions")
    if not isinstance(qs, list) or not qs:
        return ["顶层缺少非空 questions 列表"]
    counts: Counter[str] = Counter()
    seen: set[str] = set()
    for i, q in enumerate(qs):
        tag = f"questions[{i}]"
        if not isinstance(q, dict):
            errors.append(f"{tag}: 不是映射")
            continue
        qid = q.get("id")
        tag = f"{tag}({qid})"
        for field in REQUIRED_FIELDS:
            if q.get(field) in (None, "", [], {}):
                errors.append(f"{tag}: 缺字段/为空 {field}")
        t = q.get("type")
        if t not in TYPES:
            errors.append(f"{tag}: 非法 type={t!r}")
        else:
            counts[t] += 1
            prefix = TYPE_PREFIX[t]
            if not isinstance(qid, str) or not qid.startswith(prefix):
                errors.append(f"{tag}: id 前缀应为 {prefix}")
        if qid in seen:
            errors.append(f"{tag}: id 重复")
        seen.add(qid)
        exp = q.get("expected")
        if isinstance(exp, dict):
            tol = exp.get("tolerance")
            if not isinstance(tol, (int, float)) or isinstance(tol, bool) or not 0 < tol < 1:
                errors.append(f"{tag}: expected.tolerance 应为 (0,1) 内数值")
        else:
            errors.append(f"{tag}: expected 应为映射")
        outcome = q.get("expected_outcome")
        if outcome is not None and outcome not in OUTCOMES:
            errors.append(f"{tag}: 非法 expected_outcome={outcome!r}")
        for step in q.get("expected_path") or []:
            if step not in PATH_VOCAB:
                errors.append(f"{tag}: expected_path 含非法步骤 {step!r}")
        err = lint_sql(q.get("standard_sql"))
        if err:
            errors.append(f"{tag}: {err}")
    for t in TYPES:
        if counts[t] != LAYER_COUNTS[t]:
            errors.append(f"分层计数 {t}: {counts[t]} != {LAYER_COUNTS[t]}")
    if len(qs) != 50:
        errors.append(f"总题数 {len(qs)} != 50")
    return errors


def _norm(v: object) -> object:
    """结果单元格归一：Decimal/float 四舍五入 6 位（整数化），日期转 ISO 字符串。"""
    if isinstance(v, Decimal):
        v = float(v)
    if isinstance(v, float):
        if math.isnan(v) or math.isinf(v):
            return None
        r = round(v, 6)
        return int(r) if r == int(r) and abs(r) < 1e15 else r
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    return v


def execute_all(conn: psycopg.Connection, data: dict) -> dict[str, tuple]:
    """逐题执行 standard_sql；值为 (cols, rows) 或 ("ERROR", 消息)。"""
    results: dict[str, tuple] = {}
    for q in data["questions"]:
        try:
            cur = conn.execute(q["standard_sql"])
            cols = [d.name for d in cur.description]
            rows = [[_norm(v) for v in row] for row in cur.fetchall()]
            results[q["id"]] = (cols, rows)
        except psycopg.Error as e:
            msg = str(e).strip().splitlines()[0] if str(e).strip() else repr(e)
            results[q["id"]] = ("ERROR", msg)
    return results


def _sign(v: object) -> str:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return "none"
    if v > DIRECTION_EPS:
        return "up"
    if v < -DIRECTION_EPS:
        return "down"
    return "flat"


def derive_expected(q: dict, cols: list[str], rows: list[list]) -> dict:
    """从执行结果推导 expected.value / direction / extras（形态约定见模块 docstring）。"""
    t = q["type"]
    if t in ("single_metric", "compare", "dirty") or (t == "drilldown" and len(rows) == 1):
        if len(rows) != 1:
            raise ValueError(f"{t} 应返回 1 行，实得 {len(rows)} 行")
        row = rows[0]
        if not row or row[0] is None:
            raise ValueError("第 1 列（主数值）为空")
        extras = dict(zip(cols[1:], row[1:], strict=True))
        direction = _sign(row[0]) if t in ("compare", "drilldown") else "none"
        return {"value": row[0], "direction": direction, "extras": extras}
    if not rows:
        raise ValueError("结果为空集")
    if t == "drilldown":
        if len(cols) < 5:
            raise ValueError(f"维度贡献形态应为 (dim,cur,prev,delta,share)，实得列 {cols}")
        value = [[r[0], r[3], r[4]] for r in rows]
        cur_sum = sum(r[1] for r in rows if r[1] is not None)
        prev_sum = sum(r[2] for r in rows if r[2] is not None)
        overall = _norm((cur_sum - prev_sum) / prev_sum) if prev_sum else None
        extras = {"overall_change": overall, "top_dim": rows[0][0], "top_delta": rows[0][3]}
        return {"value": value, "direction": _sign(overall), "extras": extras}
    # topn
    if len(cols) < 2:
        raise ValueError(f"topn 形态应为 (dim,metric[,share])，实得列 {cols}")
    value = [list(r[:3]) for r in rows]
    extras: dict = {"top1": rows[0][0], "top1_metric": rows[0][1]}
    if len(cols) >= 3 and rows[0][2] is not None:
        extras["top1_share"] = rows[0][2]
    return {"value": value, "direction": "none", "extras": extras}


def _diff(path: str, stored: object, fresh: object, errors: list[str]) -> None:
    if isinstance(stored, dict) and isinstance(fresh, dict):
        for k in sorted(set(stored) | set(fresh)):
            if k not in stored:
                errors.append(f"{path}.{k}: 库中无此键（实测 {fresh[k]!r}）")
            elif k not in fresh:
                errors.append(f"{path}.{k}: 实测缺失（标注 {stored[k]!r}）")
            else:
                _diff(f"{path}.{k}", stored[k], fresh[k], errors)
    elif isinstance(stored, list) and isinstance(fresh, list):
        if len(stored) != len(fresh):
            errors.append(f"{path}: 行数 {len(stored)} != {len(fresh)}")
        else:
            for i, (a, b) in enumerate(zip(stored, fresh, strict=True)):
                _diff(f"{path}[{i}]", a, b, errors)
    elif isinstance(stored, bool) or isinstance(fresh, bool):
        if stored != fresh:
            errors.append(f"{path}: {stored!r} != {fresh!r}")
    elif isinstance(stored, (int, float)) and isinstance(fresh, (int, float)):
        if not math.isclose(stored, fresh, **NUM_CMP):
            errors.append(f"{path}: {stored!r} != {fresh!r}")
    elif stored != fresh:
        errors.append(f"{path}: {stored!r} != {fresh!r}")


def check_against(data: dict, derived: dict[str, dict]) -> list[str]:
    """--check 模式：实测推导值 vs 已写回标注，逐字段比对。"""
    errors: list[str] = []
    for q in data["questions"]:
        exp = q["expected"]
        for key in ("value", "direction", "extras"):
            if exp.get(key) is None:
                errors.append(f"{q['id']}: expected.{key} 未写回（先跑不带 --check 的写回模式）")
            else:
                _diff(f"{q['id']}.expected.{key}", exp[key], derived[q["id"]][key], errors)
    return errors


def write_back(data: dict, derived: dict[str, dict], db_meta: dict) -> None:
    data.setdefault("meta", {}).update(db_meta)
    for q in data["questions"]:
        q["expected"].update(derived[q["id"]])
    with open(DATASET_PATH, "w", encoding="utf-8") as f:
        f.write(HEADER)
        yaml.dump(data, f, Dumper=_Dumper, allow_unicode=True, sort_keys=False, width=100)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="ChatBI 评测集校验器（T9）")
    ap.add_argument("--check", action="store_true", help="只校验不写回")
    ap.add_argument("--dsn", help="覆盖 .env 的连接串（默认 PG_DSN_RO，回退 PG_DSN）")
    args = ap.parse_args(argv)
    load_dotenv()

    data = load_dataset()
    errors = validate_schema(data)
    if errors:
        print("结构校验失败：")
        for e in errors:
            print(f"  - {e}")
        return 1

    dsn = args.dsn or os.environ.get("PG_DSN_RO") or os.environ.get("PG_DSN")
    if not dsn:
        print("缺少 DSN：请在 .env 配置 PG_DSN_RO，或用 --dsn 指定", file=sys.stderr)
        return 2
    try:
        with psycopg.connect(dsn, connect_timeout=5,
                             options="-c statement_timeout=30s") as conn:
            results = execute_all(conn, data)
            n_orders, pmin, pmax = conn.execute(
                "SELECT count(*), min(paid_at)::date, max(paid_at)::date FROM orders"
            ).fetchone()
    except psycopg.Error as e:
        print(f"数据库不可用：{e}", file=sys.stderr)
        return 2

    counts = Counter(q["type"] for q in data["questions"])
    print("=" * 72)
    print(f"评测集（{'--check 复核' if args.check else '实测写回'}）"
          f" · 库指纹 orders={n_orders}, paid_at {pmin} ~ {pmax}")
    print("分层: " + ", ".join(f"{t}={counts[t]}" for t in TYPES)
          + f" · 共 {len(data['questions'])} 题")

    failed: dict[str, str] = {
        qid: str(res[1]) for qid, res in results.items() if res[0] == "ERROR"}
    derived: dict[str, dict] = {}
    for q in data["questions"]:
        cols, rows = results[q["id"]]
        try:
            derived[q["id"]] = derive_expected(q, cols, rows)
        except ValueError as e:
            failed[q["id"]] = str(e)

    for qid, msg in failed.items():
        print(f"FAIL  {qid}: {msg}")
    if failed:
        print(f"{len(failed)}/{len(data['questions'])} 题失败"
              f"{'（未写回任何内容）' if not args.check else ''}")
        return 1

    for q in data["questions"]:
        d = derived[q["id"]]
        v = d["value"]
        preview = v if not isinstance(v, list) else f"[{v[0][0]!r}, ...] 共 {len(v)} 行"
        print(f"PASS  {q['id']:<7} {q['type']:<13} {str(preview)[:60]}")

    if args.check:
        errors = check_against(data, derived)
        meta = data.get("meta") or {}
        if meta.get("db_orders") is None:
            errors.append("meta.db_orders 未写回（先跑写回模式）")
        elif int(meta["db_orders"]) != int(n_orders):
            errors.append(f"库指纹不符（标注 orders={meta['db_orders']}，当前 {n_orders}），"
                          "库可能已重建，请重跑写回模式")
        if errors:
            print("--check 不一致：")
            for e in errors:
                print(f"  - {e}")
            return 1
        print("50/50 可执行且与写回值一致 ✓")
        return 0

    write_back(data, derived, {
        "checked_at": datetime.now().isoformat(timespec="seconds"),
        "db_orders": int(n_orders),
        "db_paid_range": [str(pmin), str(pmax)],
    })
    print(f"50/50 可执行，期望值已写回 {DATASET_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
