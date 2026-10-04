"""T10 评测 harness：批量调 analyze() + 四指标判定 + 报告数据来源（SPEC §8.2）。

数据流：eval/dataset.yaml（T9，expected 已由 validate_dataset.py 连库实测写回）→
逐题 × N 次独立采样 analyze()（T7 契约入口）→ 逐 run 判定 → 题级聚合 →
eval/report.py 渲染 markdown（T11 全量评测与 README 评测表的数据来源）。

四指标判定口径（判定全部是纯函数，合成数据单测钉死于 tests/test_runner.py）：

1. **执行正确率**（每题）：证据 SQL 逐条用只读连接重放，任一条结果与 expected
   一致即过（相对误差 ≤ expected.tolerance，默认 ±2%）：
   - 标量（single_metric / dirty / compare / drilldown 单行）：首行任一数值格 ≈ value；
   - compare 回退：主变化率未直接出现时，若两格分别 ≈ extras.cur 与 extras.prev，
     由 (cur−prev)/prev 反推再比（列序不敏感）；
   - drilldown 单行（因子分解）：value 命中后 extras 各因子也都须命中；
   - drilldown 多行：按 dim 查行（顺序不敏感），每行 delta 命中，share 有值也须命中；
   - topn：dim 列按名次序列全匹配，每行 metric / share 须命中；
   - 率类（|expected| < 10）兼容百分比尺度（0.087 ↔ 8.7）；大数值不回退，防 53↔5300 误配。
2. **结论正确率**（每题）：结论文本提取数值词元（%/个百分点/pp ÷100、万/亿 放大、
   千分位与 Unicode 负号归一、率类含取反候选——「下滑 8.7%」即 -0.087，符号由方向词
   承载），主数值容差内命中且方向一致（方向词 ∨ 数值符号，与 expected.direction 比对；
   none / flat 免判）；topn / drilldown 多行题要求各 dim 按序出现且 metric 命中
   （share 免判，drilldown 另要求总变化 overall_change 命中）；drilldown 单行题要求
   因子 extras 全命中；compare 的 extras（cur/prev）免判。expected_outcome=unanswerable
   的脏题改判「status ∈ {failed, partial}」——不编造完整回答即正确；可解题
   status=failed 直接判不过。
3. **纠错成功率**（仅 dirty 层，分母 5）：steps（steps_log）首条 outcome ∈
   {error, invalid, duplicate, empty, no_sql} 即「首轮 SQL 失败」——与 trace.jsonl 的
   sql_result 事件同源同判（T6 由 steps_log 差分采集）；且最终 answered ∧ 执行正确。
   dt005（无解）预期 failed/partial，天然不计入分子。
4. **采样一致性**（每题）：每次采样取结论签名（关键数值 = 与 expected 最近的词元、
   换算到同量级；方向 = 方向词/符号，expected.direction 为 none/flat 时免判），
   N ≥ 2 时存在 ≥2 个签名两两一致（数值相对差 ≤5%、方向相同、None==None）即过——
   稳定地错也算一致（一致性 ≠ 正确性）。

题级聚合：exec / conclusion / recovery 取采样的严格多数（2·命中 > 采样数；n=1 即该次，
平票判否）。CLI：

    uv run python -m eval.runner --type single_metric --limit 5 --samples 1
    uv run python -m eval.runner --id dt001,dt002 --report eval/reports/dirty.md

报告默认落 eval/reports/eval_<时间戳>.md（+ 同名 .json 完整结构）。
"""

from __future__ import annotations

import argparse
import math
import os
import re
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg
from dotenv import load_dotenv

from chatbi.api import AnalysisResult, RunConfig, analyze
from eval.report import build_report, save_report
from eval.validate_dataset import TYPES, _norm, load_dataset

DEFAULT_TOLERANCE = 0.02
CONSISTENCY_TOL = 0.05  # 一致性：同题两次结论关键数值的相对差上限（> 判定容差，容忍采样噪声）
RATE_SCALE_GUARD = 10.0  # |expected| < 10 才启用百分比尺度/取反候选（率类），防 53↔5300 误配
FIRST_FAIL_OUTCOMES = ("error", "invalid", "duplicate", "empty", "no_sql")
DOWN_WORDS = ("下滑", "下降", "降", "跌", "减少", "回落")
UP_WORDS = ("增长", "上涨", "上升", "增加", "提高", "提升", "回升")

SqlExecutor = Callable[[str], tuple[list[str], list[list]]]
AnalyzeFn = Callable[[str], AnalysisResult]


