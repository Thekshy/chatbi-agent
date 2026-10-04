"""T3 测试：LLM 封装的配置读取、返回契约与 429/5xx 指数退避重试。

单元测试注入 fake client（不打网络，不需要真实凭据）；`test_real_smoke` 为
integration 标记的真实冒烟，未配置 ZHIPU_API_KEY 时自动 skip
（T3 ⑤ 验收：非空内容 + token 计数 > 0）。
"""

from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from openai import APIStatusError, RateLimitError

import chatbi.llm as llm_mod
from chatbi.llm import (
    DEFAULT_BASE_URL,
    DEFAULT_MODEL,
    RETRY_BASE_DELAY,
    RETRY_MAX,
    LLMClient,
    LLMConfigError,
    LLMRetryExhausted,
    LLMUsage,
)

# ---------- fakes ----------


class FakeCompletions:
    """按脚本回放的 chat.completions.create：每个 outcome 为响应对象或待抛异常。"""

    def __init__(self, outcomes: list[Any]) -> None:
        self._outcomes = list(outcomes)
        self.calls = 0
        self.last_kwargs: dict[str, Any] = {}

    def create(self, **kwargs: Any) -> Any:
        self.calls += 1
        self.last_kwargs = kwargs
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class FakeClient:
    def __init__(self, outcomes: list[Any]) -> None:
        self.chat = SimpleNamespace(completions=FakeCompletions(outcomes))

    @property
    def completions(self) -> FakeCompletions:
        return self.chat.completions


class Sleeper:
    """记录退避时长的 sleep 替身。"""

    def __init__(self) -> None:
        self.delays: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)


def fake_response(content: str = "ok", tokens_in: int = 12, tokens_out: int = 34) -> Any:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
        usage=SimpleNamespace(prompt_tokens=tokens_in, completion_tokens=tokens_out),
    )


def status_error(code: int) -> APIStatusError:
    request = httpx.Request("POST", "https://fake.test/v1/chat/completions")
    response = httpx.Response(code, request=request)
    if code == 429:
        return RateLimitError("rate limited", response=response, body=None)
    return APIStatusError(f"http {code}", response=response, body=None)


def make_client(outcomes: list[Any]) -> tuple[LLMClient, FakeClient, Sleeper]:
    fake = FakeClient(outcomes)
    sleeper = Sleeper()
    client = LLMClient(
        model="glm-test",
        base_url="https://fake.test/v1/",
        client=fake,
        sleep=sleeper,
    )
    return client, fake, sleeper


# ---------- 配置读取 ----------


def test_config_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(llm_mod, "load_dotenv", lambda *a, **k: False)
    monkeypatch.setenv("CHATBI_MODEL", "glm-x")
    monkeypatch.setenv("CHATBI_BASE_URL", "https://env.test/v1/")
    client = LLMClient(client=FakeClient([fake_response()]))
    assert client.model == "glm-x"
    assert client.base_url == "https://env.test/v1/"


def test_config_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(llm_mod, "load_dotenv", lambda *a, **k: False)
    monkeypatch.delenv("CHATBI_MODEL", raising=False)
    monkeypatch.delenv("CHATBI_BASE_URL", raising=False)
    client = LLMClient(client=FakeClient([fake_response()]))
    assert client.model == DEFAULT_MODEL
    assert client.base_url == DEFAULT_BASE_URL


def test_missing_key_raises_config_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(llm_mod, "load_dotenv", lambda *a, **k: False)
    monkeypatch.delenv("ZHIPU_API_KEY", raising=False)
    with pytest.raises(LLMConfigError, match="ZHIPU_API_KEY"):
        LLMClient()  # 未注入 client 时必须提供 key 才能构造真实客户端


def test_get_client_is_singleton(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(llm_mod, "load_dotenv", lambda *a, **k: False)
    monkeypatch.setenv("ZHIPU_API_KEY", "k-1")
    monkeypatch.setattr(llm_mod, "_default_client", None)
    first = llm_mod.get_client()
    assert llm_mod.get_client() is first


# ---------- 返回契约与请求参数 ----------


def test_chat_returns_content_and_usage() -> None:
    client, fake, _ = make_client([fake_response("你好，世界", 7, 9)])
    content, usage = client.chat([{"role": "user", "content": "hi"}])
    assert content == "你好，世界"
    assert usage == LLMUsage(tokens_in=7, tokens_out=9)
    sent = fake.completions.last_kwargs
    assert sent["model"] == "glm-test"
    assert sent["messages"] == [{"role": "user", "content": "hi"}]
    # 集中管理的采样常量确实下发
    assert sent["temperature"] == llm_mod.TEMPERATURE
    assert sent["top_p"] == llm_mod.TOP_P
    assert sent["max_tokens"] == llm_mod.MAX_TOKENS


def test_chat_missing_usage_counts_zero() -> None:
    resp = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="x"))], usage=None
    )
    client, _, _ = make_client([resp])
    _, usage = client.chat([{"role": "user", "content": "hi"}])
    assert usage == LLMUsage(tokens_in=0, tokens_out=0)


# ---------- 429 / 5xx 指数退避 ----------


@pytest.mark.parametrize("code", [429, 500, 502, 503, 504])
def test_retryable_status_then_success(code: int) -> None:
    client, fake, sleeper = make_client([status_error(code), fake_response()])
    content, usage = client.chat([{"role": "user", "content": "hi"}])
    assert content == "ok"
    assert usage.tokens_out == 34
    assert fake.completions.calls == 2  # 首发 1 次 + 重试 1 次
    assert sleeper.delays == [RETRY_BASE_DELAY]


def test_retry_exhausted_raises_specific_error() -> None:
    last = status_error(429)
    client, fake, sleeper = make_client([status_error(429), status_error(429), last])
    with pytest.raises(LLMRetryExhausted) as excinfo:
        client.chat([{"role": "user", "content": "hi"}])
    assert fake.completions.calls == 1 + RETRY_MAX  # 首发 + 2 次重试后仍失败
    assert sleeper.delays == [RETRY_BASE_DELAY, RETRY_BASE_DELAY * 2]  # 指数退避
    assert excinfo.value.last_error is last
    assert excinfo.value.__cause__ is last


def test_non_retryable_status_raises_immediately() -> None:
    client, fake, sleeper = make_client([status_error(401)])
    with pytest.raises(APIStatusError) as excinfo:
        client.chat([{"role": "user", "content": "hi"}])
    assert excinfo.value.status_code == 401
    assert fake.completions.calls == 1
    assert sleeper.delays == []


# ---------- integration：真实调用一条 ----------


@pytest.mark.integration
def test_real_smoke() -> None:
    try:
        client = LLMClient()  # 读真实 .env（key/model/base_url）
    except LLMConfigError:
        pytest.skip("未配置 ZHIPU_API_KEY，跳过真实冒烟")
    content, usage = client.chat([{"role": "user", "content": "只回复两个字：就绪"}])
    assert content.strip(), "冒烟内容为空"
    assert usage.tokens_in > 0
    assert usage.tokens_out > 0
