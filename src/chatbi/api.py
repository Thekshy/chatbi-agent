"""api：核心库入口——analyze() 契约落地（T7 / SPEC §7；评测 T10 与 Web T8 共用的唯一入口）。

- ``AnalysisResult``：SPEC §7 契约——status 三态 / conclusion / evidence（sql、row_count、
  rows_preview、ms）/ steps（steps_log，每次 SQL 生成尝试的审计记录）/ usage / trace_dir
  （Path；trace 关闭时为 None）；另附 ``errors``（非致命错误栈，诊断用）。frozen 防误写，
  ``to_dict()`` 给出 JSON 可序列化形态（Web T8 直接复用）。
- ``RunConfig``：预算覆写（budgets，默认 state.DEFAULT_BUDGETS）/ 模型覆写（model，
  None 走环境默认）/ trace 开关（trace_root 与 checkpoint_path 随行，测试与多环境重定向用）。
- ``analyze(question, *, config)``：包装 graph.run_analysis，**任何内部异常捕获转为
  failed 结果、不向上抛**（T7④）。KeyboardInterrupt / SystemExit 不属内部异常，照常传播。
  异常中断时 run_analysis 已在盘上留下 failed trace，但路径无法回收，trace_dir 记 None。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from chatbi.graph import run_analysis
from chatbi.llm import LLMClient
from chatbi.state import DEFAULT_BUDGETS, STATUS_FAILED, Budgets

_ZERO_USAGE: dict = {"llm_calls": 0, "query_calls": 0, "tokens_in": 0, "tokens_out": 0}


@dataclass(frozen=True)
class AnalysisResult:
    """一次分析的结构化结果（SPEC §7 契约字段 + errors 诊断附注）。"""

    status: str  # answered | partial | failed
    conclusion: str
    evidence: list[dict]  # [{id, description, sql, fingerprint, row_count, rows_preview, ms}]
    steps: list[dict]  # steps_log：[{step_id, description, attempt, outcome, sql, error, ...}]
    usage: dict  # {llm_calls, query_calls, tokens_in, tokens_out}
    trace_dir: Path | None  # runs/<run_id>/；trace 关闭或异常中断时为 None
    errors: list[str] = field(default_factory=list)  # 非致命错误栈（工具降级等）

    def to_dict(self) -> dict:
        """JSON 可序列化形态（Path → str）。"""
        return {
            "status": self.status,
            "conclusion": self.conclusion,
            "evidence": self.evidence,
            "steps": self.steps,
            "usage": self.usage,
            "trace_dir": str(self.trace_dir) if self.trace_dir is not None else None,
            "errors": self.errors,
        }


@dataclass(frozen=True)
class RunConfig:
    """analyze() 的运行配置：预算 / 模型 / trace 三类覆写（T7④）。"""

    budgets: Budgets = DEFAULT_BUDGETS
    model: str | None = None  # None → 环境变量 CHATBI_MODEL / llm.DEFAULT_MODEL
    trace: bool = True
    trace_root: str | Path = "runs"
    checkpoint_path: str | None = "runs/checkpoints.db"


def analyze(question: str, *, config: RunConfig | None = None) -> AnalysisResult:
    """一次完整分析的唯一入口（评测与 Web 共用；SPEC §7）。

    config 缺省取 ``RunConfig()``（SPEC 签名中 config 为必传关键字，此处放宽为可选默认，
    便于 CLI 与批量评测零参调用）。任何内部异常都转为 ``failed`` 结果，不向上抛。
    """
    cfg = config or RunConfig()
    try:
        llm = LLMClient(model=cfg.model) if cfg.model is not None else None
        raw = run_analysis(
            question,
            llm=llm,
            budgets=cfg.budgets,
            checkpoint_path=cfg.checkpoint_path,
            trace=cfg.trace,
            trace_root=str(cfg.trace_root),
        )
        return AnalysisResult(
            status=raw["status"] or STATUS_FAILED,
            conclusion=raw["conclusion"],
            evidence=list(raw["evidence"]),
            steps=list(raw["steps_log"]),
            usage=dict(raw["usage"]),
            trace_dir=Path(raw["trace_dir"]) if raw["trace_dir"] else None,
            errors=list(raw["errors"]),
        )
    except Exception as err:  # noqa: BLE001 —— T7④：内部异常一律转 failed，不向上抛
        return AnalysisResult(
            status=STATUS_FAILED,
            conclusion=(
                "分析异常中断（已捕获并转为 failed，未向上抛出）。\n"
                f"问题：{question}\n"
                f"失败原因：{err!r}\n"
                "异常发生前的进度不可回收（图执行内部抛出，未见结构化步骤记录）；"
                "若开启 trace，中断现场可能已落盘于 trace_root 下对应的 run 目录。"
            ),
            evidence=[],
            steps=[],
            usage=dict(_ZERO_USAGE),
            trace_dir=None,
            errors=[repr(err)],
        )
