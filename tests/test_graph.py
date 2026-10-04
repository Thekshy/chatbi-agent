"""T5 测试：StateGraph 编排核心——五节点 + 条件边 + 防失控三件套（SPEC §6.2 / Q5 / Q9）。

先测后写（T5 ⑥）：本用例集即验收清单——
- 纯函数：四个条件边路由的分流表（预算耗尽 / 步骤完成 / 需 Replan）；
- FakeLLM 端到端（不触库、不打网络）：预算耗尽 → partial、重复 SQL 指纹 → 拦截换路径、
  报错 → ≤2 次自修正后 Replan、空集 → 回灌数据分布、SqliteSaver 持久化；
- metrics：模板 example_sql 全部过 validate_sql（白名单 + 可解析）；
- integration（真实 PG + 真实 LLM，缺失自动 skip）：3 个冒烟问题端到端 answered（单指标 /
  同环比 / TopN），数值与裁判 SQL 交叉验证（±2%）。
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Callable
from decimal import Decimal, InvalidOperation
from typing import Any

import pytest

from chatbi.graph import (
    DEFAULT_BUDGETS,
    build_graph,
    route_after_executor,
    route_after_planner,
    route_after_reflect,
    route_after_replan,
    run_analysis,
)
from chatbi.llm import LLMClient, LLMConfigError, LLMUsage
from chatbi.metrics import METRIC_TEMPLATES, catalog_text
from chatbi.state import (
    STEP_PENDING,
    STEP_SKIPPED,
    Budgets,
    GraphState,
    init_state,
)
from chatbi.tools import TABLE_WHITELIST, QueryResult, run_query, validate_sql

# ---------- fakes ----------


class FakeLLM:
    """按脚本顺序回放的 LLM 替身；自记调用数与消息，供断言注入内容。"""

    def __init__(self, script: list[str]) -> None:
        self.script = list(script)
        self.calls = 0
        self.messages_log: list[list[dict[str, str]]] = []

    def chat(self, messages: Any, **kwargs: Any) -> tuple[str, LLMUsage]:
        self.calls += 1
        self.messages_log.append([dict(m) for m in messages])
        if not self.script:
            raise AssertionError(f"LLM 调用超出脚本：第 {self.calls} 次")
        return self.script.pop(0), LLMUsage(tokens_in=10, tokens_out=5)


def fake_query(sql: str) -> QueryResult:
    """SQL 文本含 'boom' → 数据库错误；含 'empty' → 空集；其余成功 1 行。"""
    v = validate_sql(sql)
    assert v.ok and v.sql and v.fingerprint
    low = v.sql.lower()
    if "boom" in low:
        return QueryResult(
            ok=False, sql=v.sql, fingerprint=v.fingerprint, ms=0.5,
            error="数据库错误：boom（测试注入）",
        )
    rows = [] if "empty" in low else [{"gmv": Decimal("123.45"), "n": 7}]
    return QueryResult(
        ok=True, sql=v.sql, fingerprint=v.fingerprint,
        rows=rows, row_count=len(rows), ms=0.5,
    )


def fake_schema(table: str) -> dict:
    return {
        "table": table,
        "row_count": 12_345,
        "columns": [
            {"name": "order_id", "type": "bigint", "nullable": False},
            {"name": "status", "type": "text", "nullable": False},
        ],
        "enum_values": {"status": ["paid", "shipped", "completed", "cancelled"]},
        "sample_rows": [{"order_id": 1, "status": "paid", "paid_at": "2026-06-01T08:00:00"}],
    }


class FakeProfile:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def __call__(self, table: str, column: str) -> dict:
        self.calls.append((table, column))
        return {
            "table": table, "column": column, "type": "text",
            "total": 100_000, "null_count": 0, "null_rate": 0.0, "distinct_count": 6,
            "min": "a", "max": "z",
            "top": [{"value": "面霜", "count": 500}, {"value": "面膜", "count": 400}],
        }


def plan_json(*descs: str, qtype: str = "single_metric") -> str:
    return json.dumps(
        {"question_type": qtype, "steps": [{"description": d} for d in descs]},
        ensure_ascii=False,
    )


def make_graph(
    script: list[str],
    *,
    query_fn: Callable[[str], QueryResult] = fake_query,
    budgets: Budgets | None = None,
) -> tuple[Any, FakeLLM]:
    llm = FakeLLM(script)
    graph = build_graph(
        llm=llm, query_fn=query_fn, schema_fn=fake_schema,
        profile_fn=FakeProfile(), budgets=budgets,
    )
    return graph, llm


def run(graph: Any, question: str = "测试问题") -> GraphState:
    cfg = {"configurable": {"thread_id": uuid.uuid4().hex}, "recursion_limit": 100}
    return graph.invoke(init_state(question), cfg)


# SQL 台词：canonical 指纹互不相同（字面量不同）；标记词放在字符串字面量内防被规范化剥掉
SQL = [
    f"SELECT {n} AS v FROM orders WHERE status = 'x{n}'" for n in range(1, 9)
]  # 成功用
BOOM = [
    f"SELECT {n} AS v FROM orders WHERE status = 'boom{n}'" for n in range(11, 19)
]  # 报错用
EMPTY_SQL = f"SELECT {21} AS v FROM orders WHERE status = 'empty'"
GARBAGE = "这个问题我无法生成 SQL。"

# ---------- 纯函数：预算常量与初始状态 ----------


def test_default_budgets_pin_spec() -> None:
    assert (DEFAULT_BUDGETS.max_steps, DEFAULT_BUDGETS.max_llm_calls,
            DEFAULT_BUDGETS.max_query_calls, DEFAULT_BUDGETS.max_corrections) == (6, 8, 5, 2)


def test_init_state_defaults() -> None:
    s = init_state("q1")
    assert s["question"] == "q1"
    assert s["plan"] == [] and s["step_cursor"] == 0 and s["attempt"] == 0
    assert s["feedback"] is None and s["evidence"] == [] and s["steps_log"] == []
    assert s["fingerprints"] == [] and s["llm_calls"] == 0 and s["query_calls"] == 0
    assert s["errors"] == [] and s["replanned"] is False and s["fatal_error"] is None
    assert s["status"] == "" and s["conclusion"] == ""


# ---------- 纯函数：条件边分流表 ----------


def st(**over: Any) -> GraphState:
    s = init_state("q")
    s.update(over)
    return s


def test_route_after_planner_branches() -> None:
    assert route_after_planner(st(fatal_error="LLM 挂了")) == "reporter"
    assert route_after_planner(st()) == "executor"


def test_route_after_executor_branches() -> None:
    pending = [{"id": 1, "description": "d", "template": None, "status": STEP_PENDING}]
    # 致命错误 / 预算耗尽 → reporter（partial 由 reporter 定状态）
    assert route_after_executor(st(fatal_error="x")) == "reporter"
    assert route_after_executor(st(llm_calls=8)) == "reporter"
    assert route_after_executor(st(query_calls=5)) == "reporter"
    # 步骤成功：还有步骤 → executor；全部完成 → reporter
    assert route_after_executor(
        st(plan=pending * 2, step_cursor=0, attempt=0, feedback=None)
    ) == "executor"
    assert route_after_executor(st(plan=pending, step_cursor=1, attempt=0,
                                   feedback=None)) == "reporter"
    # 步骤失败：修正机会未用完（<2）→ reflect；用完且未 Replan → replan；已 Replan → reporter
    for attempt in (1, 2):
        assert route_after_executor(
            st(plan=pending, attempt=attempt, feedback={"type": "error"})
        ) == "reflect"
    assert route_after_executor(
        st(plan=pending, attempt=3, feedback={"type": "error"})
    ) == "replan"
    assert route_after_executor(
        st(plan=pending, attempt=3, feedback={"type": "error"}, replanned=True)
    ) == "reporter"


def test_route_after_reflect_branches() -> None:
    assert route_after_reflect(st(fatal_error="x")) == "reporter"
    assert route_after_reflect(st(llm_calls=8)) == "reporter"
    assert route_after_reflect(st()) == "executor"


def test_route_after_replan_branches() -> None:
    pending = [{"id": 1, "description": "d", "template": None, "status": STEP_PENDING}]
    assert route_after_replan(st(fatal_error="x")) == "reporter"
    assert route_after_replan(st(llm_calls=8)) == "reporter"
    assert route_after_replan(st(query_calls=5)) == "reporter"
    assert route_after_replan(st(plan=[])) == "reporter"  # replan 未产出新计划
    assert route_after_replan(st(plan=pending)) == "executor"


# ---------- FakeLLM 端到端：happy path 与预算三件套 ----------


def test_happy_single_step_answered() -> None:
    graph, llm = make_graph(
        [plan_json("查询 GMV", qtype="single_metric"),
         f"```sql\n{SQL[0]}\n```",  # 代码块包裹：SQL 提取须能剥壳
         "结论：GMV 为 123.45 元 [E1]"]
    )
    final = run(graph, "上个月护肤品类 GMV 是多少")
    assert final["status"] == "answered"
    assert final["question_type"] == "single_metric"
    assert final["conclusion"].startswith("结论：GMV 为 123.45 元")
    assert len(final["evidence"]) == 1
    ev = final["evidence"][0]
    assert ev["id"] == "E1" and ev["row_count"] == 1 and ev["fingerprint"]
    assert ev["rows_preview"][0]["gmv"] == "123.45"  # Decimal JSON 化为字符串
    assert final["llm_calls"] == 3 and final["query_calls"] == 1  # 规划+生成+汇报 / 1 次执行
    assert final["tokens_in"] == 30 and final["tokens_out"] == 15
    assert final["steps_log"][0]["outcome"] == "ok"
    assert llm.calls == 3


def test_multi_step_answered() -> None:
    graph, _ = make_graph(
        [plan_json("本期 GMV", "上期 GMV", qtype="period_compare"),
         SQL[1], SQL[2], "结论：环比上升 5% [E1][E2]"]
    )
    final = run(graph)
    assert final["status"] == "answered"
    assert [e["id"] for e in final["evidence"]] == ["E1", "E2"]
    assert final["query_calls"] == 2 and final["llm_calls"] == 4
    assert all(s["status"] == "ok" for s in final["plan"])


def test_query_budget_exhausted_partial() -> None:
    # 4 步计划：s1 成功(q1)；s2 报错两次后成功(q2-q4)；s3 成功(q5) → query 预算达 5，
    # s4 未执行 → partial（预算三件套：run_query ≤5）
    graph, llm = make_graph(
        [plan_json("s1", "s2", "s3", "s4"),
         SQL[3], BOOM[0], BOOM[1], SQL[4], SQL[5],
         "部分结论：已完成 3 步 [E1][E2][E3]"]
    )
    final = run(graph)
    assert final["status"] == "partial"
    assert final["query_calls"] == 5 and final["llm_calls"] == 7
    assert len(final["evidence"]) == 3
    assert final["plan"][3]["status"] == STEP_SKIPPED  # 预算耗尽未执行的步骤标记 skipped
    assert [entry["outcome"] for entry in final["steps_log"]] == [
        "ok", "error", "error", "ok", "ok",
    ]
    assert llm.calls == 7


def test_llm_budget_exhausted_deterministic_reporter() -> None:
    # 全程生成失败（无 SQL 可提取，不消耗 query 预算）：规划 1 + 3 次生成 + 重规划 1
    # + 3 次生成 = 8 次 LLM → reporter 无预算调用 LLM，走规则模板结论
    graph, llm = make_graph(
        [plan_json("s1"), GARBAGE, GARBAGE, GARBAGE,
         plan_json("换路径重查"), GARBAGE, GARBAGE, GARBAGE]
    )
    final = run(graph)
    assert final["status"] == "failed"
    assert final["llm_calls"] == 8 and final["query_calls"] == 0
    assert final["replanned"] is True and final["evidence"] == []
    assert all(e["outcome"] == "no_sql" for e in final["steps_log"])
    assert len(final["steps_log"]) == 6  # 两次计划各 3 次尝试
    assert "预算" in final["conclusion"]  # 规则模板结论如实说明
    assert llm.calls == 8


def test_plan_truncated_by_budgets() -> None:
    graph, _ = make_graph(
        [plan_json(*[f"s{i}" for i in range(7)]), SQL[6], "结论 [E1]"],
        budgets=Budgets(max_steps=1, max_llm_calls=8, max_query_calls=5, max_corrections=2),
    )
    final = run(graph)
    assert len(final["plan"]) == 1  # maxSteps 截断
    assert final["status"] == "answered" and len(final["evidence"]) == 1


# ---------- FakeLLM 端到端：SQL 指纹去重 ----------


def test_duplicate_sql_intercepted_then_switch() -> None:
    dup_variant = SQL[7].lower() + " -- 同一条查询"  # 大小写/注释变体 → canonical 同指纹
    graph, llm = make_graph(
        [plan_json("s1", "s2"), SQL[7], dup_variant, SQL[6], "结论 [E1][E2]"]
    )
    final = run(graph)
    assert final["status"] == "answered"
    assert final["query_calls"] == 2  # 重复 SQL 未触库即被拦截
    assert [entry["outcome"] for entry in final["steps_log"]] == ["ok", "duplicate", "ok"]
    assert final["steps_log"][1]["fingerprint"] == final["steps_log"][0]["fingerprint"]
    assert len({e["fingerprint"] for e in final["evidence"]}) == 2
    # 重试的 executor 提示词里带「重复」反馈，引导换路径
    assert any("重复" in m["content"] for m in llm.messages_log[3])
    assert llm.calls == 5


def test_duplicate_intercept_does_not_consume_query_budget() -> None:
    dup = SQL[0]
    graph, _ = make_graph([plan_json("s1", "s2"), dup, dup, SQL[1], "结论 [E1][E2]"])
    final = run(graph)
    assert final["query_calls"] == 2  # 第二条 dup 被拦截时 query_calls 不增加
    assert final["steps_log"][1]["outcome"] == "duplicate"


# ---------- FakeLLM 端到端：纠错与 Replan ----------


def test_error_two_corrections_then_replan_success() -> None:
    # 首计划 3 次尝试（1+2 次修正）全报错 → Replan 一次 → 新路径成功 → answered
    graph, llm = make_graph(
        [plan_json("s1"), BOOM[2], BOOM[3], BOOM[4],
         plan_json("换路径"), SQL[2], "结论：成功 [E1]"]
    )
    final = run(graph)
    assert final["status"] == "answered" and final["replanned"] is True
    assert final["query_calls"] == 4 and final["llm_calls"] == 7
    assert [entry["outcome"] for entry in final["steps_log"]] == ["error", "error", "error", "ok"]
    # Replan 提示词携带失败记录（错误原文回灌）：第 5 次调用是 replan
    assert any("boom" in m["content"] for m in llm.messages_log[4])
    # 每条 SQL 修正机会 ≤2：两次失败后的重试提示词都带反馈与错误原文
    for idx in (2, 3):
        joined = "".join(m["content"] for m in llm.messages_log[idx])
        assert "上一轮反馈" in joined and "boom" in joined, f"call#{idx} 缺反馈"


def test_error_feedback_carries_db_error_text() -> None:
    graph, llm = make_graph([plan_json("s1"), BOOM[5], SQL[0], "结论 [E1]"])
    final = run(graph)
    assert final["status"] == "answered"
    assert any("boom" in m["content"] for m in llm.messages_log[2])  # 错误原文进重试提示词


def test_all_fail_after_replan_yields_failed() -> None:
    # 首计划与重规划后均持续报错：query 预算 5 先耗尽 → failed（无任何证据）
    graph, _ = make_graph(
        [plan_json("s1"), BOOM[6], BOOM[7],
         f"SELECT {31} AS v FROM orders WHERE status = 'boom31'",
         plan_json("换路径"),
         f"SELECT {32} AS v FROM orders WHERE status = 'boom32'",
         f"SELECT {33} AS v FROM orders WHERE status = 'boom33'",
         "失败报告"]
    )
    final = run(graph)
    assert final["status"] == "failed" and final["evidence"] == []
    assert final["query_calls"] == 5 and final["replanned"] is True


# ---------- FakeLLM 端到端：空集回灌与提示词注入 ----------


def test_empty_result_triggers_profile_feedback() -> None:
    profile = FakeProfile()
    llm = FakeLLM([plan_json("s1"), EMPTY_SQL, SQL[0], "结论 [E1]"])
    graph = build_graph(llm=llm, query_fn=fake_query, schema_fn=fake_schema,
                        profile_fn=profile)
    final = graph.invoke(init_state("q"), {"configurable": {"thread_id": "t-empty"}})
    assert final["status"] == "answered"
    assert [entry["outcome"] for entry in final["steps_log"]] == ["empty", "ok"]
    assert profile.calls, "reflect 应调用 get_data_profile 取数据分布"
    # 数据分布提示（top 值）注入下一次生成的提示词
    assert any("面霜" in m["content"] for m in llm.messages_log[2])


def test_executor_prompt_injects_template_and_schema() -> None:
    graph, llm = make_graph(
        [json.dumps({"question_type": "single_metric",
                     "steps": [{"description": "查 GMV", "template": "gmv"}]},
                    ensure_ascii=False),
         SQL[0], "结论 [E1]"]
    )
    final = run(graph, "6 月护肤 GMV")
    assert final["status"] == "answered"
    executor_msgs = llm.messages_log[1]
    joined = "".join(m["content"] for m in executor_msgs)
    assert "unit_price" in joined  # 指标模板 SQL 模式
    assert "paid_at" in joined and "orders" in joined  # 口径要点 + 表结构
    assert "order_id" in joined  # schema 注入


def test_planner_fenced_json_parsed() -> None:
    fenced = "```json\n" + plan_json("s1") + "\n```"
    graph, _ = make_graph([fenced, SQL[0], "结论 [E1]"])
    final = run(graph)
    assert final["question_type"] == "single_metric"
    assert final["status"] == "answered"


def test_planner_invalid_json_falls_back_single_step() -> None:
    graph, _ = make_graph(["我不会规划", SQL[0], "结论 [E1]"])
    final = run(graph, "任意问题")
    assert len(final["plan"]) == 1
    assert final["question_type"] == "unknown"
    assert final["plan"][0]["description"] == "任意问题"  # 兜底步骤继承原问题
    assert final["status"] == "answered"


# ---------- SqliteSaver checkpointer ----------


def test_sqlite_checkpointer_persists(tmp_path: Any) -> None:
    conn = sqlite3.connect(tmp_path / "cp.db", check_same_thread=False)
    saver = sqlite_saver(conn)
    llm = FakeLLM([plan_json("s1"), SQL[0], "结论 [E1]"])
    graph = build_graph(llm=llm, query_fn=fake_query, schema_fn=fake_schema,
                        profile_fn=FakeProfile(), checkpointer=saver)
    cfg = {"configurable": {"thread_id": "t-1"}, "recursion_limit": 100}
    final = graph.invoke(init_state("持久化测试"), cfg)
    assert final["status"] == "answered"
    snap = graph.get_state(cfg)
    assert snap.values["question"] == "持久化测试"
    assert snap.values["status"] == "answered"
    assert len(list(saver.list(cfg))) >= 3  # 每个超步至少一个检查点
    conn.close()


def sqlite_saver(conn: sqlite3.Connection) -> Any:
    from langgraph.checkpoint.sqlite import SqliteSaver

    return SqliteSaver(conn)


# ---------- metrics：指标字典模板契约 ----------


def test_metric_templates_examples_pass_validation() -> None:
    assert len(METRIC_TEMPLATES) >= 10  # 8 指标 + 2 下钻 + promotions_lookup
    for t in METRIC_TEMPLATES.values():
        v = validate_sql(t.example_sql)
        assert v.ok, f"{t.key}: {v.code} {v.reason}"
        assert set(t.tables) <= TABLE_WHITELIST
        assert t.sql.strip() and t.notes and t.title


def test_catalog_text_lists_all_templates() -> None:
    text = catalog_text()
    for t in METRIC_TEMPLATES.values():
        assert t.key in text and t.title in text


# ---------- integration：真实 PG + 真实 LLM 冒烟（3 问端到端） ----------

_REF_GMV_SKINCARE_JUN = (
    "SELECT SUM(oi.unit_price * oi.quantity) AS gmv "
    "FROM orders o JOIN order_items oi ON oi.order_id = o.order_id "
    "JOIN products p ON p.product_id = oi.product_id "
    "WHERE o.status IN ('paid','shipped','completed') "
    "AND o.paid_at >= '2026-06-01' AND o.paid_at < '2026-07-01' "
    "AND p.category_l1 = '护肤'"
)
_REF_GMV_MONTHS = (
    "SELECT to_char(o.paid_at, 'YYYY-MM') AS ym, SUM(oi.unit_price * oi.quantity) AS gmv "
    "FROM orders o JOIN order_items oi ON oi.order_id = o.order_id "
    "JOIN products p ON p.product_id = oi.product_id "
    "WHERE o.status IN ('paid','shipped','completed') "
    "AND o.paid_at >= '2026-05-01' AND o.paid_at < '2026-07-01' GROUP BY 1"
)
_REF_TOPN_618 = (
    "SELECT p.brand, SUM(oi.unit_price * oi.quantity) AS gmv, "
    "SUM(oi.unit_price * oi.quantity) / SUM(SUM(oi.unit_price * oi.quantity)) OVER () "
    "AS share "
    "FROM orders o JOIN order_items oi ON oi.order_id = o.order_id "
    "JOIN products p ON p.product_id = oi.product_id "
    "WHERE o.status IN ('paid','shipped','completed') "
    "AND o.paid_at >= '2026-05-31' AND o.paid_at < '2026-06-19' "
    "GROUP BY 1 ORDER BY 2 DESC LIMIT 5"
)


def _numbers(rows: list[dict]) -> list[Decimal]:
    out: list[Decimal] = []
    for row in rows:
        for v in row.values():
            try:
                out.append(Decimal(str(v)))
            except InvalidOperation:
                pass
    return out


def _hit(value: Decimal, numbers: list[Decimal], tol: float = 0.02) -> bool:
    return any(abs(n - value) / abs(value) <= tol for n in numbers if n != 0)


@pytest.fixture(scope="module")
def real_env() -> LLMClient:
    try:
        res = run_query("SELECT count(*) AS n FROM orders")
    except Exception:  # noqa: BLE001 —— 环境不可用一律转 skip
        pytest.skip("本机 PG 不可连（先 brew services 起 PG）")
    if not (res.ok and res.rows[0]["n"] > 1000):
        pytest.skip("库中无 mock 数据（先 uv run python -m data.mock_gen）")
    try:
        return LLMClient()
    except LLMConfigError:
        pytest.skip("未配置 ZHIPU_API_KEY，跳过真实冒烟")


def _analyze(llm: LLMClient, question: str, tmp_path: Any, name: str) -> dict:
    return run_analysis(
        question, llm=llm, checkpoint_path=str(tmp_path / f"{name}.db"),
        thread_id=f"smoke-{name}-{uuid.uuid4().hex[:8]}",
    )


@pytest.mark.integration
def test_smoke_single_metric_gmv(real_env: LLMClient, tmp_path: Any) -> None:
    result = _analyze(real_env, "2026年6月护肤品类的GMV是多少", tmp_path, "single")
    assert result["status"] == "answered", result["conclusion"]
    assert result["evidence"], "answered 必须携带证据"
    ref = run_query(_REF_GMV_SKINCARE_JUN).rows[0]["gmv"]
    numbers = [n for e in result["evidence"] for n in _numbers(e["rows_preview"])]
    assert _hit(Decimal(str(ref)), numbers), f"护肤 6 月 GMV {ref} 未出现在证据数值中"


@pytest.mark.integration
def test_smoke_period_compare(real_env: LLMClient, tmp_path: Any) -> None:
    result = _analyze(real_env, "2026年6月全场GMV环比5月变化多少", tmp_path, "compare")
    assert result["status"] == "answered", result["conclusion"]
    refs = {r["ym"]: Decimal(str(r["gmv"])) for r in run_query(_REF_GMV_MONTHS).rows}
    assert set(refs) == {"2026-05", "2026-06"}
    numbers = [n for e in result["evidence"] for n in _numbers(e["rows_preview"])]
    for ym, v in refs.items():
        assert _hit(v, numbers), f"{ym} GMV {v} 未出现在证据数值中"


@pytest.mark.integration
def test_smoke_topn_brands(real_env: LLMClient, tmp_path: Any) -> None:
    # 日期边界显式钉死（SPEC §8.2：边界在标注中约定），避免「618期间」的自然歧义
    result = _analyze(real_env, "618活动期间（5月31日至6月18日）GMV前5品牌及其占比是多少",
                      tmp_path, "topn")
    assert result["status"] == "answered", result["conclusion"]
    ref_rows = run_query(_REF_TOPN_618).rows
    top1_brand, top1_gmv = ref_rows[0]["brand"], Decimal(str(ref_rows[0]["gmv"]))
    assert top1_brand == "雪滢"  # SPEC §5.3 定稿：全场 Top1 恒为雪滢
    numbers = [n for e in result["evidence"] for n in _numbers(e["rows_preview"])]
    assert _hit(top1_gmv, numbers), f"618 Top1 品牌 GMV {top1_gmv} 未出现在证据数值中"
    evidence_text = json.dumps(
        [e["rows_preview"] for e in result["evidence"]], ensure_ascii=False
    )
    assert "雪滢" in evidence_text or "雪滢" in result["conclusion"]
    # 真实跑也要守住预算三件套
    assert 1 <= result["usage"]["llm_calls"] <= 8
    assert 1 <= result["usage"]["query_calls"] <= 5
