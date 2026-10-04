"""T6 测试：trace 落盘——事件序列、字段完整、三态全覆盖（SPEC §Q10 / §8.1）。

先测后写（T6 ⑥）：本用例集即验收清单——
- FakeLLM 端到端（不触库、不打网络）：happy path 事件序列钉死
  planner→executor→工具→reporter 全链路；partial / failed / LLM 异常中断
  同样完整落盘；sql_result 的 outcome 全枚举（ok/error/empty/duplicate/no_sql）；
- usage 汇总与 LLM 返回逐字一致；费用估算默认关、显式 cost_model 才有 cost_est；
- run_analysis 默认落 runs/<run_id>/trace.jsonl，异常中断也出 failed trace；
- integration（真实 PG + 真实 LLM，缺失自动 skip）：一问冒烟，全链路事件与
  usage 交叉验证。
"""

from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from typing import Any

import pytest
from test_graph import (
    BOOM,
    EMPTY_SQL,
    GARBAGE,
    SQL,
    FakeLLM,
    FakeProfile,
    fake_query,
    fake_schema,
    plan_json,
)

from chatbi.graph import build_graph, run_analysis
from chatbi.state import init_state
from chatbi.tracing import CostModel, TraceRecorder

# ---------- 助手 ----------


def traced(
    tmp_path: Path,
    script: list[str],
    *,
    budgets: Any = None,
    cost_model: CostModel | None = None,
    question: str = "测试问题",
) -> tuple[Any, FakeLLM, TraceRecorder]:
    rec = TraceRecorder(tmp_path / "runs", question=question, cost_model=cost_model)
    llm = FakeLLM(script)
    graph = build_graph(
        llm=llm,
        query_fn=fake_query,
        schema_fn=fake_schema,
        profile_fn=FakeProfile(),
        budgets=budgets,
        trace=rec,
    )
    return graph, llm, rec


def run_and_finalize(graph: Any, rec: TraceRecorder, question: str = "测试问题") -> dict:
    cfg = {"configurable": {"thread_id": uuid.uuid4().hex}, "recursion_limit": 100}
    final = graph.invoke(init_state(question), cfg)
    rec.finalize(final)
    rec.close()
    return final


def events_of(rec: TraceRecorder) -> list[dict]:
    lines = rec.path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines]


# 各事件类型的必备字段（公共字段 seq/ts/at 之外）
_REQUIRED: dict[str, set[str]] = {
    "message": {"node", "messages", "response", "ms", "tokens_in", "tokens_out"},
    "tool_call": {"node", "tool", "args", "ms", "ok"},
    "sql_result": {"node", "step_id", "attempt", "outcome", "sql", "row_count", "ms"},
    "step": {"node", "event"},
    "conclusion": {"status", "conclusion", "evidence", "question"},
    "usage": {"llm_calls", "query_calls", "tokens_in", "tokens_out", "wall_ms"},
}


def assert_event_schema(events: list[dict]) -> None:
    assert events, "trace 不能为空"
    for i, ev in enumerate(events, 1):
        assert ev["seq"] == i, f"第 {i} 行 seq 不连续：{ev}"
        assert isinstance(ev["ts"], (int, float)) and ev["ts"] > 0
        assert isinstance(ev["at"], str)
        assert ev["type"] in _REQUIRED, f"未知事件类型：{ev['type']}"
        missing = _REQUIRED[ev["type"]] - set(ev)
        assert not missing, f"{ev['type']} 缺字段：{sorted(missing)}"
    assert events[-1]["type"] == "usage", "trace 必须以 usage 汇总收尾"


# ---------- FakeLLM 端到端：happy path 全链路 ----------

_GMV_PLAN = json.dumps(
    {"question_type": "single_metric",
     "steps": [{"description": "查询 GMV", "template": "gmv"}]},
    ensure_ascii=False,
)


