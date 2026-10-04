"""tracing：JSONL trace 落盘（T6 / SPEC §Q10、§8.1；T10 纠错成功率据此判读）。

每次分析一个 ``runs/<run_id>/trace.jsonl``，事件即时写盘（逐行 flush）——
进程中途崩溃也保留已写事件；answered / partial / failed / 异常中断同样完整落盘
（异常路径走 ``finalize(None, error=...)``，终态记 failed）。

事件 schema（每行一个 JSON 对象；公共字段 ``seq``（行序，1 起）/ ``ts``（epoch 秒）/
``at``（本地时刻），其余字段按 type 给出）：

- ``message``     LLM 一次调用：``node`` ``messages``（提示词全文）``response``
                  （回复全文）``ms`` ``tokens_in`` ``tokens_out``；调用抛异常时
                  ``error`` 置位、response/tokens 为 null（且不计入 usage 汇总）。
- ``tool_call``   工具调用（get_schema / get_data_profile / run_query）：
                  ``node`` ``tool`` ``args`` ``ms`` ``ok``，失败另附 ``error``；
                  run_query 的 QueryResult 另附 ``row_count``。
- ``sql_result``  每次 SQL 生成尝试的判定（executor steps_log 差分采集）：
                  ``node`` ``step_id`` ``attempt`` ``outcome``
                  （ok/error/invalid/duplicate/empty/no_sql）``sql`` ``fingerprint``
                  ``row_count`` ``ms`` ``error``。T10 判「首轮 SQL 失败」看这里。
- ``step``        计划与步骤生命周期：planner/replan 的 ``plan_created``（含全部
                  步骤与 question_type）、``status_changed``（ok/failed/skipped
                  变迁，含 from/to）。
- ``conclusion``  终态：``status`` 三态、``conclusion`` 文本、``question``
                  ``question_type`` ``evidence``（含 SQL 与行数）``errors``
                  ``fatal_error``。
- ``usage``       末行汇总：``llm_calls`` ``query_calls`` ``tokens_in/out``
                  ``tool_calls``（分工具计数）``wall_ms``；费用估算默认关
                  （SPEC §7：订阅制无按 token 单价），显式传 ``cost_model``
                  才输出 ``cost_est``。

采集接线（graph.py 最小侵入挂回调）：``build_graph(trace=recorder)`` 用
``wrap_llm`` / ``wrap_tool`` / ``wrap_node`` 在装配时包一层，节点函数体零改动；
``wrap_node`` 在节点返回后对 state→updates 做差分（steps_log 新增 → sql_result、
plan 状态变迁 / 新计划 → step），``wrap_llm`` / ``wrap_tool`` 在调用现场计时取数。
usage 汇总由事件流自行累计（与 state 计数同口径：只计成功返回的调用），
finalize(None) 异常路径也能出全量 usage。
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from chatbi.state import STATUS_FAILED, GraphState

TRACE_FILENAME = "trace.jsonl"


@dataclass(frozen=True)
class CostModel:
    """费用估算开关：默认不启用；显式传入才在 usage 事件输出 cost_est（元/千 token）。"""

    per_1k_in: float
    per_1k_out: float


def default_run_id() -> str:
    """``<YYYYmmddTHHMMSS>_<6位hex>``：秒级时间戳保序，后缀防同秒碰撞。"""
    return f"{datetime.now():%Y%m%dT%H%M%S}_{uuid.uuid4().hex[:6]}"


class _TracedLLM:
    """LLM 代理：chat 前后计时并落 message 事件，调用语义原样透传。"""

    def __init__(self, inner: Any, recorder: TraceRecorder) -> None:
        self._inner = inner
        self._recorder = recorder

    def chat(
        self, messages: Sequence[Mapping[str, Any]], **kwargs: Any
    ) -> tuple[str, Any]:
        t0 = time.perf_counter()
        try:
            content, usage = self._inner.chat(messages, **kwargs)
        except Exception as err:  # noqa: BLE001 —— 记完事件原样上抛，节点自行降级
            self._recorder.message(
                messages, None, None, time.perf_counter() - t0, error=err
            )
            raise
        self._recorder.message(messages, content, usage, time.perf_counter() - t0)
        return content, usage


class TraceRecorder:
    """一次分析的 trace 记录器：装配期包回调，运行期逐事件写 JSONL，终期 finalize。"""

    def __init__(
        self,
        trace_root: str | Path = "runs",
        *,
        question: str = "",
        run_id: str | None = None,
        cost_model: CostModel | None = None,
    ) -> None:
        self.question = question
        self.run_id = run_id or default_run_id()
        self.cost_model = cost_model
        self.dir = Path(trace_root) / self.run_id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / TRACE_FILENAME
        self._fh = self.path.open("a", encoding="utf-8")
        self._seq = 0
        self._t0 = time.perf_counter()
        self._node: str | None = None
        self._closed = False
        # usage 汇总（事件流累计，与 state 计数同口径：只计成功返回的调用）
        self.llm_calls = 0
        self.tokens_in = 0
        self.tokens_out = 0
        self.tool_calls: dict[str, int] = {}

    # ---------- 事件写入 ----------

    def emit(self, event: dict) -> None:
        if self._closed:
            return
        self._seq += 1
        record = {
            "seq": self._seq,
            "ts": round(time.time(), 3),
            "at": datetime.now().isoformat(timespec="milliseconds"),
            **event,
        }
        self._fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        self._fh.flush()

    def message(
        self,
        messages: Sequence[Mapping[str, Any]],
        response: str | None,
        usage: Any,
        ms: float,
        *,
        error: BaseException | None = None,
    ) -> None:
        if usage is not None:  # 失败调用不计入汇总（与 state.llm_calls 口径一致）
            self.llm_calls += 1
            self.tokens_in += usage.tokens_in
            self.tokens_out += usage.tokens_out
        self.emit(
            {
                "type": "message",
                "node": self._node,
                "messages": [dict(m) for m in messages],
                "response": response,
                "ms": round(ms, 3),
                "tokens_in": usage.tokens_in if usage else None,
                "tokens_out": usage.tokens_out if usage else None,
                "error": str(error) if error else None,
            }
        )

    def tool_call(
        self,
        tool: str,
        args: tuple,
        kwargs: dict,
        ms: float,
        *,
        ok: bool = True,
        error: BaseException | None = None,
        row_count: int | None = None,
    ) -> None:
        if ok:  # 抛异常的调用不计入汇总（与 state.query_calls 口径一致）
            self.tool_calls[tool] = self.tool_calls.get(tool, 0) + 1
        event: dict = {
            "type": "tool_call",
            "node": self._node,
            "tool": tool,
            "args": list(args),
            "ms": round(ms, 3),
            "ok": ok,
            "error": str(error) if error else None,
        }
        if row_count is not None:
            event["row_count"] = row_count
        self.emit(event)

    # ---------- 装配期回调包装 ----------

    def wrap_llm(self, llm: Any) -> _TracedLLM:
        return _TracedLLM(llm, self)

    def wrap_tool(self, tool: str, fn: Callable) -> Callable:
        recorder = self

        def wrapped(*args: Any, **kwargs: Any) -> Any:
            t0 = time.perf_counter()
            try:
                out = fn(*args, **kwargs)
            except Exception as err:  # noqa: BLE001 —— 记完事件原样上抛
                recorder.tool_call(
                    tool, args, kwargs, time.perf_counter() - t0, ok=False, error=err
                )
                raise
            row_count = getattr(out, "row_count", None)  # QueryResult 附带行数
            recorder.tool_call(
                tool, args, kwargs, time.perf_counter() - t0, row_count=row_count
            )
            return out

        return wrapped

    def wrap_node(
        self, node: str, fn: Callable[[GraphState], dict]
    ) -> Callable[[GraphState], dict]:
        recorder = self

        def wrapped(state: GraphState) -> dict:
            prev = recorder._node
            recorder._node = node
            t0 = time.perf_counter()
            try:
                updates = fn(state)
            finally:
                recorder._node = prev
            recorder.observe_node(node, state, updates or {}, time.perf_counter() - t0)
            return updates

        return wrapped

    def observe_node(
        self, node: str, state: GraphState, updates: dict, ms: float
    ) -> None:
        """节点返回后的 state→updates 差分采集：sql_result 与 step 事件。"""
        old_log = state.get("steps_log") or []
        new_log = updates.get("steps_log", old_log)
        for entry in new_log[len(old_log):]:
            self.emit(
                {
                    "type": "sql_result",
                    "node": node,
                    "step": entry.get("step"),
                    "step_id": entry.get("step_id"),
                    "attempt": entry.get("attempt"),
                    "outcome": entry.get("outcome"),
                    "sql": entry.get("sql"),
                    "fingerprint": entry.get("fingerprint"),
                    "row_count": entry.get("row_count"),
                    "ms": entry.get("ms"),
                    "error": entry.get("error"),
                }
            )
        plan = updates.get("plan")
        if plan is None:
            return
        if node in ("planner", "replan"):
            self.emit(
                {
                    "type": "step",
                    "node": node,
                    "event": "plan_created",
                    "question_type": updates.get("question_type")
                    or state.get("question_type"),
                    "replanned": bool(state.get("replanned")) or node == "replan",
                    "steps": [
                        {
                            "id": s["id"],
                            "description": s["description"],
                            "template": s.get("template"),
                            "status": s["status"],
                        }
                        for s in plan
                    ],
                }
            )
            return
        old_plan = state.get("plan") or []
        for i, s in enumerate(plan):
            old_status = old_plan[i]["status"] if i < len(old_plan) else None
            if s["status"] != old_status:
                self.emit(
                    {
                        "type": "step",
                        "node": node,
                        "event": "status_changed",
                        "step_id": s["id"],
                        "description": s["description"],
                        "from": old_status,
                        "to": s["status"],
                    }
                )

    # ---------- 终期 ----------

    def finalize(
        self,
        final: Mapping[str, Any] | None,
        *,
        error: BaseException | None = None,
        extra: dict | None = None,
    ) -> None:
        """落 conclusion + usage 两条收尾事件；异常中断传 final=None。"""
        if final is not None:
            conclusion = {
                "type": "conclusion",
                "status": final.get("status") or STATUS_FAILED,
                "conclusion": final.get("conclusion") or "",
                "question": final.get("question") or self.question,
                "question_type": final.get("question_type"),
                "evidence": final.get("evidence") or [],
                "errors": final.get("errors") or [],
                "fatal_error": final.get("fatal_error"),
            }
        else:
            conclusion = {
                "type": "conclusion",
                "status": STATUS_FAILED,
                "conclusion": "",
                "question": self.question,
                "evidence": [],
                "errors": [],
                "fatal_error": f"分析异常中断：{error!r}" if error else "分析异常中断",
            }
        if extra:
            conclusion.update(extra)
        self.emit(conclusion)

        usage: dict = {
            "type": "usage",
            "llm_calls": self.llm_calls,
            "query_calls": self.tool_calls.get("run_query", 0),
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "tool_calls": dict(self.tool_calls),
            "wall_ms": round((time.perf_counter() - self._t0) * 1000, 1),
        }
        if self.cost_model is not None:  # 费用估算开关：默认关
            usage["cost_est"] = round(
                self.tokens_in / 1000 * self.cost_model.per_1k_in
                + self.tokens_out / 1000 * self.cost_model.per_1k_out,
                6,
            )
        if extra:
            usage.update(extra)
        self.emit(usage)

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._fh.close()
