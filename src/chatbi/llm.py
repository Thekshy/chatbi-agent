"""智谱 GLM 接入封装：全项目唯一 LLM 出口（SPEC §6.5 / §7）。

- 配置读环境变量（.env）：``ZHIPU_API_KEY`` 必填；``CHATBI_MODEL``、``CHATBI_BASE_URL``
  可省，默认取 SPEC §7 锁定值。
- 采样参数集中为模块常量（TEMPERATURE / TOP_P / MAX_TOKENS），调用点不散落魔数，
  需要偏离时以具名参数覆写。
- 429 / 5xx 指数退避重试至多 RETRY_MAX 次，仍失败抛 LLMRetryExhausted。
- chat() 返回 (content, LLMUsage(tokens_in, tokens_out))，token 用量由调用方
  回写 state 供 trace 与费用统计（T5/T6 接手）。
"""

import os
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from dotenv import load_dotenv
from openai import APIStatusError, OpenAI

DEFAULT_BASE_URL = "https://open.bigmodel.cn/api/coding/paas/v4/"
DEFAULT_MODEL = "glm-5.3"

# ---- 采样参数（集中管理；调用方可具名覆写）----
TEMPERATURE = 0.2  # 取数 / 归因类任务偏确定性
TOP_P = 0.9
MAX_TOKENS = 4096

# ---- 退避重试（SPEC Q5：429/5xx 重试 2 次）----
RETRY_MAX = 2
RETRY_BASE_DELAY = 1.0  # 秒；第 n 次重试前等待 BASE * 2**(n-1)
REQUEST_TIMEOUT = 120.0  # 秒；单次 HTTP 上限，防挂死拖垮同步分析


class LLMError(Exception):
    """llm 模块异常基类。"""


class LLMConfigError(LLMError):
    """配置缺失或非法（key / model / base_url）。"""


class LLMRetryExhausted(LLMError):
    """429/5xx 退避重试上限后仍失败；last_error 保留最后一次原始异常。"""

    def __init__(self, attempts: int, last_error: BaseException) -> None:
        self.attempts = attempts
        self.last_error = last_error
        super().__init__(
            f"LLM 调用连续 {attempts} 次失败（429/5xx 重试上限），最后错误：{last_error!r}"
        )


@dataclass(frozen=True)
class LLMUsage:
    tokens_in: int
    tokens_out: int


def _is_retryable(status_code: int) -> bool:
    return status_code == 429 or status_code >= 500


def _extract(resp: Any) -> tuple[str, LLMUsage]:
    choices = getattr(resp, "choices", None) or []
    content = getattr(getattr(choices[0], "message", None), "content", None) if choices else None
    usage = getattr(resp, "usage", None)
    tokens_in = int(getattr(usage, "prompt_tokens", 0) or 0)
    tokens_out = int(getattr(usage, "completion_tokens", 0) or 0)
    return content or "", LLMUsage(tokens_in=tokens_in, tokens_out=tokens_out)


class LLMClient:
    """智谱 OpenAI 兼容客户端薄封装。

    ``client`` / ``sleep`` 参数仅供测试注入替身；业务代码经 get_client() 取单例。
    """

    def __init__(
        self,
        *,
        model: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
        client: Any | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        load_dotenv()
        self.api_key = api_key or os.environ.get("ZHIPU_API_KEY")
        self.model = model or os.environ.get("CHATBI_MODEL") or DEFAULT_MODEL
        self.base_url = base_url or os.environ.get("CHATBI_BASE_URL") or DEFAULT_BASE_URL
        if client is None:
            if not self.api_key:
                raise LLMConfigError("缺少 ZHIPU_API_KEY（检查 .env，模板见 .env.example）")
            client = OpenAI(
                api_key=self.api_key,
                base_url=self.base_url,
                timeout=REQUEST_TIMEOUT,
                max_retries=0,  # 重试策略由本模块统一实现，SDK 层不叠加
            )
        self._client = client
        self._sleep = sleep

    def chat(
        self,
        messages: Sequence[Mapping[str, str]],
        *,
        temperature: float = TEMPERATURE,
        top_p: float = TOP_P,
        max_tokens: int = MAX_TOKENS,
    ) -> tuple[str, LLMUsage]:
        """一次对话补全，返回 (content, usage)；429/5xx 指数退避重试。"""
        attempt = 0
        delay = RETRY_BASE_DELAY
        while True:
            attempt += 1
            try:
                resp = self._client.chat.completions.create(
                    model=self.model,
                    messages=[dict(m) for m in messages],
                    temperature=temperature,
                    top_p=top_p,
                    max_tokens=max_tokens,
                )
            except APIStatusError as err:
                if not _is_retryable(err.status_code):
                    raise
                if attempt > RETRY_MAX:
                    raise LLMRetryExhausted(attempts=attempt, last_error=err) from err
                self._sleep(delay)
                delay *= 2
                continue
            return _extract(resp)


_default_client: LLMClient | None = None


def get_client() -> LLMClient:
    """进程级单例：全项目唯一的 LLM 出口句柄。"""
    global _default_client
    if _default_client is None:
        _default_client = LLMClient()
    return _default_client