# ---------- 数值基础 ----------


def _num(v: Any) -> float | None:
    """单元格/标量 → float；bool 与非数值返回 None（千分位逗号剥离）。"""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float, Decimal)):
        return float(v)
    if isinstance(v, str):
        s = v.replace(",", "").replace("，", "").strip()
        return float(s) if re.fullmatch(r"[+-]?\d+(?:\.\d+)?", s) else None
    return None


def rel_err(value: float, expected: float) -> float:
    """相对误差 |value−expected|/|expected|；expected=0 时仅 0≈0。"""
    if expected == 0:
        return 0.0 if value == 0 else math.inf
    return abs(value - expected) / abs(expected)


def values_match(value: Any, expected: Any, tol: float = DEFAULT_TOLERANCE) -> bool:
    """数值命中：容差内，或（率类）百分比尺度换算后命中。SQL 结果比对不走取反——
    符号错就是结果错；文本结论的取反候选见 _token_candidates。"""
    v, e = _num(value), _num(expected)
    if v is None or e is None:
        return False
    if rel_err(v, e) <= tol:
        return True
    if abs(e) < RATE_SCALE_GUARD:
        return rel_err(v, e * 100) <= tol or rel_err(v, e / 100) <= tol
    return False


# ---------- 结论文本的数值词元 ----------


@dataclass(frozen=True)
class NumToken:
    raw: str
    value: float  # 归一化：%/个百分点/pp ÷100，万/亿 放大
    unit: str | None


_TOKEN_RE = re.compile(r"(-?\d[\d,，]*(?:\.\d+)?)\s*(%|％|个百分点|pp|万|亿)?")


def extract_numbers(text: str) -> list[NumToken]:
    """提取数值词元（Unicode 负号归一；单位只认紧随数字的这几种）。"""
    out: list[NumToken] = []
    for m in _TOKEN_RE.finditer(text.replace("−", "-")):
        v = _num(m.group(1))
        if v is None:
            continue
        unit = m.group(2)
        if unit in ("%", "％", "个百分点", "pp"):
            v /= 100
        elif unit == "万":
            v *= 10_000
        elif unit == "亿":
            v *= 100_000_000
        out.append(NumToken(raw=m.group(1), value=v, unit=unit))
    return out


def classify_direction(text: str, key_value: float | None) -> str:
    """方向判定：恰好一组方向词 → 词判；两组同现（如「UV 降转化升」）或无词 → 数值符号。"""
    down = any(w in text for w in DOWN_WORDS)
    up = any(w in text for w in UP_WORDS)
    if down != up:
        return "down" if down else "up"
    if key_value is not None:
        if key_value < 0:
            return "down"
        if key_value > 0:
            return "up"
    return "none"


def primary_number(expected: dict) -> float | None:
    """expected 的主数值：标量取 value；多行取 overall_change / top1_metric / 首行 metric。"""
    v = _num(expected.get("value"))
    if v is not None:
        return v
    extras = expected.get("extras") or {}
    for key in ("overall_change", "top1_metric"):
        n = _num(extras.get(key))
        if n is not None:
            return n
    value = expected.get("value")
    if isinstance(value, list) and value and isinstance(value[0], list) and len(value[0]) > 1:
        return _num(value[0][1])
    return None


def _token_candidates(token_value: float, expected_value: float) -> list[float]:
    """文本词元换算到与 expected 同量级的候选（率类含取反：「下滑 8.7%」表 -0.087，
    符号由方向词承载，方向正确性由 classify_direction 把关）。"""
    cands = [token_value]
    if abs(expected_value) < RATE_SCALE_GUARD:
        cands += [-token_value, token_value * 100, -token_value * 100,
                  token_value / 100, -token_value / 100]
    return cands


def nearest_token(conclusion: str, expected_value: float) -> tuple[float | None, float]:
    """与 expected 同量级换算后距离最近的词元 →（规范化值， 相对误差）。"""
    best_val: float | None = None
    best_err = math.inf
    for t in extract_numbers(conclusion):
        for cand in _token_candidates(t.value, expected_value):
            err = rel_err(cand, expected_value)
            if err < best_err:
                best_val, best_err = cand, err
    return best_val, best_err


# ---------- 指标 2：结论正确率 ----------


