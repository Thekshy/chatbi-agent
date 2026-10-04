"""T8 测试：Web 壳——四路由契约 + 静态托管 + mock analyze 三态（ASGI，不触真实图/库）。

先测后写（T8⑥）：本用例集即验收清单——
- ``/api/health``：存活探针；
- ``/api/analyze``：mock analyze 三态各一，响应字段与 SPEC §7 一致（to_dict 超集
  含 trace_dir / errors）；请求体校验（缺字段 / strip 后空白 / 超长 / 类型错 /
  非 JSON body）→ 422；question strip 后透传给 analyze；HTTP 恒 200（三态在 body）；
- ``/api/traces``：摘要列表（run_id 降序新者在前、limit 生效；runs/ 不存在 → 空表；
  半写坏行跳过；无 trace.jsonl 的目录与 checkpoints.db 不入列）；
- ``/api/traces/{run_id}``：全量事件 + conclusion / usage 摘取；不存在 / 非法
  （点号、路径穿越形态）一律 404；
- ``/api/schema``：get_schema 复用与 table 参数透传；表不在白名单 → 400；
  环境问题（ToolsConfigError / 连接失败）→ 503；
- 静态单页：``/`` 出 HTML、``/app.js`` / ``/style.css`` 可取（原生 JS，无框架）；
- 模块级 ``app`` 单例存在（``uvicorn chatbi.web.app:app`` 的挂载点）；
- integration（本机 PG 缺失自动 skip）：真实 get_schema 经 ``/api/schema`` 透传。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from chatbi.api import AnalysisResult
from chatbi.tools import ToolsConfigError
from chatbi.web.app import MAX_QUESTION_LEN, create_app
from chatbi.web.app import app as web_singleton

# ---------- 助手 ----------


def _client(**kwargs: Any) -> TestClient:
    return TestClient(create_app(**kwargs))


def _fake_result(status: str) -> AnalysisResult:
    return AnalysisResult(
        status=status,
        conclusion="GMV 为 100 元" if status != "failed" else "分析失败：无可用证据",
        evidence=[{"id": "E1", "description": "查 GMV", "sql": "SELECT 1",
                   "row_count": 1, "rows_preview": [{"gmv": "100"}], "ms": 1.2}],
        steps=[{"step_id": 1, "description": "查 GMV", "attempt": 1,
                "outcome": "ok" if status != "failed" else "no_sql", "error": None}],
        usage={"llm_calls": 3, "query_calls": 1, "tokens_in": 30, "tokens_out": 15},
        trace_dir=Path("/tmp/runs/20260916T090000_ab12cd") if status != "failed" else None,
        errors=["非致命一例"] if status == "partial" else [],
    )


def _event(seq: int, etype: str, **fields: Any) -> dict:
    return {"seq": seq, "ts": 1000.0 + seq, "at": "2026-09-16T09:00:00.000",
            "type": etype, **fields}


def _write_trace(
    root: Path,
    run_id: str,
    *,
    status: str = "answered",
    question: str = "上月护肤 GMV 是多少",
    bad_line: bool = False,
) -> None:
    events = [
        _event(1, "message", node="planner", messages=[], response="规划完成",
               ms=800.0, tokens_in=100, tokens_out=20, error=None),
        _event(2, "sql_result", node="executor", step=1, step_id=1, attempt=1,
               outcome="ok", sql="SELECT 1", fingerprint="abc", row_count=1, ms=5.0,
               error=None),
        _event(3, "conclusion", node="reporter", status=status, conclusion="GMV 为 100 元",
               question=question, question_type="single_metric",
               evidence=[{"id": "E1", "sql": "SELECT 1", "row_count": 1,
                          "rows_preview": [{"gmv": "100"}], "ms": 5.0}],
               errors=[], fatal_error=None),
        _event(4, "usage", node=None, llm_calls=3, query_calls=1, tokens_in=30,
               tokens_out=15, tool_calls={"run_query": 1}, wall_ms=1200.5),
    ]
    lines = [json.dumps(e, ensure_ascii=False) for e in events]
    if bad_line:
        lines.insert(1, '{"seq": 2, "type": "半写坏行')  # 崩溃现场的残行
    run_dir = root / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "trace.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------- /api/health ----------


def test_health() -> None:
    with _client() as client:
        resp = client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


# ---------- /api/analyze：三态 + 契约 + 校验 ----------


@pytest.mark.parametrize("status", ["answered", "partial", "failed"])
def test_analyze_route_three_states(status: str, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    def fake_analyze(question: str, *, config: Any = None) -> AnalysisResult:
        seen.append(question)
        return _fake_result(status)

    monkeypatch.setattr("chatbi.web.app.analyze", fake_analyze)
    with _client() as client:
        resp = client.post("/api/analyze", json={"question": "上月护肤 GMV"})
    assert resp.status_code == 200  # 三态由 body.status 承载，HTTP 恒 200
    body = resp.json()
    assert body["status"] == status
    assert {"status", "conclusion", "evidence", "steps", "usage"} <= set(body)  # SPEC §7
    assert body["evidence"][0]["sql"] == "SELECT 1"
    assert body["usage"] == {"llm_calls": 3, "query_calls": 1, "tokens_in": 30, "tokens_out": 15}
    assert seen == ["上月护肤 GMV"]


def test_analyze_strips_question_before_passthrough(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[str] = []
    monkeypatch.setattr("chatbi.web.app.analyze",
                        lambda q, *, config=None: seen.append(q) or _fake_result("answered"))
    with _client() as client:
        resp = client.post("/api/analyze", json={"question": "  上月 GMV  \n"})
    assert resp.status_code == 200
    assert seen == ["上月 GMV"]  # strip 后透传


def test_analyze_validates_request_body() -> None:
    with _client() as client:
        missing = client.post("/api/analyze", json={})
        blank = client.post("/api/analyze", json={"question": "   "})
        too_long = client.post("/api/analyze", json={"question": "问" * (MAX_QUESTION_LEN + 1)})
        wrong_type = client.post("/api/analyze", json={"question": 123})
        not_json = client.post(
            "/api/analyze", content="不是 JSON", headers={"Content-Type": "application/json"}
        )
    assert missing.status_code == 422
    assert blank.status_code == 422  # strip 后空白同缺字段
    assert too_long.status_code == 422
    assert wrong_type.status_code == 422
    assert not_json.status_code == 422


# ---------- /api/traces：列表 ----------


def test_traces_list_newest_first_with_summary(tmp_path: Path) -> None:
    _write_trace(tmp_path, "20260916T090000_aaa", status="failed", question="第一问")
    _write_trace(tmp_path, "20260916T090101_bbb", status="answered", question="第二问")
    with _client(trace_root=tmp_path) as client:
        resp = client.get("/api/traces")
    assert resp.status_code == 200
    traces = resp.json()["traces"]
    assert [t["run_id"] for t in traces] == ["20260916T090101_bbb", "20260916T090000_aaa"]
    newest = traces[0]
    assert newest["status"] == "answered" and newest["question"] == "第二问"
    assert newest["question_type"] == "single_metric"
    assert newest["llm_calls"] == 3 and newest["query_calls"] == 1
    assert newest["tokens_in"] == 30 and newest["tokens_out"] == 15
    assert newest["wall_ms"] == 1200.5
    assert newest["at"]  # 取自 conclusion 事件


def test_traces_list_limit_and_noise_filtering(tmp_path: Path) -> None:
    for i in range(3):
        _write_trace(tmp_path, f"20260916T09000{i}_x{i}")
    (tmp_path / "20260916T090099_empty").mkdir()  # 无 trace.jsonl 的目录
    (tmp_path / "checkpoints.db").write_text("")  # runs/ 下的散文件
    with _client(trace_root=tmp_path) as client:
        resp = client.get("/api/traces", params={"limit": 2})
    traces = resp.json()["traces"]
    assert [t["run_id"] for t in traces] == ["20260916T090002_x2", "20260916T090001_x1"]


def test_traces_list_empty_when_root_missing(tmp_path: Path) -> None:
    with _client(trace_root=tmp_path / "nope") as client:
        resp = client.get("/api/traces")
    assert resp.status_code == 200
    assert resp.json() == {"traces": []}


def test_traces_list_skips_malformed_lines(tmp_path: Path) -> None:
    _write_trace(tmp_path, "20260916T090000_aaa", bad_line=True)
    with _client(trace_root=tmp_path) as client:
        resp = client.get("/api/traces")
    traces = resp.json()["traces"]
    assert len(traces) == 1
    assert traces[0]["status"] == "answered"  # 坏行不影响结论摘取


# ---------- /api/traces/{run_id}：详情 ----------


def test_trace_detail_returns_full_events(tmp_path: Path) -> None:
    _write_trace(tmp_path, "20260916T090000_aaa", bad_line=True)
    with _client(trace_root=tmp_path) as client:
        resp = client.get("/api/traces/20260916T090000_aaa")
    assert resp.status_code == 200
    body = resp.json()
    assert body["run_id"] == "20260916T090000_aaa"
    assert body["status"] == "answered" and body["question"] == "上月护肤 GMV 是多少"
    assert body["conclusion"] == "GMV 为 100 元"
    assert body["evidence"][0]["id"] == "E1"
    assert body["usage"]["llm_calls"] == 3 and body["usage"]["wall_ms"] == 1200.5
    types = [e["type"] for e in body["events"]]
    assert types == ["message", "sql_result", "conclusion", "usage"]  # 坏行被跳过


@pytest.mark.parametrize("run_id", ["不存在", "..", "a.b", "%2e%2e", "-"])
def test_trace_detail_404_on_unknown_or_invalid(run_id: str, tmp_path: Path) -> None:
    _write_trace(tmp_path, "20260916T090000_aaa")
    with _client(trace_root=tmp_path) as client:
        resp = client.get(f"/api/traces/{run_id}")
    assert resp.status_code == 404  # 非法 id 与未命中同样 404，不泄露路径信息


# ---------- /api/schema：get_schema 复用 ----------


def test_schema_passthrough_with_table_param(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str | None] = []
    fixture = {"tables": [{"table": "products", "row_count": 2000, "columns": [],
                           "enum_values": {}, "sample_rows": []}]}

    def fake_schema(table: str | None = None) -> dict:
        seen.append(table)
        return fixture

    monkeypatch.setattr("chatbi.web.app.get_schema", fake_schema)
    with _client() as client:
        all_resp = client.get("/api/schema")
        one_resp = client.get("/api/schema", params={"table": "products"})
    assert all_resp.status_code == 200 and all_resp.json() == fixture
    assert one_resp.status_code == 200 and one_resp.json() == fixture
    assert seen == [None, "products"]  # table 参数透传


def test_schema_error_mapping(monkeypatch: pytest.MonkeyPatch) -> None:
    def bad_table(table: str | None = None) -> dict:
        raise ValueError("表 ['nope'] 不在白名单内")

    def no_env(table: str | None = None) -> dict:
        raise ToolsConfigError("缺少 PG_DSN_RO")

    monkeypatch.setattr("chatbi.web.app.get_schema", bad_table)
    with _client() as client:
        assert client.get("/api/schema", params={"table": "nope"}).status_code == 400
    monkeypatch.setattr("chatbi.web.app.get_schema", no_env)
    with _client() as client:
        resp = client.get("/api/schema")
    assert resp.status_code == 503
    assert "PG_DSN_RO" in resp.json()["detail"]


# ---------- 静态单页与模块单例 ----------


def test_static_page_served() -> None:
    with _client() as client:
        index = client.get("/")
        js = client.get("/app.js")
        css = client.get("/style.css")
    assert index.status_code == 200 and "text/html" in index.headers["content-type"]
    assert "ChatBI" in index.text and "提问" in index.text
    assert js.status_code == 200 and "fetch(" in js.text  # 原生 JS + fetch，无框架
    assert css.status_code == 200


def test_module_level_app_singleton_for_uvicorn() -> None:
    from fastapi import FastAPI as _FastAPI

    assert isinstance(web_singleton, _FastAPI)


# ---------- integration：真实 get_schema 透传（PG 缺失自动 skip） ----------


@pytest.mark.integration
def test_integration_schema_real_pg() -> None:
    from chatbi.tools import run_query as real_run_query

    try:
        res = real_run_query("SELECT count(*) AS n FROM products")
    except Exception:  # noqa: BLE001 —— 环境不可用一律转 skip
        pytest.skip("本机 PG 不可连（先 brew services 起 PG）")
    if not (res.ok and res.rows[0]["n"] > 0):
        pytest.skip("库中无 mock 数据（先 uv run python -m data.mock_gen）")

    with _client() as client:
        resp = client.get("/api/schema", params={"table": "products"})
    assert resp.status_code == 200
    table = resp.json()["tables"][0]
    assert table["table"] == "products"
    assert table["row_count"] == res.rows[0]["n"]
    assert any(c["name"] == "brand" for c in table["columns"])
