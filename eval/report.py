"""T10 报告生成：分层 + 总体 markdown 表、失败题明细、token 消耗汇总（喂 README 评测表）。

输入是 eval.runner.run_eval 的结果结构：``{meta, questions[{id, question, type,
expected_outcome, samples, exec_ok, conclusion_ok, recovery, consistent, runs[…]}],
totals{n, exec_ok, conclusion_ok, dirty_n, recovery, consistent_judged, consistent,
runs, usage, wall_s, by_type}}``。目标值来自 SPEC §8.2（执行 ≥80% / 结论 ≥70% /
纠错 ≥50% / 一致性 ≥80%）——实测如实呈现，不凑数；达标列只是对照，不回改数据。

save_report 落盘 markdown，并可选同步写同名 .json（run_eval 结构原样，T11 复查用）。
"""

from __future__ import annotations

import json
from pathlib import Path

TARGETS = {"exec": 0.80, "conclusion": 0.70, "recovery": 0.50, "consistency": 0.80}
TYPE_LABELS = {"single_metric": "单指标", "compare": "同环比", "drilldown": "下钻归因",
               "topn": "TopN", "dirty": "纠错脏题"}
_ORDER = ("single_metric", "compare", "drilldown", "topn", "dirty")


def _ratio(num: int, den: int) -> str:
    return f"{num}/{den}（{num / den:.1%}）" if den else "—"


def _mark(num: int, den: int, target: float) -> str:
    if not den:
        return "—"
    return "✅" if num / den >= target else "❌"


def _flag(v: bool | None) -> str:
    return "✓" if v else ("—" if v is None else "✗")


def build_report(results: dict) -> str:
    t = results["totals"]
    meta = results["meta"]
    lines: list[str] = []
    lines.append("# ChatBI v1 离线评测报告（T10 harness）")
    lines.append("")
    lines.append(f"- 运行：{meta.get('started_at')} · 耗时 {t['wall_s']}s"
                 f" · 每题采样 {meta.get('samples')} 次 · 共 {t['runs']} 次分析")
    if meta.get("model"):
        lines.append(f"- 模型：{meta['model']}")
    if meta.get("db_orders"):
        lines.append(f"- 数据集校验：{meta.get('dataset_checked_at')}"
                     f"（orders={meta['db_orders']}）")
    if meta.get("selected"):
        lines.append(f"- 题目范围：{meta['selected']}")

    lines += ["", "## 总体（SPEC §8.2 目标）", "",
              "| 指标 | 实测 | 目标 | 达标 |", "|---|---|---|---|",
              f"| 执行正确率 | {_ratio(t['exec_ok'], t['n'])} | ≥80% |"
              f" {_mark(t['exec_ok'], t['n'], TARGETS['exec'])} |",
              f"| 结论正确率 | {_ratio(t['conclusion_ok'], t['n'])} | ≥70% |"
              f" {_mark(t['conclusion_ok'], t['n'], TARGETS['conclusion'])} |",
              f"| 纠错成功率（脏题） | {_ratio(t['recovery'], t['dirty_n'])} | ≥50% |"
              f" {_mark(t['recovery'], t['dirty_n'], TARGETS['recovery'])} |",
              f"| 采样一致性 | {_ratio(t['consistent'], t['consistent_judged'])} | ≥80% |"
              f" {_mark(t['consistent'], t['consistent_judged'], TARGETS['consistency'])} |"]

    by_type = t["by_type"]
    lines += ["", "## 分层明细", "",
              "| 类型 | 题数 | 执行 | 结论 | 纠错（脏题） | 一致 |",
              "|---|---|---|---|---|---|"]
    for ty in _ORDER:
        if ty not in by_type:
            continue
        b = by_type[ty]
        lines.append(
            f"| {TYPE_LABELS.get(ty, ty)} | {b['n']} | {_ratio(b['exec_ok'], b['n'])} |"
            f" {_ratio(b['conclusion_ok'], b['n'])} | {_ratio(b['recovery'], b['dirty_n'])} |"
            f" {_ratio(b['consistent'], b['consistent_judged'])} |")
    judged = sum(b["consistent_judged"] for b in by_type.values())
    lines.append(f"| 合计 | {t['n']} | {_ratio(t['exec_ok'], t['n'])} |"
                 f" {_ratio(t['conclusion_ok'], t['n'])} | {_ratio(t['recovery'], t['dirty_n'])} |"
                 f" {_ratio(t['consistent'], judged)} |")

    failed = [q for q in results["questions"]
              if not q["exec_ok"] or not q["conclusion_ok"] or q["consistent"] is False
              or (q["type"] == "dirty" and not q["recovery"])]
    lines += ["", "## 失败题明细", ""]
    if not failed:
        lines.append("全部通过（无执行 / 结论 / 一致性 / 纠错失败项）。")
    for q in failed:
        label = TYPE_LABELS.get(q["type"], q["type"])
        lines.append(f"### {q['id']}（{label}）：{q['question']}")
        flags = [f"执行 {_flag(q['exec_ok'])}", f"结论 {_flag(q['conclusion_ok'])}",
                 f"一致 {_flag(q['consistent'])}"]
        if q["type"] == "dirty":
            flags.append(f"纠错 {_flag(q['recovery'])}")
        lines.append("- 题级：" + " · ".join(flags))
        for r in q["runs"]:
            conc = (r["conclusion"] or "").replace("\n", " ")
            if len(conc) > 100:
                conc = conc[:100] + "…"
            direction = r["direction"] or "none"
            lines.append(
                f"- run{r['sample']}：{r['status']}"
                f" · 执行{_flag(r['exec_ok'])}（{r['exec_detail']}）"
                f" · 结论{_flag(r['conclusion_ok'])}（{r['conc_detail']}）"
                f" · 首轮失败{_flag(r['first_round_failed'])}"
                f" · 关键值 {r['key_value']}（{direction}）"
                f" · tokens {r['usage'].get('tokens_in', 0)}+{r['usage'].get('tokens_out', 0)}")
            if r.get("trace_dir"):
                lines.append(f"  - trace：{r['trace_dir']} · 结论：「{conc}」")
        lines.append("")

    u = t["usage"]
    n = max(t["n"], 1)
    lines += ["", "## Token 与调用消耗", "",
              "| 项 | 合计 | 每题平均 |", "|---|---|---|",
              f"| LLM 调用 | {u['llm_calls']} | {u['llm_calls'] / n:.1f} |",
              f"| run_query | {u['query_calls']} | {u['query_calls'] / n:.1f} |",
              f"| tokens_in | {u['tokens_in']} | {u['tokens_in'] / n:.0f} |",
              f"| tokens_out | {u['tokens_out']} | {u['tokens_out'] / n:.0f} |",
              "", "分层消耗：", "",
              "| 类型 | 采样数 | LLM 调用 | tokens_in | tokens_out |",
              "|---|---|---|---|---|"]
    for ty in _ORDER:
        if ty not in by_type:
            continue
        b = by_type[ty]
        bu = b["usage"]
        lines.append(f"| {TYPE_LABELS.get(ty, ty)} | {b['runs']} | {bu['llm_calls']}"
                     f" | {bu['tokens_in']} | {bu['tokens_out']} |")
    lines.append("")
    return "\n".join(lines)


def save_report(markdown: str, path: str | Path, *, payload: dict | None = None) -> Path:
    """落盘 markdown；给 payload 时同步写同名 .json（run_eval 结构原样）。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(markdown, encoding="utf-8")
    if payload is not None:
        p.with_suffix(".json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    return p