def test_happy_path_event_sequence_full_chain(tmp_path: Path) -> None:
    # gmv 模板涉及 3 表 → 恰好 3 次 get_schema；事件序列钉死全链路顺序
    graph, llm, rec = traced(
        tmp_path,
        [_GMV_PLAN, f"```sql\n{SQL[0]}\n```", "结论：GMV 为 123.45 元 [E1]"],
    )
    final = run_and_finalize(graph, rec)
    assert final["status"] == "answered"
    events = events_of(rec)
    assert [e["type"] for e in events] == [
        "message",  # planner 规划调用
        "step",  # plan_created
        "tool_call", "tool_call", "tool_call",  # get_schema ×3（orders/order_items/products）
        "message",  # executor 生成 SQL
        "tool_call",  # run_query
        "sql_result",  # outcome=ok
        "step",  # 步骤 pending → ok
        "message",  # reporter 结论
        "conclusion",
        "usage",
    ]
    assert_event_schema(events)
    # message：节点归因 + 提示词/回复全文 + token
    assert [e["node"] for e in events if e["type"] == "message"] == [
        "planner", "executor", "reporter",
    ]
    plan_msg = events[0]
    assert plan_msg["tokens_in"] == 10 and plan_msg["tokens_out"] == 5
    assert plan_msg["response"] == _GMV_PLAN
    assert plan_msg["messages"][0]["role"] == "system"
    assert "测试问题" in plan_msg["messages"][1]["content"]  # 原问题可回放
    # tool_call：get_schema 逐表 + run_query 附行数
    schema_tools = [e for e in events if e["type"] == "tool_call" and e["tool"] == "get_schema"]
    assert [e["args"] for e in schema_tools] == [["orders"], ["order_items"], ["products"]]
    assert all(e["node"] == "executor" and e["ok"] for e in schema_tools)
    run_ev = next(e for e in events if e["type"] == "tool_call" and e["tool"] == "run_query")
    assert run_ev["ok"] is True and run_ev["row_count"] == 1
    # sql_result：与 steps_log 逐字段一致（T10 判首轮失败的数据源）
    sr = next(e for e in events if e["type"] == "sql_result")
    assert sr["outcome"] == "ok" and sr["attempt"] == 1 and sr["step_id"] == 1
    assert sr["sql"] == final["steps_log"][0]["sql"]
    assert sr["fingerprint"] == final["steps_log"][0]["fingerprint"]
    assert sr["row_count"] == 1 and isinstance(sr["ms"], (int, float))
    # conclusion：三态 + 证据
    concl = next(e for e in events if e["type"] == "conclusion")
    assert concl["status"] == "answered"
    assert concl["conclusion"].startswith("结论：GMV 为 123.45 元")
    assert concl["question"] == "测试问题"
    assert concl["evidence"][0]["id"] == "E1"
    assert concl["evidence"][0]["rows_preview"][0]["gmv"] == "123.45"
    # usage：与 LLM 返回逐字一致（FakeLLM 每次 10/5，共 3 次），费用默认关
    usage = events[-1]
    assert usage["llm_calls"] == 3 and usage["query_calls"] == 1
    assert usage["tokens_in"] == 30 and usage["tokens_out"] == 15
    assert usage["llm_calls"] == final["llm_calls"] == llm.calls
    assert usage["tokens_in"] == final["tokens_in"]
    assert usage["query_calls"] == final["query_calls"]
    assert usage["tool_calls"] == {"get_schema": 3, "run_query": 1}
    assert "cost_est" not in usage
    assert isinstance(usage["wall_ms"], (int, float))


