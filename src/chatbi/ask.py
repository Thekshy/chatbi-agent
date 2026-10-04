"""ask：CLI 薄壳——``python -m chatbi.ask "问题"``（T7 / SPEC §6.4；开发调试用，非验收范围）。

rich 呈现 analyze() 结果：状态面板（三态着色）+ 结论 Markdown + 证据表
（SQL / 行数 / 耗时 / 结果预览）+ 步骤时间线（每次生成尝试的结局）+ 用量与 trace 路径。
退出码：failed → 1，answered / partial → 0。
"""

from __future__ import annotations

import argparse
import json
import sys

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table

from chatbi.api import AnalysisResult, RunConfig, analyze

_STATUS_STYLE = {"answered": "green", "partial": "yellow", "failed": "red"}
_OUTCOME_STYLE = {"ok": "green", "error": "red", "invalid": "red",
                  "empty": "yellow", "duplicate": "yellow", "no_sql": "red"}


def _evidence_table(result: AnalysisResult) -> Table:
    table = Table(title="证据", show_lines=True)
    table.add_column("ID", style="cyan", no_wrap=True)
    table.add_column("说明")
    table.add_column("SQL", overflow="fold")
    table.add_column("行数", justify="right")
    table.add_column("耗时ms", justify="right")
    table.add_column("结果预览", overflow="fold")
    for e in result.evidence:
        preview = json.dumps((e.get("rows_preview") or [])[:2], ensure_ascii=False)
        if len(preview) > 120:
            preview = preview[:117] + "..."
        table.add_row(
            str(e.get("id", "")), e.get("description", ""), e.get("sql", ""),
            str(e.get("row_count", "")), str(e.get("ms", "")), preview,
        )
    return table


def _steps_table(result: AnalysisResult) -> Table:
    table = Table(title="步骤时间线（每次 SQL 生成尝试）")
    table.add_column("步骤", justify="right")
    table.add_column("说明")
    table.add_column("尝试", justify="right")
    table.add_column("结局")
    table.add_column("错误", overflow="fold")
    for s in result.steps:
        outcome = s.get("outcome") or ""
        style = _OUTCOME_STYLE.get(outcome, "white")
        err = s.get("error") or ""
        if len(err) > 80:
            err = err[:77] + "..."
        table.add_row(
            str(s.get("step_id", "")), s.get("description", ""), str(s.get("attempt", "")),
            f"[{style}]{outcome}[/{style}]", err,
        )
    return table


def render(console: Console, question: str, result: AnalysisResult) -> None:
    style = _STATUS_STYLE.get(result.status, "white")
    console.print(
        Panel(Markdown(result.conclusion or "（无结论）"),
              title=f"[{style}]{result.status}[/{style}] · {question}")
    )
    if result.evidence:
        console.print(_evidence_table(result))
    if result.steps:
        console.print(_steps_table(result))
    usage = result.usage
    console.print(
        f"用量：LLM {usage.get('llm_calls', 0)} 次 · 查询 {usage.get('query_calls', 0)} 次 · "
        f"tokens {usage.get('tokens_in', 0)}/{usage.get('tokens_out', 0)}"
    )
    if result.errors:
        n = len(result.errors)
        console.print(f"[yellow]非致命错误（{n} 条，末条）：[/yellow]{result.errors[-1]}")
    if result.trace_dir is not None:
        console.print(f"trace：{result.trace_dir / 'trace.jsonl'}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m chatbi.ask", description="ChatBI 一次分析（开发调试薄壳，非验收范围）"
    )
    parser.add_argument("question", help="自然语言问题（问法范围见 SPEC §3）")
    parser.add_argument("--model", default=None, help="覆写 CHATBI_MODEL")
    parser.add_argument("--no-trace", action="store_true", help="关闭 trace 落盘")
    args = parser.parse_args(argv)

    console = Console()
    console.print("[dim]分析中（同步等待，通常 30~60s）…[/dim]")
    result = analyze(
        args.question, config=RunConfig(model=args.model, trace=not args.no_trace)
    )
    render(console, args.question, result)
    return 1 if result.status == "failed" else 0


if __name__ == "__main__":
    sys.exit(main())
