"""web：FastAPI Web 壳（T8 / SPEC §6.4）——四个 API 路由 + 原生 JS 单页托管。

- ``create_app(trace_root="runs")``：应用工厂；路由闭包引用模块级 ``analyze`` /
  ``get_schema``，测试直接 monkeypatch 这两个名字（ASGI 层 mock，不触真实图 / 库）；
- ``POST /api/analyze``：``{question}`` → 同步 30~60s → SPEC §7 JSON（复用
  ``AnalysisResult.to_dict()``，含 trace_dir / errors 附注）；请求体校验：非空字符串
  （strip 后非空）、长度 ≤2000，非法一律 422；analyze 的内部异常已被 T7 捕获转为
  failed，HTTP 恒 200、三态由 ``body.status`` 承载；
- ``GET /api/traces``：runs/ 汇总列表（run_id 降序 = 新者在前；limit 默认 50），
  每条摘取末条 conclusion / usage 事件；半写坏行（崩溃现场）跳过，无 conclusion
  的中断现场也如实列出（status 为 null）；
- ``GET /api/traces/{run_id}``：全量事件回放（时间线用）；run_id 仅允许
  ``[0-9A-Za-z_-]+``（无路径成分，防穿越），未命中或非法一律 404；
- ``GET /api/schema``：复用 ``get_schema``（``table`` 参数可选）；表不在白名单 →
  400，环境问题（缺 PG_DSN_RO / 库不可达）→ 503；
- ``GET /api/health``：存活探针；
- 单页挂 ``/``（StaticFiles html=True；路由先注册故 /api/* 优先）。绑定 127.0.0.1
  属启动事项：``python -m chatbi.web``（默认 host 127.0.0.1，SPEC 非目标：单用户
  无鉴权）。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from chatbi.api import analyze
from chatbi.tools import get_schema
from chatbi.tracing import TRACE_FILENAME

_STATIC_DIR = Path(__file__).parent / "static"
_RUN_ID_RE = re.compile(r"[0-9A-Za-z_\-]+")
MAX_QUESTION_LEN = 2000
DEFAULT_TRACE_LIMIT = 50
_USAGE_KEYS = ("llm_calls", "query_calls", "tokens_in", "tokens_out", "wall_ms")


class AnalyzeRequest(BaseModel):
    """POST /api/analyze 请求体：question 必填、strip 后非空、≤2000 字符。"""

    question: str = Field(min_length=1, max_length=MAX_QUESTION_LEN)

    @field_validator("question")
    @classmethod
    def _strip_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("question 不能为空白（strip 后须非空）")
        return value


def _read_events(path: Path) -> list[dict]:
    """逐行解析 trace.jsonl；半写坏行（崩溃现场）跳过、保留其余。"""
    events: list[dict] = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return events


def _last_event(events: list[dict], type_: str) -> dict | None:
    for event in reversed(events):
        if event.get("type") == type_:
            return event
    return None


def _trace_summary(run_dir: Path) -> dict:
    """单条历史摘要：末条 conclusion（问句 / 三态）+ 末条 usage（用量 / 耗时）。"""
    events = _read_events(run_dir / TRACE_FILENAME)
    conclusion = _last_event(events, "conclusion")
    usage = _last_event(events, "usage")
    anchor = conclusion or (events[0] if events else None)
    return {
        "run_id": run_dir.name,
        "at": anchor.get("at") if anchor else None,
        "status": conclusion.get("status") if conclusion else None,
        "question": conclusion.get("question") if conclusion else None,
        "question_type": conclusion.get("question_type") if conclusion else None,
        **{k: (usage.get(k) if usage else None) for k in _USAGE_KEYS},
    }


def create_app(*, trace_root: str | Path = "runs") -> FastAPI:
    """Web 壳应用；trace_root 与 RunConfig 默认一致（相对 CWD），测试可重定向。"""
    root = Path(trace_root)
    app = FastAPI(title="ChatBI Web", version="0.1.0")

    @app.middleware("http")
    async def no_cache_static(request: Request, call_next: Any) -> Response:
        """静态单页资产禁用启发式缓存（no-cache 走 ETag/Last-Modified 重验证）：本地
        迭代频繁，改 CSS/JS 后刷新即生效，不出现陈旧页面。"""
        response = await call_next(request)
        if request.url.path in ("/", "/style.css", "/app.js"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/api/analyze")
    def post_analyze(req: AnalyzeRequest) -> dict[str, Any]:
        result = analyze(req.question)  # 内部异常已转 failed（T7），HTTP 恒 200
        return result.to_dict()

    @app.get("/api/traces")
    def list_traces(limit: int = Query(DEFAULT_TRACE_LIMIT, ge=1, le=200)) -> dict:
        traces: list[dict] = []
        if root.is_dir():
            run_dirs = [d for d in root.iterdir() if (d / TRACE_FILENAME).is_file()]
            newest = sorted(run_dirs, key=lambda d: d.name, reverse=True)[:limit]
            traces = [_trace_summary(d) for d in newest]
        return {"traces": traces}

    @app.get("/api/traces/{run_id}")
    def trace_detail(run_id: str) -> dict:
        if not _RUN_ID_RE.fullmatch(run_id):
            raise HTTPException(status_code=404, detail="trace 不存在")
        path = root / run_id / TRACE_FILENAME
        if not path.is_file():
            raise HTTPException(status_code=404, detail="trace 不存在")
        events = _read_events(path)
        conclusion = _last_event(events, "conclusion")
        usage = _last_event(events, "usage")
        return {
            "run_id": run_id,
            "question": conclusion.get("question") if conclusion else None,
            "question_type": conclusion.get("question_type") if conclusion else None,
            "status": conclusion.get("status") if conclusion else None,
            "conclusion": conclusion.get("conclusion") if conclusion else None,
            "evidence": (conclusion or {}).get("evidence") or [],
            "errors": (conclusion or {}).get("errors") or [],
            "usage": {k: usage.get(k) for k in _USAGE_KEYS} if usage else None,
            "events": events,
        }

    @app.get("/api/schema")
    def schema(table: str | None = Query(None)) -> dict:
        try:
            return get_schema(table)
        except ValueError as err:  # 表不在白名单：调用方参数问题
            raise HTTPException(status_code=400, detail=str(err)) from err
        except Exception as err:  # noqa: BLE001 —— 环境/连接问题统一 503
            raise HTTPException(status_code=503, detail=f"数据库不可用：{err}") from err

    app.mount("/", StaticFiles(directory=_STATIC_DIR, html=True), name="web")
    return app


app = create_app()  # uvicorn chatbi.web.app:app / python -m chatbi.web 共用