def test_partial_run_trace_complete(tmp_path: Path) -> None:
    # T5 的 query 预算耗尽脚本：s4 被标 skipped → partial，trace 仍完整
    graph, _, rec = traced(
        tmp_path,
        [plan_json("s1", "s2", "s3", "s4"),
         SQL[3], BOOM[0], BOOM[1], SQL[4], SQL[5],
         "部分结论：已完成 3 步 [E1][E2][E3]"],
    )
    final = run_and_finalize(graph, rec)
    assert final["status"] == "partial"
    events = events_of(rec)
    assert_event_schema(events)
    sql_events = [e for e in events if e["type"] == "sql_result"]
    assert [e["outcome"] for e in sql_events] == ["ok", "error", "error", "ok", "ok"]
    assert all("boom" in (e["error"] or "") for e in sql_events[1:3])  # 错误原文回放
    steps = [e for e in events if e["type"] == "step" and e["event"] == "status_changed"]
    assert [(s["step_id"], s["to"]) for s in steps] == [
        (1, "ok"), (2, "ok"), (3, "ok"), (4, "skipped"),
    ]
    assert steps[-1]["node"] == "reporter"  # skipped 由 reporter 收尾时标记
    concl = next(e for e in events if e["type"] == "conclusion")
    assert concl["status"] == "partial" and len(concl["evidence"]) == 3
    usage = events[-1]
    assert usage["llm_calls"] == 7 and usage["query_calls"] == 5
    assert usage["llm_calls"] == final["llm_calls"] and usage["query_calls"] == final["query_calls"]


def test_failed_run_trace_complete(tmp_path: Path) -> None:
    # T5 的 LLM 预算耗尽脚本：全程无 SQL 可提取 → failed，8 次 LLM 全在 trace
    graph, llm, rec = traced(
        tmp_path,
        [plan_json("s1"), GARBAGE, GARBAGE, GARBAGE,
         plan_json("换路径重查"), GARBAGE, GARBAGE, GARBAGE],
    )
    final = run_and_finalize(graph, rec)
    assert final["status"] == "failed"
    events = events_of(rec)
    assert_event_schema(events)
    msgs = [e for e in events if e["type"] == "message"]
    assert len(msgs) == 8 == llm.calls
    sql_events = [e for e in events if e["type"] == "sql_result"]
    assert len(sql_events) == 6 and all(
        e["outcome"] == "no_sql" and e["sql"] is None for e in sql_events
    )
    plans = [e for e in events if e["type"] == "step" and e["event"] == "plan_created"]
    assert [p["node"] for p in plans] == ["planner", "replan"]  # 两次计划各落一条
    assert plans[1]["steps"][0]["description"] == "换路径重查"
    failed_steps = [e for e in events
                    if e["type"] == "step" and e.get("to") == "failed"]
    assert len(failed_steps) == 2  # 两次计划各终局失败一次
    concl = next(e for e in events if e["type"] == "conclusion")
    assert concl["status"] == "failed" and concl["evidence"] == []
    assert "预算" in concl["conclusion"]
    usage = events[-1]
    assert usage["llm_calls"] == 8 and usage["query_calls"] == 0
    assert usage["tokens_in"] == 80 and usage["tokens_out"] == 40  # 8 × (10, 5)
    assert usage["tool_calls"]["get_schema"] == 5  # schema_cache 生效：只取一轮全表


def test_empty_and_duplicate_outcomes_traced(tmp_path: Path) -> None:
    # 空集 → reflect 调 get_data_profile（tool_call 归因 reflect）→ 重试成功
    graph, _, rec = traced(tmp_path, [plan_json("s1"), EMPTY_SQL, SQL[0], "结论 [E1]"])
    final = run_and_finalize(graph, rec)
    assert final["status"] == "answered"
    events = events_of(rec)
    assert_event_schema(events)
    assert [e["outcome"] for e in events if e["type"] == "sql_result"] == ["empty", "ok"]
    profile_calls = [e for e in events
                     if e["type"] == "tool_call" and e["tool"] == "get_data_profile"]
    assert profile_calls and all(e["node"] == "reflect" for e in profile_calls)

    # 重复指纹 → 拦截未触库（无 run_query tool_call）→ 换路径成功
    dup_variant = SQL[7].lower() + " -- 同一条查询"
    graph2, _, rec2 = traced(tmp_path / "dup",
                             [plan_json("s1", "s2"), SQL[7], dup_variant, SQL[6], "结论 [E1][E2]"])
    final2 = run_and_finalize(graph2, rec2)
    assert final2["status"] == "answered"
    events2 = events_of(rec2)
    assert_event_schema(events2)
    sql_events2 = [e for e in events2 if e["type"] == "sql_result"]
    assert [e["outcome"] for e in sql_events2] == ["ok", "duplicate", "ok"]
    assert sql_events2[1]["fingerprint"] == sql_events2[0]["fingerprint"]
    assert "指纹" in (sql_events2[1]["error"] or "")
    assert events2[-1]["query_calls"] == 2  # 拦截不耗 query 预算