def conclusion_signature(conclusion: str, expected: dict) -> tuple[float | None, str]:
    """一致性签名（关键数值， 方向）；expected.direction 为 none/flat 时方向免判。"""
    ev = primary_number(expected)
    val: float | None = None
    if ev is not None:
        val, _err = nearest_token(conclusion, ev)
    exp_dir = expected.get("direction") or "none"
    direction = classify_direction(conclusion, val) if exp_dir not in ("none", "flat") else "none"
    return val, direction


def _judge_conclusion_rows(conclusion: str, rows: list, tol: float) -> tuple[bool, str]:
    """多行形态（topn / drilldown 维度分解）：每个 dim 按序出现且 metric 命中；share 免判。"""
    pos = -1
    for row in rows:
        dim = str(row[0])
        at = conclusion.find(dim, pos + 1)
        if at < 0:
            return False, f"结论缺少维度 {dim}（或顺序不符）"
        pos = at
        metric = _num(row[1]) if len(row) > 1 else None
        if metric is None:
            continue
        val, err = nearest_token(conclusion, metric)
        if val is None or err > tol:
            return False, f"{dim} 的数值未命中（偏差 {err:.1%}）"
    return True, "各维度按序出现且数值命中"


def judge_conclusion(
    conclusion: str,
    status: str,
    expected: dict,
    expected_outcome: str | None = None,
    qtype: str | None = None,
) -> tuple[bool, str]:
    """结论正确率判定（口径见模块 docstring 第 2 条）。"""
    tol = float(expected.get("tolerance") or DEFAULT_TOLERANCE)
    if expected_outcome == "unanswerable":
        if status in ("failed", "partial"):
            return True, f"无解题未编造完整回答（status={status}）"
        return False, "无解题给出 answered（应说明数据边界）"
    if status == "failed":
        return False, "分析 failed，结论不可用"
    value = expected.get("value")
    if isinstance(value, list):
        ok, why = _judge_conclusion_rows(conclusion, value, tol)
        if not ok:
            return ok, why
        overall = _num((expected.get("extras") or {}).get("overall_change"))
        if overall is not None:  # drilldown 多行：总变化是结论的第一主数
            val, err = nearest_token(conclusion, overall)
            if val is None or err > tol:
                return False, f"总变化（overall_change）未命中（偏差 {err:.1%}）"
        return True, why
    ev = _num(value)
    if ev is None:
        return False, "expected.value 非数值"
    val, err = nearest_token(conclusion, ev)
    if val is None:
        return False, "结论未提取到数值词元"
    if err > tol:
        return False, f"关键数值偏差 {err:.1%} > {tol:.0%}"
    if qtype == "drilldown":  # 因子分解是答案本体（「各贡献多少」），结论须逐因子命中
        for key, v in (expected.get("extras") or {}).items():
            n = _num(v)
            if n is None:
                continue
            _val, factor_err = nearest_token(conclusion, n)
            if _val is None or factor_err > tol:
                return False, f"结论缺少因子 {key}（偏差 {factor_err:.1%}）"
    exp_dir = expected.get("direction") or "none"
    judged = classify_direction(conclusion, val)
    if exp_dir not in ("none", "flat") and judged != exp_dir:
        return False, f"数值命中（{err:.1%}）但方向 {judged} ≠ {exp_dir}"
    dir_note = judged if exp_dir not in ("none", "flat") else "方向免判"
    return True, f"数值命中（{err:.1%}），{dir_note}"


# ---------- 指标 1：执行正确率 ----------


def _cells(row: list) -> list[float]:
    return [x for x in (_num(v) for v in row) if x is not None]


def _row_matches(row: list, target: Any, tol: float) -> bool:
    return any(values_match(c, target, tol) for c in _cells(row))


def _dim_column_index(rows: list[list], dims: list[str]) -> int | None:
    if not rows or len(rows) < len(dims):
        return None
    for c in range(len(rows[0])):
        if [str(r[c]) for r in rows[: len(dims)]] == dims:
            return c
    return None


