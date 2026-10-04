"""graph：StateGraph 装配——Plan-Execute 编排核心（T5 / SPEC §6.2、Q5、Q9）。

五节点：``planner``（问题 → 步骤计划）→ ``executor``（逐步：schema 检索 + 模板注入 →
LLM 生成 SQL → validate → 指纹去重 → run_query，记录证据）→ ``reflect``（确定性回灌：
报错附错误原文；空集附 get_data_profile 数据分布提示）→ ``replan``（换路径，至多一次）→
``reporter``（汇总证据生成结构化结论；预算耗尽走规则模板降级）。

条件边分流（路由函数纯函数化，单测直接钉死）：
- 任何节点后：致命错误 / 预算耗尽（LLM ≥8 或 query ≥5）→ reporter；
- executor 后：步骤成功且有余步 → executor；全部完成 → reporter；
  失败且修正机会未用完（<2）→ reflect；用完且未 Replan → replan；否则 → reporter；
- replan 后：新计划为空 → reporter。

防失控三件套见 state.Budgets；SQL 指纹 = validate_sql 产出的 canonical SHA256，
重复即拦截（不触库、不耗 query 预算），由 feedback 引导换路径。

T6：``build_graph(trace=TraceRecorder)`` 装配期挂 trace 回调（LLM / 工具代理 +
节点差分采集，节点函数体零改动）；``run_analysis`` 默认每次分析落
``<trace_root>/<run_id>/trace.jsonl``（SPEC §Q10），三态与异常中断均完整落盘。
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
import uuid
from collections.abc import Callable
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import sqlglot
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from sqlglot import exp

from chatbi.llm import LLMClient, get_client
from chatbi.metrics import catalog_text, get_template
from chatbi.prompts import (
    EXECUTOR_SYSTEM,
    EXECUTOR_USER,
    PLANNER_SYSTEM,
    PLANNER_USER,
    REPLAN_SYSTEM,
    REPLAN_USER,
    REPORTER_SYSTEM,
    REPORTER_USER,
    render_feedback,
    template_section,
)
from chatbi.state import (
    DEFAULT_BUDGETS,
    STATUS_ANSWERED,
    STATUS_FAILED,
    STATUS_PARTIAL,
    STEP_FAILED,
    STEP_OK,
    Budgets,
    GraphState,
    init_state,
    make_step,
    mark_remaining_skipped,
)
from chatbi.tools import (
    TABLE_WHITELIST,
    QueryResult,
    get_data_profile,
    get_schema,
    run_query,
    validate_sql,
)
from chatbi.tools.validate_sql import ValidationResult
from chatbi.tracing import TraceRecorder

# SPEC §5.1：mock 数据覆盖窗口（orders.paid_at 锚点）
DATA_WINDOW = "2025-09-15 ~ 2026-09-13"

# 空集纠错时 get_data_profile 的取样优先列（每表最多取 2 列、全分析最多 3 次画像）
_PROFILE_COLUMNS: dict[str, tuple[str, ...]] = {
    "products": ("category_l2", "brand"),
    "orders": ("status", "paid_at"),
    "traffic_daily": ("category_l1", "channel"),
    "promotions": ("promo_type",),
    "order_items": ("product_id",),
}
_MAX_PROFILES = 3

QueryFn = Callable[[str], QueryResult]
SchemaFn = Callable[[str], dict]
ProfileFn = Callable[[str, str], dict]


def _today() -> str:
    return date.today().isoformat()


# ---------- LLM 输出适配（JSON 计划 / SQL 提取） ----------

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)
_SQL_FENCE_RE = re.compile(r"```(?:sql)?\s*\n?(.*?)```", re.DOTALL | re.IGNORECASE)
_SQL_START_RE = re.compile(r"(?is)\b(select|with)\b")


def parse_plan(content: str, max_steps: int) -> tuple[str, list[dict]]:
    """解析 planner/replan 输出为 (question_type, steps)；任何异常收敛为空计划。"""
    text = (content or "").strip()
    m = _JSON_FENCE_RE.search(text)
    if m:
        text = m.group(1).strip()
    else:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            text = text[start : end + 1]
    try:
        data = json.loads(text)
    except ValueError:
        return "unknown", []
    if not isinstance(data, dict):
        return "unknown", []
    qtype = str(data.get("question_type") or "unknown").strip() or "unknown"
    steps: list[dict] = []
    raw = data.get("steps")
    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            desc = str(item.get("description") or item.get("desc") or "").strip()
            if not desc:
                continue
            template = str(item.get("template") or "").strip().lower() or None
            steps.append(make_step(len(steps) + 1, desc, template))
    return qtype, steps[:max_steps]


def extract_sql(content: str) -> tuple[str, ValidationResult | None]:
    """从 LLM 回复提取 SQL：```sql 块 > 整段 > SELECT/WITH 起始片段，首个过校验者胜。

    返回 (sql_text, validation)：text 非空但 validation 为 None 表示回复里没有
    SQL 样式的文本（no_sql 回灌）；validation.ok 为 False 表示像 SQL 但不合法。
    """
    text = (content or "").strip()
    if not text:
        return "", None
    candidates: list[str] = []
    m = _SQL_FENCE_RE.search(text)
    if m:
        candidates.append(m.group(1).strip().rstrip(";").strip())
    candidates.append(text.rstrip(";").strip())
    m2 = _SQL_START_RE.search(text)
    if m2:
        candidates.append(text[m2.start() :].split(";")[0].strip())
    first_sqlish, invalid = "", None
    for cand in candidates:
        if not cand or not _SQL_START_RE.search(cand):
            continue
        v = validate_sql(cand)
        if v.ok:
            return cand, v
        if not first_sqlish:
            first_sqlish, invalid = cand, v
    if first_sqlish:
        return first_sqlish, invalid
    return (candidates[0] if candidates else ""), None


# ---------- 条件边路由（纯函数，单测钉死） ----------


def route_after_planner(state: GraphState, budgets: Budgets = DEFAULT_BUDGETS) -> str:
    if state.get("fatal_error"):
        return "reporter"
    return "executor"


def route_after_executor(state: GraphState, budgets: Budgets = DEFAULT_BUDGETS) -> str:
    if state.get("fatal_error"):
        return "reporter"
    if (
        state["llm_calls"] >= budgets.max_llm_calls
        or state["query_calls"] >= budgets.max_query_calls
    ):
        return "reporter"
    if state.get("feedback") is None:  # 上一步成功（或尚未开始）
        return "executor" if state["step_cursor"] < len(state["plan"]) else "reporter"
    if state["attempt"] - 1 < budgets.max_corrections:  # 修正机会未用完 → Reflexion
        return "reflect"
    return "reporter" if state["replanned"] else "replan"


def route_after_reflect(state: GraphState, budgets: Budgets = DEFAULT_BUDGETS) -> str:
    if state.get("fatal_error"):
        return "reporter"
    if (
        state["llm_calls"] >= budgets.max_llm_calls
        or state["query_calls"] >= budgets.max_query_calls
    ):
        return "reporter"
    return "executor"


def route_after_replan(state: GraphState, budgets: Budgets = DEFAULT_BUDGETS) -> str:
    if state.get("fatal_error"):
        return "reporter"
    if (
        state["llm_calls"] >= budgets.max_llm_calls
        or state["query_calls"] >= budgets.max_query_calls
    ):
        return "reporter"
    return "executor" if state["plan"] else "reporter"


# ---------- 上下文渲染 ----------


def _jsonable_row(row: dict) -> dict:
    """证据行 JSON 化：Decimal → 字符串（保精度），datetime/date → isoformat。"""
    out = {}
    for k, v in row.items():
        if isinstance(v, Decimal):
            out[k] = str(v)
        elif isinstance(v, (datetime, date)):
            out[k] = v.isoformat()
        else:
            out[k] = v
    return out


def _render_schema(s: dict) -> str:
    cols = ", ".join(f"{c['name']} {c['type']}" for c in s.get("columns", []))
    lines = [f"### {s['table']}（约 {s.get('row_count', '?')} 行）", f"字段：{cols}"]
    for col, values in (s.get("enum_values") or {}).items():
        lines.append(f"枚举 {col}：{' / '.join(str(v) for v in values[:30])}")
    samples = s.get("sample_rows") or []
    if samples:
        lines.append("示例行：" + json.dumps(_jsonable_row(samples[0]), ensure_ascii=False))
    return "\n".join(lines)


def _schema_context(
    cache: dict[str, dict], tables: tuple[str, ...] | None, schema_fn: SchemaFn
) -> tuple[str, dict[str, dict], list[str]]:
    """取模板涉及表的 schema（缓存进 state）；失败降级为提示文字，不致命。"""
    if tables is None:
        tables = tuple(sorted(TABLE_WHITELIST))
    parts: list[str] = []
    errs: list[str] = []
    for t in tables:
        if t not in cache:
            try:
                schema = schema_fn(t)
                # get_schema 返回 {"tables": [表dict]}；测试替身可能直接给内层 dict
                if isinstance(schema, dict) and isinstance(schema.get("tables"), list):
                    schema = schema["tables"][0] if schema["tables"] else None
                cache[t] = schema
            except Exception as err:  # noqa: BLE001 —— schema 失败降级，不中断分析
                errs.append(f"get_schema({t}) 失败：{err!r}")
                cache[t] = None
        if cache[t]:
            parts.append(_render_schema(cache[t]))
    ctx = "\n\n".join(parts) or "（schema 获取失败，按指标模板与表名生成）"
    return ctx, cache, errs


def _profile_hint(sql: str, profile_fn: ProfileFn) -> tuple[str, list[str]]:
    """空集纠错素材：解析 SQL 引用表，取优先列的 distinct/min/max/top 分布。"""
    tables: list[str] = []
    try:
        for stmt in sqlglot.parse(sql, read="postgres"):
            if stmt is None:
                continue
            for t in stmt.find_all(exp.Table):
                name = t.name.lower()
                if name in _PROFILE_COLUMNS and name not in tables:
                    tables.append(name)
    except sqlglot.errors.ParseError:
        pass
    lines: list[str] = []
    errs: list[str] = []
    budget = _MAX_PROFILES
    for t in tables[:2]:
        for c in _PROFILE_COLUMNS[t]:
            if budget <= 0:
                break
            budget -= 1
            try:
                p = profile_fn(t, c)
            except Exception as err:  # noqa: BLE001 —— 画像失败降级
                errs.append(f"get_data_profile({t}.{c}) 失败：{err!r}")
                continue
            top = ", ".join(f"{e['value']}({e['count']})" for e in p["top"][:3])
            lines.append(
                f"- {t}.{c}：distinct={p['distinct_count']}，空值率={p['null_rate']:.1%}，"
                f"min={p['min']}，max={p['max']}，top3=[{top}]"
            )
    return "\n".join(lines), errs


def _failures_text(state: GraphState) -> str:
    entries = [e for e in state["steps_log"] if e.get("outcome") not in ("ok", None)]
    if not entries:
        return "（无）"
    return "\n".join(
        f"- 步骤{entry['step_id']}「{entry['description']}」第 {entry['attempt']} 次尝试："
        f"[{entry['outcome']}] {entry.get('error') or ''}\n  SQL：{entry.get('sql') or '（无）'}"
        for entry in entries[-8:]
    )


def _unfinished_text(plan: list[dict]) -> str:
    items = [f"- 步骤{s['id']}「{s['description']}」{s['status']}" for s in plan
             if s["status"] != STEP_OK]
    return "\n".join(items) or "（无）"


# ---------- 节点工厂 ----------


def _make_planner_node(llm: LLMClient, budgets: Budgets) -> Callable[[GraphState], dict]:
    def planner_node(state: GraphState) -> dict:
        msgs = [
            {
                "role": "system",
                "content": PLANNER_SYSTEM.format(
                    catalog=catalog_text(),
                    max_steps=budgets.max_steps,
                    data_window=DATA_WINDOW,
                    today=_today(),
                ),
            },
            {"role": "user", "content": PLANNER_USER.format(question=state["question"])},
        ]
        try:
            content, usage = llm.chat(msgs)
        except Exception as err:  # noqa: BLE001 —— 环境级致命错误直达 reporter
            return {"fatal_error": f"planner LLM 调用失败：{err!r}"}
        qtype, steps = parse_plan(content, budgets.max_steps)
        if not steps:  # 规划失败兜底：按可答部分给单步通用计划（不反问，SPEC §4）
            qtype, steps = "unknown", [make_step(1, state["question"], None)]
        return {
            "question_type": qtype,
            "plan": steps,
            "step_cursor": 0,
            "attempt": 0,
            "feedback": None,
            "llm_calls": state["llm_calls"] + 1,
            "tokens_in": state["tokens_in"] + usage.tokens_in,
            "tokens_out": state["tokens_out"] + usage.tokens_out,
        }

    return planner_node


def _make_executor_node(
    llm: LLMClient, query_fn: QueryFn, schema_fn: SchemaFn, budgets: Budgets
) -> Callable[[GraphState], dict]:
    def executor_node(state: GraphState) -> dict:
        plan = [dict(s) for s in state["plan"]]
        cursor = state["step_cursor"]
        if cursor >= len(plan):
            return {}  # 防御：无可执行步骤（路由应已送 reporter）
        # 预算入口守卫：超限停止扩展，剩余步骤标记 skipped（EARS：输出 partial）
        if (
            state["llm_calls"] >= budgets.max_llm_calls
            or state["query_calls"] >= budgets.max_query_calls
        ):
            return {"plan": mark_remaining_skipped(plan, cursor)}

        step = plan[cursor]
        template = get_template(step.get("template"))
        cache = dict(state["schema_cache"])
        schema_ctx, cache, schema_errs = _schema_context(
            cache, template.tables if template else None, schema_fn
        )
        updates: dict = {"schema_cache": cache}
        if schema_errs:
            updates["errors"] = state["errors"] + schema_errs

        attempt = state["attempt"] + 1
        feedback_text = render_feedback(state["feedback"], max_corrections=budgets.max_corrections)
        user = EXECUTOR_USER.format(
            question=state["question"],
            step_no=cursor + 1,
            total=len(plan),
            description=step["description"],
            template_section=template_section(template),
            schema_ctx=schema_ctx,
            feedback_section=feedback_text,
            today=_today(),
            data_window=DATA_WINDOW,
        )
        try:
            content, usage = llm.chat(
                [{"role": "system", "content": EXECUTOR_SYSTEM}, {"role": "user", "content": user}]
            )
        except Exception as err:  # noqa: BLE001
            return {**updates, "fatal_error": f"executor LLM 调用失败：{err!r}"}
        updates["llm_calls"] = state["llm_calls"] + 1
        updates["tokens_in"] = state["tokens_in"] + usage.tokens_in
        updates["tokens_out"] = state["tokens_out"] + usage.tokens_out

        executed_recent = [
            e["sql"]
            for e in state["steps_log"]
            if e.get("fingerprint") and e.get("sql")
        ]
        log = {
            "step": cursor,
            "step_id": step["id"],
            "description": step["description"],
            "attempt": attempt,
            "ts": time.time(),
            "sql": None,
            "fingerprint": None,
            "outcome": None,
            "error": None,
            "row_count": None,
            "ms": None,
        }

        def _fail(
            outcome: str,
            message: str,
            *,
            sql: str | None = None,
            fingerprint: str | None = None,
        ) -> dict:
            fb_type = "error" if outcome in ("error", "invalid") else outcome
            fb: dict = {"type": fb_type, "attempt": attempt, "sql": sql, "message": message}
            if fb_type == "duplicate":
                fb["executed_recent"] = executed_recent
            if attempt - 1 >= budgets.max_corrections:  # 修正机会用完，步骤终局失败
                plan[cursor]["status"] = STEP_FAILED
            log.update(outcome=outcome, error=message, sql=sql, fingerprint=fingerprint)
            updates.update(
                attempt=attempt,
                feedback=fb,
                plan=plan,
                steps_log=state["steps_log"] + [log],
            )
            return updates

        def _ok(qr: QueryResult) -> dict:
            evidence = {
                "id": f"E{len(state['evidence']) + 1}",
                "description": step["description"],
                "sql": qr.sql,
                "fingerprint": qr.fingerprint,
                "row_count": qr.row_count,
                "rows_preview": [_jsonable_row(r) for r in qr.rows[:5]],
                "ms": qr.ms,
            }
            plan[cursor]["status"] = STEP_OK
            log.update(outcome="ok", sql=qr.sql, fingerprint=qr.fingerprint,
                       row_count=qr.row_count, ms=qr.ms)
            updates.update(
                plan=plan,
                step_cursor=cursor + 1,
                attempt=0,
                feedback=None,
                evidence=state["evidence"] + [evidence],
                steps_log=state["steps_log"] + [log],
            )
            return updates

        sql_text, validation = extract_sql(content)
        if not sql_text or validation is None:
            return _fail("no_sql", "回复中未找到可解析的只读 SELECT，请只输出一条 SQL")
        if not validation.ok:
            return _fail("invalid", f"[{validation.code}] {validation.reason}", sql=sql_text)
        fingerprints = list(state["fingerprints"])
        if validation.fingerprint in fingerprints:
            return _fail(
                "duplicate",
                "SQL 指纹与已执行语句相同（canonical SHA256 去重），未触库即被拦截",
                sql=validation.sql,
                fingerprint=validation.fingerprint,
            )
        try:
            qr = query_fn(validation.sql)
        except Exception as err:  # noqa: BLE001 —— 工具层异常也走回灌而非致命
            return _fail("error", f"查询执行异常：{err!r}",
                         sql=validation.sql, fingerprint=validation.fingerprint)
        updates["query_calls"] = state["query_calls"] + 1
        updates["fingerprints"] = fingerprints + [validation.fingerprint]
        if not qr.ok:
            return _fail("error", qr.error or "未知数据库错误",
                         sql=qr.sql or validation.sql, fingerprint=qr.fingerprint)
        if qr.row_count == 0:
            return _fail(
                "empty",
                "查询执行成功但返回 0 行：检查过滤值是否真实存在、时间窗是否有数据",
                sql=qr.sql,
                fingerprint=qr.fingerprint,
            )
        return _ok(qr)

    return executor_node


def _make_reflect_node(profile_fn: ProfileFn) -> Callable[[GraphState], dict]:
    def reflect_node(state: GraphState) -> dict:
        fb = dict(state["feedback"] or {})
        errors = list(state["errors"])
        if fb.get("type") == "empty":  # 空集：补数据分布提示（SPEC §6.2）
            hint, errs = _profile_hint(fb.get("sql") or "", profile_fn)
            errors.extend(errs)
            if hint:
                fb["hint"] = hint
        updates: dict = {"feedback": fb}
        if errors != state["errors"]:
            updates["errors"] = errors
        return updates

    return reflect_node


def _make_replan_node(llm: LLMClient, budgets: Budgets) -> Callable[[GraphState], dict]:
    def replan_node(state: GraphState) -> dict:
        msgs = [
            {
                "role": "system",
                "content": REPLAN_SYSTEM.format(
                    catalog=catalog_text(),
                    max_steps=budgets.max_steps,
                    data_window=DATA_WINDOW,
                    today=_today(),
                ),
            },
            {
                "role": "user",
                "content": REPLAN_USER.format(
                    question=state["question"], failures=_failures_text(state)
                ),
            },
        ]
        try:
            content, usage = llm.chat(msgs)
        except Exception as err:  # noqa: BLE001
            return {"fatal_error": f"replan LLM 调用失败：{err!r}"}
        qtype, steps = parse_plan(content, budgets.max_steps)
        updates = {
            "plan": steps,  # 空计划 → 路由送 reporter（failed）
            "replanned": True,
            "step_cursor": 0,
            "attempt": 0,
            "feedback": None,
            "llm_calls": state["llm_calls"] + 1,
            "tokens_in": state["tokens_in"] + usage.tokens_in,
            "tokens_out": state["tokens_out"] + usage.tokens_out,
        }
        if qtype != "unknown":
            updates["question_type"] = qtype
        return updates

    return replan_node


def _fallback_conclusion(
    state: GraphState, plan: list[dict], status: str, budgets: Budgets
) -> str:
    """预算耗尽 / 环境异常时的规则模板结论：只陈述事实，不推演新数值。"""
    lines = [f"### 结论（状态：{status}；规则模板生成——LLM 预算已耗尽或环境异常）",
             f"问题：{state['question']}"]
    if state.get("fatal_error"):
        lines.append(f"致命错误：{state['fatal_error']}")
    if state["evidence"]:
        lines.append("已完成证据：")
        lines.extend(
            f"- [{e['id']}] {e['description']}：{e['row_count']} 行，"
            f"结果预览 {json.dumps(e['rows_preview'][:1], ensure_ascii=False)}"
            for e in state["evidence"]
        )
    else:
        lines.append("没有成功执行的查询证据。")
    unfinished = _unfinished_text(plan)
    if unfinished != "（无）":
        lines.append(f"未完成 / 失败步骤：\n{unfinished}")
    lines.append(
        f"预算使用：LLM {state['llm_calls']}/{budgets.max_llm_calls} 次，"
        f"查询 {state['query_calls']}/{budgets.max_query_calls} 次；"
        "完整过程见步骤日志。"
    )
    return "\n".join(lines)


def _make_reporter_node(llm: LLMClient, budgets: Budgets) -> Callable[[GraphState], dict]:
    def reporter_node(state: GraphState) -> dict:
        plan = mark_remaining_skipped(state["plan"], state["step_cursor"])
        evidence = state["evidence"]
        has_failed = any(s["status"] == STEP_FAILED for s in plan)
        # 空 plan（replan 未产出新步骤）也视为未完成：有证据 → partial，无证据 → failed
        has_unfinished = (not plan) or any(s["status"] != STEP_OK for s in plan)
        if state.get("fatal_error") or not evidence:
            status = STATUS_FAILED
        elif has_failed or has_unfinished:
            status = STATUS_PARTIAL
        else:
            status = STATUS_ANSWERED
        updates: dict = {"plan": plan, "status": status}

        use_llm = not state.get("fatal_error") and state["llm_calls"] < budgets.max_llm_calls
        if use_llm:
            ev_lines = [
                f"- [{e['id']}] {e['description']}（{e['ms']}ms，{e['row_count']} 行）\n"
                f"  SQL：{e['sql']}\n"
                f"  结果预览：{json.dumps(e['rows_preview'], ensure_ascii=False)}"
                for e in evidence
            ]
            user = REPORTER_USER.format(
                question=state["question"],
                status=f"{status}（answered=完整回答 / partial=部分完成 / failed=未能回答）",
                evidence="\n".join(ev_lines) or "（无证据）",
                failures=_failures_text(state) + "\n未完成步骤：\n" + _unfinished_text(plan),
                llm_calls=state["llm_calls"],
                max_llm_calls=budgets.max_llm_calls,
                query_calls=state["query_calls"],
                max_query_calls=budgets.max_query_calls,
            )
            try:
                content, usage = llm.chat(
                    [
                        {"role": "system", "content": REPORTER_SYSTEM},
                        {"role": "user", "content": user},
                    ]
                )
                updates["conclusion"] = (
                    content.strip()
                    or _fallback_conclusion(state, plan, status, budgets)
                )
                updates["llm_calls"] = state["llm_calls"] + 1
                updates["tokens_in"] = state["tokens_in"] + usage.tokens_in
                updates["tokens_out"] = state["tokens_out"] + usage.tokens_out
                return updates
            except Exception as err:  # noqa: BLE001 —— 汇报失败也必须出结论
                updates["errors"] = state["errors"] + [f"reporter LLM 调用失败：{err!r}"]
        updates["conclusion"] = _fallback_conclusion(state, plan, status, budgets)
        return updates

    return reporter_node


# ---------- 装配与运行入口 ----------


def build_graph(
    *,
    llm: LLMClient | None = None,
    query_fn: QueryFn | None = None,
    schema_fn: SchemaFn | None = None,
    profile_fn: ProfileFn | None = None,
    budgets: Budgets | None = None,
    checkpointer: Any | None = None,
    trace: TraceRecorder | None = None,
) -> Any:
    """装配 StateGraph。依赖全部可注入（FakeLLM / 假工具供单测），默认接真实实现。

    ``trace`` 传入 TraceRecorder 时挂 T6 回调（LLM / 工具代理 + 节点差分采集）；
    缺省 None 完全不包装、零开销，行为与 T5 一致。
    """
    budget = budgets or DEFAULT_BUDGETS
    resolved_llm = llm or get_client()
    query = query_fn or run_query
    schema = schema_fn or get_schema
    profile = profile_fn or get_data_profile
    if trace is not None:
        resolved_llm = trace.wrap_llm(resolved_llm)
        query = trace.wrap_tool("run_query", query)
        schema = trace.wrap_tool("get_schema", schema)
        profile = trace.wrap_tool("get_data_profile", profile)

    def add_node(name: str, node: Callable[[GraphState], dict]) -> None:
        g.add_node(name, trace.wrap_node(name, node) if trace is not None else node)

    g = StateGraph(GraphState)
    add_node("planner", _make_planner_node(resolved_llm, budget))
    add_node("executor", _make_executor_node(resolved_llm, query, schema, budget))
    add_node("reflect", _make_reflect_node(profile))
    add_node("replan", _make_replan_node(resolved_llm, budget))
    add_node("reporter", _make_reporter_node(resolved_llm, budget))
    g.add_edge(START, "planner")
    g.add_conditional_edges(
        "planner", lambda s: route_after_planner(s, budget), ["executor", "reporter"]
    )
    g.add_conditional_edges(
        "executor",
        lambda s: route_after_executor(s, budget),
        ["executor", "reflect", "replan", "reporter"],
    )
    g.add_conditional_edges(
        "reflect", lambda s: route_after_reflect(s, budget), ["executor", "reporter"]
    )
    g.add_conditional_edges(
        "replan", lambda s: route_after_replan(s, budget), ["executor", "reporter"]
    )
    g.add_edge("reporter", END)
    return g.compile(checkpointer=checkpointer)


def run_analysis(
    question: str,
    *,
    llm: LLMClient | None = None,
    budgets: Budgets | None = None,
    checkpoint_path: str | None = "runs/checkpoints.db",
    thread_id: str | None = None,
    trace: bool = True,
    trace_root: str = "runs",
) -> dict:
    """一次完整分析（真实依赖）：SqliteSaver 持久化，thread_id 每次分析独立。

    T6：默认每次分析落 ``<trace_root>/<run_id>/trace.jsonl``（SPEC §Q10），
    answered / partial / failed / 异常中断均完整落盘，返回值带 trace_dir。
    T7 的 analyze() 在此之上补 AnalysisResult 契约；本函数已可直接跑冒烟。
    """
    recorder = TraceRecorder(trace_root, question=question) if trace else None
    conn: sqlite3.Connection | None = None
    checkpointer: Any | None = None
    if checkpoint_path:
        Path(checkpoint_path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(checkpoint_path, check_same_thread=False)
        checkpointer = SqliteSaver(conn)
    tid = thread_id or uuid.uuid4().hex
    try:
        graph = build_graph(
            llm=llm or get_client(), budgets=budgets, checkpointer=checkpointer,
            trace=recorder,
        )
        final = graph.invoke(
            init_state(question),
            {"configurable": {"thread_id": tid}, "recursion_limit": 100},
        )
        if recorder is not None:
            recorder.finalize(final, extra={"thread_id": tid})
    except Exception as err:  # noqa: BLE001 —— 异常中断也落 failed trace 再上抛
        if recorder is not None:
            recorder.finalize(None, error=err, extra={"thread_id": tid})
        raise
    finally:
        if conn is not None:
            conn.close()
        if recorder is not None:
            recorder.close()
    return {
        "status": final["status"],
        "conclusion": final["conclusion"],
        "evidence": final["evidence"],
        "steps_log": final["steps_log"],
        "plan": final["plan"],
        "question": question,
        "question_type": final["question_type"],
        "usage": {
            "llm_calls": final["llm_calls"],
            "query_calls": final["query_calls"],
            "tokens_in": final["tokens_in"],
            "tokens_out": final["tokens_out"],
        },
        "errors": final["errors"],
        "thread_id": tid,
        "trace_dir": str(recorder.dir) if recorder is not None else None,
    }
