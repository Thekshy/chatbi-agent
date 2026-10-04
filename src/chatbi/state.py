"""GraphState：LangGraph 全图共享状态与预算契约（T5 / SPEC §6.2、Q5）。

状态字段分四组：
- 计划与进度：``plan``（步骤列表）/ ``step_cursor`` / ``attempt``（当前步骤 SQL 生成尝试次数）；
- 预算计数：``llm_calls`` / ``query_calls``（steps 由 plan 长度约束，上限 max_steps）；
- 防失控：``fingerprints``（已执行 SQL 的 canonical SHA256 集合，重复即拦截）、
  ``errors`` 错误栈、``fatal_error`` 环境级致命错误（LLM 断连等，直接走 reporter）；
- 产出：``evidence``（成功证据，reporter 唯一数据源）/ ``steps_log``（每次生成尝试的
  审计记录，T6 trace 的素材）/ ``conclusion`` / ``status``。

预算三件套常量是 SPEC Q5 拍板值（maxSteps=6 / LLM ≤8 / run_query ≤5），
修正机会 per SQL ≤2 来自 Q9；不要在调用点散落魔数，统一引用 Budgets。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TypedDict

# 步骤状态
STEP_PENDING = "pending"
STEP_OK = "ok"
STEP_FAILED = "failed"
STEP_SKIPPED = "skipped"  # 预算耗尽未执行

# 分析三态（SPEC §7）：reporter 落定，其余节点不得写
STATUS_ANSWERED = "answered"
STATUS_PARTIAL = "partial"
STATUS_FAILED = "failed"


@dataclass(frozen=True)
class Budgets:
    """防失控三件套 + 修正机会（SPEC Q5 / Q9）。"""

    max_steps: int = 6
    max_llm_calls: int = 8
    max_query_calls: int = 5
    max_corrections: int = 2  # 每条 SQL 的 Reflexion 自修正次数上限


DEFAULT_BUDGETS = Budgets()


class GraphState(TypedDict):
    """全图共享状态：节点返回局部更新，键级整体替换（无并发分支）。"""

    # 输入
    question: str
    question_type: str  # planner 判定的问法类型（4+1 类，SPEC §3）
    # 计划与进度
    plan: list[dict]  # [{id, description, template, status}]
    step_cursor: int  # 当前执行到的步骤下标
    attempt: int  # 当前步骤第 attempt 次生成尝试（1 起；0 表示尚未尝试）
    feedback: dict | None  # 回灌上下文 {type, sql, message, hint?, attempt}
    # 预算计数
    llm_calls: int
    query_calls: int
    tokens_in: int
    tokens_out: int
    # 防失控
    fingerprints: list[str]  # 已执行 SQL 的 canonical SHA256（去重拦截集）
    errors: list[str]  # 非致命错误栈（工具失败等，供 trace 与回灌）
    fatal_error: str | None  # 致命错误（LLM 断连等）：置位后直达 reporter
    replanned: bool  # 是否已 Replan 过（至多一次，Q9）
    # 产出
    evidence: list[dict]  # [{id, description, sql, fingerprint, row_count, rows_preview, ms}]
    steps_log: list[dict]  # 每次生成尝试的审计记录（含失败与拦截）
    schema_cache: dict[str, dict]  # get_schema 结果缓存（表名 → schema dict）
    status: str  # answered | partial | failed，reporter 落定
    conclusion: str


def init_state(question: str) -> GraphState:
    """一次分析的初始状态（全字段显式给出，便于 checkpointer 完整序列化）。"""
    return GraphState(
        question=question,
        question_type="unknown",
        plan=[],
        step_cursor=0,
        attempt=0,
        feedback=None,
        llm_calls=0,
        query_calls=0,
        tokens_in=0,
        tokens_out=0,
        fingerprints=[],
        errors=[],
        fatal_error=None,
        replanned=False,
        evidence=[],
        steps_log=[],
        schema_cache={},
        status="",
        conclusion="",
    )


def make_step(idx: int, description: str, template: str | None) -> dict:
    return {"id": idx, "description": description, "template": template, "status": STEP_PENDING}


def mark_remaining_skipped(plan: list[dict], cursor: int) -> list[dict]:
    """把 cursor 起的 pending 步骤标记为 skipped（预算耗尽停止扩展时）。"""
    out = [dict(s) for s in plan]
    for s in out[cursor:]:
        if s["status"] == STEP_PENDING:
            s["status"] = STEP_SKIPPED
    return out