def sql_result_matches(
    qtype: str, expected: dict, rows: list[list], tol: float
) -> tuple[bool, str]:
    """一条证据 SQL 的重放结果 vs expected（形态比对口径见模块 docstring 第 1 条）。"""
    value = expected.get("value")
    extras = expected.get("extras") or {}
    if isinstance(value, list):
        dims = [str(r[0]) for r in value]
        if qtype == "topn":
            col = _dim_column_index(rows, dims)
            if col is None:
                return False, "dim 名次序列不匹配"
            for i, exp_row in enumerate(value):
                metric = _num(exp_row[1]) if len(exp_row) > 1 else None
                if metric is not None and not _row_matches(rows[i], metric, tol):
                    return False, f"第 {i + 1} 名 metric 不匹配"
                share = _num(exp_row[2]) if len(exp_row) > 2 else None
                if share is not None and not _row_matches(rows[i], share, tol):
                    return False, f"第 {i + 1} 名 share 不匹配"
            return True, "名次序列与数值匹配"
        by_dim = {str(r[0]): r for r in rows if r}  # drilldown 多行：dim 查行，顺序不敏感
        for exp_row in value:
            dim = str(exp_row[0])
            row = by_dim.get(dim)
            if row is None:
                return False, f"缺少维度行 {dim}"
            delta = _num(exp_row[1]) if len(exp_row) > 1 else None
            if delta is not None and not _row_matches(row, delta, tol):
                return False, f"{dim} 的 delta 不匹配"
            share = _num(exp_row[2]) if len(exp_row) > 2 else None
            if share is not None and not _row_matches(row, share, tol):
                return False, f"{dim} 的 share 不匹配"
        return True, "维度分解匹配"
    if not rows:
        return False, "结果 0 行"
    ev = _num(value)
    if ev is None:
        return False, "expected.value 非数值"
    row0 = rows[0]
    if not _row_matches(row0, ev, tol):
        cur, prev = _num(extras.get("cur")), _num(extras.get("prev"))
        if qtype == "compare" and cur is not None and prev is not None:
            cells = _cells(row0)
            for i, ci in enumerate(cells):
                for j, cj in enumerate(cells):
                    derived = (ci - cj) / cj if cj != 0 else None
                    if (
                        i != j
                        and values_match(ci, cur, tol)
                        and values_match(cj, prev, tol)
                        and derived is not None
                        and values_match(derived, ev, tol)
                    ):
                        return True, "由 cur/prev 反推变化率命中"
        return False, "主数值未命中"
    if qtype == "drilldown":  # 因子分解：extras 是答案本体，全须命中
        for key, v in extras.items():
            if v is None or not _row_matches(row0, v, tol):
                return False, f"因子 {key} 未命中"
    return True, "主数值命中"


def judge_execution(
    evidence_sqls: list[str], qtype: str, expected: dict, executor: SqlExecutor
) -> tuple[bool, str]:
    """执行正确率判定：证据 SQL 逐条重放，任一条与 expected 一致即过。"""
    if not evidence_sqls:
        return False, "无成功执行的证据 SQL"
    tol = float(expected.get("tolerance") or DEFAULT_TOLERANCE)
    detail = "全部证据 SQL 均不匹配"
    for sql in evidence_sqls:
        try:
            _cols, rows = executor(sql)
        except Exception as err:  # noqa: BLE001 —— 单条重放失败换下一条证据
            detail = f"重放执行失败：{err}"
            continue
        ok, why = sql_result_matches(qtype, expected, rows, tol)
        if ok:
            return True, why
        detail = why
    return False, detail


# ---------- 指标 3：纠错成功率 ----------


def first_round_failed(steps: list) -> bool:
    """首轮 SQL 是否失败：steps（steps_log）首条 outcome ∈ 失败集——与 trace.jsonl
    的 sql_result 事件同源同判（T6 由 steps_log 差分采集）。无任何尝试不算失败。"""
    if not steps:
        return False
    return (steps[0].get("outcome") or "") in FIRST_FAIL_OUTCOMES


def is_recovered_run(result: AnalysisResult, exec_ok: bool) -> bool:
    """一次采样的纠错成功：首轮失败 ∧ 最终 answered ∧ 执行正确。"""
    return result.status == "answered" and exec_ok and first_round_failed(result.steps)


# ---------- 聚合与批量执行 ----------


def majority(verdicts: list) -> bool:
    """严格多数：2·命中 > 采样数（n=1 即该次；平票判否）。"""
    return 2 * sum(1 for v in verdicts if v) > len(verdicts)


def consistent(
    signatures: list[tuple[float | None, str]], tol: float = CONSISTENCY_TOL
) -> bool | None:
    """采样一致性：存在 ≥2 个签名两两一致（方向相同；数值同 None 或相对差 ≤ tol）。
    采样 <2 → None（不判，报告显示 —）。"""
    n = len(signatures)
    if n < 2:
        return None
    best = 0
    for a_val, a_dir in signatures:
        size = 0
        for b_val, b_dir in signatures:
            if a_dir != b_dir:
                continue
            if a_val is None and b_val is None:
                size += 1
            elif a_val is None or b_val is None:
                continue
            elif abs(a_val - b_val) / max(abs(a_val), abs(b_val), 1e-12) <= tol:
                size += 1
        best = max(best, size)
    return best >= 2