def test_llm_exception_message_event(tmp_path: Path) -> None:
    # planner 第一次调用即抛异常 → fatal → reporter 规则模板 → failed
    graph, _, rec = traced(tmp_path, [])
    final = run_and_finalize(graph, rec)
    assert final["status"] == "failed"
    events = events_of(rec)
    assert_event_schema(events)
    msg = events[0]
    assert msg["type"] == "message" and msg["node"] == "planner"
    assert msg["tokens_in"] is None and msg["response"] is None
    assert "超出脚本" in (msg["error"] or "")
    concl = next(e for e in events if e["type"] == "conclusion")
    assert concl["status"] == "failed"
    assert "planner LLM 调用失败" in (concl["fatal_error"] or "")
    assert events[-1]["llm_calls"] == 0  # 失败调用不计入 usage 汇总


def test_cost_model_off_by_default_on_when_given(tmp_path: Path) -> None:
    graph, _, rec = traced(
        tmp_path,
        [_GMV_PLAN, f"```sql\n{SQL[0]}\n```", "结论 [E1]"],
        cost_model=CostModel(per_1k_in=0.01, per_1k_out=0.03),
    )
    final = run_and_finalize(graph, rec)
    assert final["status"] == "answered"
    usage = events_of(rec)[-1]
    assert usage["cost_est"] == pytest.approx(30 / 1000 * 0.01 + 15 / 1000 * 0.03)


# ---------- run_analysis：落盘目录契约与异常中断 ----------


