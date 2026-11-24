"""Model client: one place that talks to client.messages.create, with timeouts and retries.

Works with anthropic.Anthropic (sync, run in a thread), anthropic.AsyncAnthropic, and any fake
with the same `messages.create(**kwargs)` shape (tests/fake_claude.py).

Tip: build the SDK client with max_retries=0 so this module owns all retrying (otherwise
retries multiply: SDK retries x ours).
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import random
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

import anthropic

from .tracing import Usage

log = logging.getLogger("agentcore.model")


class ModelError(Exception):
    def __init__(self, message: str, *, retryable: bool = False, status: int | None = None, attempts: int = 1):
        super().__init__(message)
        self.retryable, self.status, self.attempts = retryable, status, attempts


@dataclass
class RetryPolicy:
    max_retries: int = 4
    base_delay_s: float = 0.5
    max_delay_s: float = 20.0
    jitter: float = 0.25  # +/- fraction of the delay

    def delay(self, attempt: int, retry_after: float | None = None) -> float:
        d = min(self.max_delay_s, self.base_delay_s * (2 ** attempt))
        d *= 1 + random.uniform(-self.jitter, self.jitter)
        return max(d, min(retry_after, self.max_delay_s)) if retry_after else d


@dataclass
class ModelRequest:
    model: str
    system: str
    messages: list[dict]
    tools: list[dict] = field(default_factory=list)
    max_tokens: int = 2048
    system_suffix: str = ""  # volatile text (date, caller facts) placed AFTER the cached prefix
    cache: bool = True  # prompt caching on the system prompt and tool list
    params: dict[str, Any] = field(default_factory=dict)  # passthrough, e.g. {"output_config": {"effort": "low"}}

    def to_kwargs(self) -> dict:
        kw: dict[str, Any] = {"model": self.model, "max_tokens": self.max_tokens, "messages": self.messages}
        system: list[dict] = []
        if self.system:
            block: dict[str, Any] = {"type": "text", "text": self.system}
            if self.cache:
                block["cache_control"] = {"type": "ephemeral"}
            system.append(block)
        if self.system_suffix:
            system.append({"type": "text", "text": self.system_suffix})
        if system:
            kw["system"] = system
        if self.tools:
            tools = [dict(t) for t in self.tools]
            if self.cache:
                tools[-1]["cache_control"] = {"type": "ephemeral"}
            kw["tools"] = tools
        kw.update(self.params)
        return kw


@dataclass
class ModelResponse:
    content: list[dict]  # JSON-safe content blocks
    stop_reason: str | None
    usage: Usage
    model: str = ""
    id: str = ""
    stop_details: dict | None = None
    attempts: int = 1

    @property
    def text(self) -> str:
        return "".join(b.get("text", "") for b in self.content if b.get("type") == "text")

    @property
    def tool_uses(self) -> list[dict]:
        return [b for b in self.content if b.get("type") == "tool_use"]


def _block_to_dict(b: Any) -> dict:
    if isinstance(b, dict):
        return b
    return b.model_dump(mode="json", exclude_none=True)


def _retry_after(e: Exception) -> float | None:
    try:
        return float(e.response.headers.get("retry-after"))  # type: ignore[attr-defined]
    except (AttributeError, TypeError, ValueError):
        return None


def classify(e: Exception) -> tuple[bool, int | None]:
    """(retryable?, http status) for an SDK exception."""
    if isinstance(e, asyncio.TimeoutError):
        return True, None
    if isinstance(e, anthropic.APIConnectionError):  # includes APITimeoutError
        return True, None
    if isinstance(e, anthropic.APIStatusError):
        s = e.status_code
        return (s in (408, 409, 429, 529) or s >= 500), s
    return False, None


class ModelClient:
    def __init__(self, client: Any, *, retry: RetryPolicy | None = None, timeout_s: float = 60.0,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep):
        self.client = client
        self.retry = retry or RetryPolicy()
        self.timeout_s = timeout_s
        self._sleep = sleep

    async def _once(self, kwargs: dict) -> Any:
        create = self.client.messages.create
        if inspect.iscoroutinefunction(create):
            return await create(**kwargs)
        out = await asyncio.to_thread(create, **kwargs)  # sync client: keep the event loop free
        return await out if inspect.isawaitable(out) else out

    async def create(self, request: ModelRequest, *, timeout_s: float | None = None) -> ModelResponse:
        kwargs = request.to_kwargs()
        per_try = min(self.timeout_s, timeout_s) if timeout_s else self.timeout_s
        attempt = 0
        while True:
            try:
                msg = await asyncio.wait_for(self._once(kwargs), per_try)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                retryable, status = classify(e)
                if not retryable or attempt >= self.retry.max_retries:
                    raise ModelError(f"{type(e).__name__}: {e}", retryable=retryable, status=status,
                                     attempts=attempt + 1) from e
                delay = self.retry.delay(attempt, _retry_after(e))
                log.warning("model call failed (%s); retry %d in %.1fs", type(e).__name__, attempt + 1, delay)
                await self._sleep(delay)
                attempt += 1
                continue
            sd = getattr(msg, "stop_details", None)
            return ModelResponse(
                content=[_block_to_dict(b) for b in msg.content],
                stop_reason=msg.stop_reason,
                usage=Usage.from_response(msg.usage),
                model=getattr(msg, "model", "") or request.model,
                id=getattr(msg, "id", "") or "",
                stop_details=(sd.model_dump(mode="json") if hasattr(sd, "model_dump") else sd) or None,
                attempts=attempt + 1,
            )