def select_questions(
    questions: list[dict],
    *,
    types: list[str] | None = None,
    ids: list[str] | None = None,
    limit: int | None = None,
) -> list[dict]:
    """子集选择：type / id（可逗号分隔）/ limit 依次过滤；空集或未知 id 报错。"""
    qs = list(questions)
    if types:
        qs = [q for q in qs if q["type"] in set(types)]
    if ids:
        wanted = {x.strip() for part in ids for x in part.split(",") if x.strip()}
        unknown = sorted(wanted - {q["id"] for q in qs})
        if unknown:
            raise ValueError(f"未知题目 id：{', '.join(unknown)}")
        qs = [q for q in qs if q["id"] in wanted]
    if limit is not None and limit > 0:
        qs = qs[:limit]
    if not qs:
        raise ValueError("选题子集为空（检查 --type/--id/--limit）")
    return qs


def run_eval(
    questions: list[dict],
    *,
    analyze_fn: AnalyzeFn,
    executor: SqlExecutor,
    samples: int = 3,
) -> dict:
    """批量执行 + 逐 run 判定 + 题级聚合。返回结构见 report.py docstring。"""
    if samples < 1:
        raise ValueError(f"samples 应 ≥1，实得 {samples}")
    started = datetime.now().isoformat(timespec="seconds")
    t0 = time.perf_counter()
    out_questions: list[dict] = []
    for q in questions:
        expected = q["expected"]
        outcome = q.get("expected_outcome")
        runs: list[dict] = []
        for sample in range(1, samples + 1):
            res = analyze_fn(q["question"])
            ev_sqls = [e.get("sql") for e in (res.evidence or []) if e.get("sql")]
            exec_ok, exec_detail = judge_execution(ev_sqls, q["type"], expected, executor)
            conc_ok, conc_detail = judge_conclusion(
                res.conclusion, res.status, expected, outcome, qtype=q["type"]
            )
            fr = first_round_failed(res.steps)
            key_value, direction = conclusion_signature(res.conclusion, expected)
            runs.append(
                {
                    "sample": sample,
                    "status": res.status,
                    "conclusion": res.conclusion,
                    "evidence_sqls": ev_sqls,
                    "exec_ok": exec_ok,
                    "exec_detail": exec_detail,
                    "conclusion_ok": conc_ok,
                    "conc_detail": conc_detail,
                    "first_round_failed": fr,
                    "recovery": fr and exec_ok and res.status == "answered",
                    "key_value": key_value,
                    "direction": direction,
                    "usage": dict(res.usage),
                    "trace_dir": str(res.trace_dir) if res.trace_dir else None,
                }
            )
        sigs = [(r["key_value"], r["direction"]) for r in runs]
        out_questions.append(
            {
                "id": q["id"],
                "question": q["question"],
                "type": q["type"],
                "expected_outcome": outcome,
                "samples": samples,
                "exec_ok": majority([r["exec_ok"] for r in runs]),
                "conclusion_ok": majority([r["conclusion_ok"] for r in runs]),
                "recovery": majority([r["recovery"] for r in runs]),
                "consistent": consistent(sigs),
                "runs": runs,
            }
        )
    usage_tot = {"llm_calls": 0, "query_calls": 0, "tokens_in": 0, "tokens_out": 0}
    by_type: dict[str, dict] = {}
    for q in out_questions:
        t = by_type.setdefault(
            q["type"],
            {"n": 0, "exec_ok": 0, "conclusion_ok": 0, "dirty_n": 0, "recovery": 0,
             "consistent_judged": 0, "consistent": 0, "runs": 0,
             "usage": {"llm_calls": 0, "query_calls": 0, "tokens_in": 0, "tokens_out": 0}},
        )
        t["n"] += 1
        t["exec_ok"] += q["exec_ok"]
        t["conclusion_ok"] += q["conclusion_ok"]
        if q["type"] == "dirty":
            t["dirty_n"] += 1
            t["recovery"] += q["recovery"]
        if q["consistent"] is not None:
            t["consistent_judged"] += 1
            t["consistent"] += bool(q["consistent"])
        for r in q["runs"]:
            t["runs"] += 1
            for k in usage_tot:
                t["usage"][k] += r["usage"].get(k, 0)
                usage_tot[k] += r["usage"].get(k, 0)
    totals = {
        "n": len(out_questions),
        "exec_ok": sum(q["exec_ok"] for q in out_questions),
        "conclusion_ok": sum(q["conclusion_ok"] for q in out_questions),
        "dirty_n": sum(1 for q in out_questions if q["type"] == "dirty"),
        "recovery": sum(q["recovery"] for q in out_questions if q["type"] == "dirty"),
        "consistent_judged": sum(1 for q in out_questions if q["consistent"] is not None),
        "consistent": sum(1 for q in out_questions if q["consistent"]),
        "runs": sum(len(q["runs"]) for q in out_questions),
        "usage": usage_tot,
        "wall_s": round(time.perf_counter() - t0, 1),
        "by_type": by_type,
    }
    return {"meta": {"started_at": started, "samples": samples},
            "questions": out_questions, "totals": totals}