def _patch_real_deps(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("chatbi.graph.run_query", fake_query)
    monkeypatch.setattr("chatbi.graph.get_schema", fake_schema)
    monkeypatch.setattr("chatbi.graph.get_data_profile", FakeProfile())


def test_run_analysis_writes_runs_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_real_deps(monkeypatch)
    res = run_analysis(
        "上个月护肤品类 GMV 是多少",
        llm=FakeLLM([_GMV_PLAN, SQL[0], "结论 [E1]"]),
        checkpoint_path=str(tmp_path / "cp.db"),
        trace_root=str(tmp_path / "runs"),
    )
    assert res["status"] == "answered"
    trace_dir = Path(res["trace_dir"])
    assert trace_dir.parent == tmp_path / "runs"
    assert re.fullmatch(r"\d{8}T\d{6}_[0-9a-f]{6}", trace_dir.name)
    events = [json.loads(line)
              for line in (trace_dir / "trace.jsonl").read_text(encoding="utf-8").splitlines()]
    assert_event_schema(events)
    assert events[-1]["llm_calls"] == res["usage"]["llm_calls"]
    assert events[-1]["tokens_out"] == res["usage"]["tokens_out"]

    # failed 分析同样落盘，且与 answered 的 run 目录互不覆盖
    res2 = run_analysis(
        "测试问题",
        llm=FakeLLM([plan_json("s1"), BOOM[6], BOOM[7],
                     f"SELECT {31} AS v FROM orders WHERE status = 'boom31'",
                     plan_json("换路径"),
                     f"SELECT {32} AS v FROM orders WHERE status = 'boom32'",
                     f"SELECT {33} AS v FROM orders WHERE status = 'boom33'",
                     "失败报告"]),
        checkpoint_path=str(tmp_path / "cp.db"),
        trace_root=str(tmp_path / "runs"),
    )
    assert res2["status"] == "failed"
    trace_dir2 = Path(res2["trace_dir"])
    assert trace_dir2 != trace_dir and (trace_dir2 / "trace.jsonl").is_file()
    events2 = [json.loads(line)
               for line in (trace_dir2 / "trace.jsonl").read_text(encoding="utf-8").splitlines()]
    assert_event_schema(events2)
    concl2 = next(e for e in events2 if e["type"] == "conclusion")
    assert concl2["status"] == "failed" and concl2["evidence"] == []


def test_run_analysis_exception_still_finalizes_trace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_real_deps(monkeypatch)

    def boom(_: str) -> dict:
        raise RuntimeError("炸了")

    monkeypatch.setattr("chatbi.graph.init_state", boom)
    with pytest.raises(RuntimeError, match="炸了"):
        run_analysis(
            "任意问题",
            llm=FakeLLM([]),
            checkpoint_path=str(tmp_path / "cp.db"),
            trace_root=str(tmp_path / "runs"),
        )
    run_dirs = list((tmp_path / "runs").iterdir())
    assert len(run_dirs) == 1
    events = [json.loads(line)
              for line in (run_dirs[0] / "trace.jsonl").read_text(encoding="utf-8").splitlines()]
    assert_event_schema(events)
    concl = next(e for e in events if e["type"] == "conclusion")
    assert concl["status"] == "failed" and "炸了" in (concl["fatal_error"] or "")
    assert concl["question"] == "任意问题"


# ---------- integration：真实 PG + 真实 LLM 冒烟 ----------


@pytest.mark.integration
def test_smoke_real_trace_full_chain(tmp_path: Path) -> None:
    from chatbi.llm import LLMClient, LLMConfigError
    from chatbi.tools import run_query as real_run_query

    try:
        res = real_run_query("SELECT count(*) AS n FROM orders")
    except Exception:  # noqa: BLE001 —— 环境不可用一律转 skip
        pytest.skip("本机 PG 不可连（先 brew services 起 PG）")
    if not (res.ok and res.rows[0]["n"] > 1000):
        pytest.skip("库中无 mock 数据（先 uv run python -m data.mock_gen）")
    try:
        llm = LLMClient()
    except LLMConfigError:
        pytest.skip("未配置 ZHIPU_API_KEY，跳过真实冒烟")

    result = run_analysis(
        "2026年6月护肤品类的GMV是多少",
        llm=llm,
        checkpoint_path=str(tmp_path / "cp.db"),
        trace_root=str(tmp_path / "runs"),
    )
    assert result["status"] == "answered", result["conclusion"]
    events = [json.loads(line)
              for line in (Path(result["trace_dir"]) / "trace.jsonl")
              .read_text(encoding="utf-8").splitlines()]
    assert_event_schema(events)
    # 全链路：planner 起步、executor 中段、reporter 收尾；工具与 SQL 证据在册
    msg_nodes = [e["node"] for e in events if e["type"] == "message"]
    assert msg_nodes[0] == "planner"
    assert "executor" in msg_nodes and msg_nodes[-1] == "reporter"
    tools = {e["tool"] for e in events if e["type"] == "tool_call"}
    assert {"get_schema", "run_query"} <= tools
    assert any(e["type"] == "sql_result" and e["outcome"] == "ok" and e["row_count"]
               for e in events)
    concl = events[-2]
    assert concl["type"] == "conclusion" and concl["status"] == "answered"
    assert concl["evidence"], "answered 结论必须携带证据"
    # usage 与 LLM 返回一致（state 与事件流两路对账）
    usage = events[-1]
    assert usage["llm_calls"] == result["usage"]["llm_calls"]
    assert usage["query_calls"] == result["usage"]["query_calls"]
    assert usage["tokens_in"] == result["usage"]["tokens_in"]
    assert usage["tokens_out"] == result["usage"]["tokens_out"]
