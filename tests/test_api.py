"""T7 测试：核心库 API + CLI 薄壳——AnalysisResult 契约、RunConfig 覆写、异常转 failed。

先测后写（T7 ⑥）：本用例集即验收清单——
- 三态分别构造触发：answered（单步 happy）/ partial（query 预算耗尽）/ failed（LLM 预算耗尽）；
- 契约映射：run_analysis dict → AnalysisResult（evidence / steps=steps_log / usage /
  trace_dir: Path），frozen + to_dict JSON 化；
- RunConfig：预算覆写（maxSteps 截断计划）、模型覆写（LLMClient(model=…) 构造透传）、
  trace 开关（关闭既不落盘也不返回目录）；
- 任何内部异常 → failed 结果且不向上抛（图执行炸 / 模型配置缺失两条路都验）；
- ask CLI：三态渲染 + 退出码（failed → 1，其余 0）；
- integration（真实 PG + 真实 LLM，缺失自动 skip）：analyze() 一问冒烟。
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any

import pytest
from test_graph import (
    BOOM,
    GARBAGE,
    SQL,
    FakeLLM,
    FakeProfile,
    fake_query,
    fake_schema,
    plan_json,
)

from chatbi.api import AnalysisResult, RunConfig, analyze
from chatbi.llm import LLMConfigError, LLMUsage
from chatbi.state import Budgets

# ---------- 助手：把 graph 的真实依赖换成 fakes（同 test_tracing 套路） ----------


def _patch_deps(monkeypatch: pytest.MonkeyPatch, script: list[str]) -> FakeLLM:
    monkeypatch.setattr("chatbi.graph.run_query", fake_query)
    monkeypatch.setattr("chatbi.graph.get_schema", fake_schema)
    monkeypatch.setattr("chatbi.graph.get_data_profile", FakeProfile())
    llm = FakeLLM(script)
    monkeypatch.setattr("chatbi.graph.get_client", lambda: llm)
    return llm


def _cfg(tmp_path: Path, **over: Any) -> RunConfig:
    """默认把 trace / checkpoint 重定向到 tmp_path，不污染仓库 runs/。"""
    return RunConfig(
        trace_root=str(tmp_path / "runs"),
        checkpoint_path=str(tmp_path / "cp.db"),
        **over,
    )


# ---------- 三态分别构造触发 ----------


def test_analyze_answered_contract(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    llm = _patch_deps(
        monkeypatch,
        [plan_json("查询 GMV"), f"```sql\n{SQL[0]}\n```", "结论：GMV 为 123.45 元 [E1]"],
    )
    result = analyze("上个月护肤品类 GMV 是多少", config=_cfg(tmp_path))
    assert isinstance(result, AnalysisResult)
    assert result.status == "answered"
    assert result.conclusion.startswith("结论：GMV 为 123.45 元")
    assert len(result.evidence) == 1
    ev = result.evidence[0]
    assert {"sql", "row_count", "rows_preview", "ms"} <= set(ev)  # SPEC §7 证据字段
    assert result.steps[0]["outcome"] == "ok"  # steps = steps_log（生成尝试审计）
    assert result.usage == {"llm_calls": 3, "query_calls": 1, "tokens_in": 30, "tokens_out": 15}
    assert isinstance(result.trace_dir, Path)
    assert result.trace_dir.parent == tmp_path / "runs"
    assert (result.trace_dir / "trace.jsonl").is_file()
    assert llm.calls == 3


def test_analyze_partial_on_query_budget(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # T5 的预算耗尽脚本：s4 未执行 → partial
    _patch_deps(
        monkeypatch,
        [plan_json("s1", "s2", "s3", "s4"),
         SQL[3], BOOM[0], BOOM[1], SQL[4], SQL[5],
         "部分结论：已完成 3 步 [E1][E2][E3]"],
    )
    result = analyze("多步问题", config=_cfg(tmp_path))
    assert result.status == "partial"
    assert len(result.evidence) == 3
    assert [s["outcome"] for s in result.steps] == ["ok", "error", "error", "ok", "ok"]
    assert result.usage["query_calls"] == 5 and result.usage["llm_calls"] == 7


def test_analyze_failed_on_llm_budget(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_deps(
        monkeypatch,
        [plan_json("s1"), GARBAGE, GARBAGE, GARBAGE,
         plan_json("换路径重查"), GARBAGE, GARBAGE, GARBAGE],
    )
    result = analyze("无解问题", config=_cfg(tmp_path))
    assert result.status == "failed"
    assert result.evidence == [] and result.steps
    assert all(s["outcome"] == "no_sql" for s in result.steps)
    assert "预算" in result.conclusion  # 规则模板结论如实说明
    assert result.usage["llm_calls"] == 8


# ---------- 契约：不可变与 JSON 化 ----------


def test_result_immutable_and_to_dict(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_deps(monkeypatch, [plan_json("s1"), SQL[0], "结论 [E1]"])
    result = analyze("q", config=_cfg(tmp_path))
    with pytest.raises(FrozenInstanceError):
        result.status = "answered"  # type: ignore[misc]
    d = result.to_dict()
    assert set(d) == {"status", "conclusion", "evidence", "steps", "usage", "trace_dir", "errors"}
    assert d["trace_dir"] == str(result.trace_dir)
    import json

    json.dumps(d, ensure_ascii=False)  # 全量可序列化（Web T8 直接复用）


# ---------- RunConfig 覆写 ----------


def test_runconfig_budget_override_truncates_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_deps(monkeypatch, [plan_json(*[f"s{i}" for i in range(7)]), SQL[6], "结论 [E1]"])
    result = analyze(
        "七步问题",
        config=_cfg(
            tmp_path,
            budgets=Budgets(max_steps=1, max_llm_calls=8, max_query_calls=5, max_corrections=2),
        ),
    )
    assert result.status == "answered"
    assert len(result.evidence) == 1  # 计划被 maxSteps=1 截断
    assert result.usage["llm_calls"] == 3


def test_runconfig_trace_off(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_deps(monkeypatch, [plan_json("s1"), SQL[0], "结论 [E1]"])
    result = analyze("q", config=_cfg(tmp_path, trace=False))
    assert result.status == "answered"
    assert result.trace_dir is None
    assert not (tmp_path / "runs").exists()  # 关闭即完全不落盘


class _RecordingClient:
    """记录构造参数与调用次数的 LLMClient 替身；固定回放一条合法 SQL。"""

    made: list[_RecordingClient] = []

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.calls = 0
        _RecordingClient.made.append(self)

    def chat(self, messages: Any, **kwargs: Any) -> tuple[str, LLMUsage]:
        self.calls += 1
        return f"```sql\n{SQL[0]}\n```", LLMUsage(tokens_in=1, tokens_out=1)


def test_runconfig_model_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_deps(monkeypatch, [])  # 工具换 fakes；LLM 应由 api 层构造注入，不走 get_client
    monkeypatch.setattr("chatbi.api.LLMClient", _RecordingClient)
    result = analyze("模型覆写问题", config=_cfg(tmp_path, model="glm-unit-test"))
    client = _RecordingClient.made[-1]
    assert client.kwargs.get("model") == "glm-unit-test"
    assert client.calls == 3  # 同一实例贯穿 planner / executor / reporter
    assert result.status == "answered" and result.usage["llm_calls"] == 3


# ---------- 异常转 failed：不向上抛 ----------


def test_analyze_converts_internal_exception_to_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_deps(monkeypatch, [])

    def boom(_: str) -> dict:
        raise RuntimeError("炸了")

    monkeypatch.setattr("chatbi.graph.init_state", boom)
    result = analyze("会炸的问题", config=_cfg(tmp_path))  # 不抛、返回 failed
    assert result.status == "failed"
    assert "炸了" in result.conclusion
    assert result.errors and "炸了" in result.errors[0]
    assert result.evidence == [] and result.steps == []
    assert result.usage == {"llm_calls": 0, "query_calls": 0, "tokens_in": 0, "tokens_out": 0}
    assert result.trace_dir is None


def test_analyze_config_error_becomes_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_deps(monkeypatch, [])

    class _NoKey:
        def __init__(self, **kwargs: Any) -> None:
            raise LLMConfigError("缺少 ZHIPU_API_KEY（检查 .env，模板见 .env.example）")

    monkeypatch.setattr("chatbi.api.LLMClient", _NoKey)
    result = analyze("无 key 问题", config=_cfg(tmp_path, model="glm-x"))
    assert result.status == "failed"
    assert "ZHIPU_API_KEY" in result.conclusion


# ---------- CLI 薄壳：三态渲染与退出码 ----------


def _fake_result(status: str) -> AnalysisResult:
    return AnalysisResult(
        status=status,
        conclusion="GMV 为 100 元" if status != "failed" else "分析失败：无可用证据",
        evidence=[{"id": "E1", "description": "查 GMV", "sql": "SELECT 1",
                   "row_count": 1, "rows_preview": [{"gmv": "100"}], "ms": 1.2}],
        steps=[{"step_id": 1, "description": "查 GMV", "attempt": 1,
                "outcome": "ok" if status != "failed" else "no_sql", "error": None}],
        usage={"llm_calls": 3, "query_calls": 1, "tokens_in": 30, "tokens_out": 15},
        trace_dir=None,
    )


def test_ask_cli_renders_and_exit_codes(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from chatbi import ask

    monkeypatch.setattr("chatbi.ask.analyze", lambda q, *, config=None: _fake_result("answered"))
    assert ask.main(["测试问题"]) == 0
    out = capsys.readouterr().out
    assert "GMV 为 100 元" in out and "SELECT 1" in out and "测试问题" in out

    monkeypatch.setattr("chatbi.ask.analyze", lambda q, *, config=None: _fake_result("partial"))
    assert ask.main(["部分问题"]) == 0

    monkeypatch.setattr("chatbi.ask.analyze", lambda q, *, config=None: _fake_result("failed"))
    assert ask.main(["失败问题"]) == 1
    out = capsys.readouterr().out
    assert "分析失败" in out


# ---------- integration：真实 PG + 真实 LLM 冒烟一问 ----------


@pytest.mark.integration
def test_integration_analyze_one_question(tmp_path: Path) -> None:
    from chatbi.llm import LLMClient
    from chatbi.tools import run_query as real_run_query

    try:
        res = real_run_query("SELECT count(*) AS n FROM orders")
    except Exception:  # noqa: BLE001 —— 环境不可用一律转 skip
        pytest.skip("本机 PG 不可连（先 brew services 起 PG）")
    if not (res.ok and res.rows[0]["n"] > 1000):
        pytest.skip("库中无 mock 数据（先 uv run python -m data.mock_gen）")
    try:
        LLMClient()
    except LLMConfigError:
        pytest.skip("未配置 ZHIPU_API_KEY，跳过真实冒烟")

    result = analyze("2026年6月护肤品类的GMV是多少", config=_cfg(tmp_path))
    assert result.status == "answered", result.conclusion
    assert result.evidence and result.evidence[0]["sql"]
    assert result.trace_dir is not None
    assert (result.trace_dir / "trace.jsonl").is_file()
    assert 1 <= result.usage["llm_calls"] <= 8  # 真实跑也要守住预算三件套
    assert 1 <= result.usage["query_calls"] <= 5