# ---------- CLI ----------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="ChatBI 离线评测 harness（T10；SPEC §8.2）")
    ap.add_argument("--type", dest="types", action="append", choices=TYPES,
                    help="按题型取子集（可多次）")
    ap.add_argument("--id", dest="ids", action="append",
                    help="按题目 id 取子集（可多次，逗号分隔）")
    ap.add_argument("--limit", type=int, help="子集后再截断的前 N 题")
    ap.add_argument("--samples", type=int, default=3, help="每题独立采样次数（默认 3）")
    ap.add_argument("--model", help="覆写 .env 的 CHATBI_MODEL")
    ap.add_argument("--runs-root", default="runs", help="trace 与 checkpoint 根目录")
    ap.add_argument("--report", help="报告路径（默认 eval/reports/eval_<时间戳>.md）")
    args = ap.parse_args(argv)
    load_dotenv()

    data = load_dataset()
    try:
        qs = select_questions(
            data["questions"], types=args.types, ids=args.ids, limit=args.limit
        )
    except ValueError as err:
        print(f"选题失败：{err}", file=sys.stderr)
        return 2

    dsn = os.environ.get("PG_DSN_RO") or os.environ.get("PG_DSN")
    if not dsn:
        print("缺少 DSN：请在 .env 配置 PG_DSN_RO（或 PG_DSN）", file=sys.stderr)
        return 2
    try:
        conn = psycopg.connect(dsn, connect_timeout=5,
                               options="-c statement_timeout=30s")
    except psycopg.Error as err:
        print(f"数据库不可用：{err}", file=sys.stderr)
        return 2

    root = Path(args.runs_root)
    cfg = RunConfig(trace_root=root / "eval",
                    checkpoint_path=str(root / "eval-checkpoints.db"),
                    model=args.model)

    def executor(sql: str) -> tuple[list[str], list[list]]:
        cur = conn.execute(sql)
        cols = [d.name for d in cur.description]
        rows = [[_norm(v) for v in row] for row in cur.fetchall()]
        return cols, rows

    def analyze_fn(question: str) -> AnalysisResult:
        return analyze(question, config=cfg)

    print(f"评测开始：{len(qs)} 题 × {args.samples} 采样"
          f"（共 {len(qs) * args.samples} 次分析，逐题顺序执行）")
    try:
        with conn:
            results = run_eval(qs, analyze_fn=analyze_fn, executor=executor,
                               samples=args.samples)
    finally:
        conn.close()

    meta = data.get("meta") or {}
    results["meta"].update({
        "dataset_checked_at": meta.get("checked_at"),
        "db_orders": meta.get("db_orders"),
        "model": args.model or os.environ.get("CHATBI_MODEL"),
        "selected": f"{len(qs)}/{len(data['questions'])} 题"
                    f"{'（' + '/'.join(args.types) + '）' if args.types else ''}",
    })
    out = (Path(args.report) if args.report
           else Path("eval/reports") / f"eval_{datetime.now():%Y%m%dT%H%M%S}.md")
    save_report(build_report(results), out, payload=results)

    t = results["totals"]

    def _r(num: int, den: int) -> str:
        return f"{num}/{den}（{num / den:.0%}）" if den else "—"

    print("=" * 72)
    print(f"评测完成：{t['n']} 题 × {args.samples} 采样 · 耗时 {t['wall_s']}s")
    print(f"执行正确率 {_r(t['exec_ok'], t['n'])} · 结论正确率 {_r(t['conclusion_ok'], t['n'])}"
          f" · 纠错（脏题）{_r(t['recovery'], t['dirty_n'])}"
          f" · 一致性 {_r(t['consistent'], t['consistent_judged'])}")
    print(f"报告已写入：{out}（同名 .json 附完整结构）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
