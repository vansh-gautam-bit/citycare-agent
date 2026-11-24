"""Tracing: one span per model call and per tool call, with inputs, outputs, timing, tokens, cost."""

from __future__ import annotations

import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass
class Usage:
    input_tokens: int = 0  # uncached input
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    def add(self, other: "Usage") -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_read_tokens += other.cache_read_tokens
        self.cache_write_tokens += other.cache_write_tokens

    def to_dict(self) -> dict:
        return dict(self.__dict__)

    @classmethod
    def from_response(cls, usage: Any) -> "Usage":
        g = lambda name: int(getattr(usage, name, 0) or 0)  # noqa: E731 - fields may be missing or None
        return cls(g("input_tokens"), g("output_tokens"), g("cache_read_input_tokens"), g("cache_creation_input_tokens"))


@dataclass(frozen=True)
class ModelPrice:
    """USD per million tokens."""
    input: float
    output: float
    cache_read: float | None = None   # default 0.1x input
    cache_write: float | None = None  # default 1.25x input


# Prices from context.md section 6 / the current model table. Cache-read rates marked (est.) are
# assumed 0.1x input where the docs don't list one. Haiku 5.5 is dearer above 100K-token prompts;
# front-desk prompts are far below that, so it is ignored here.
DEFAULT_PRICES: dict[str, ModelPrice] = {
    "claude-haiku-5-5": ModelPrice(0.10, 0.50),               # cache read (est.)
    "claude-sonnet-5-5": ModelPrice(2.00, 10.00, cache_read=0.10),
    "claude-opus-5-5": ModelPrice(4.00, 20.00, cache_read=0.20),
}


def cost_usd(model: str, usage: Usage, prices: dict[str, ModelPrice] | None = None) -> float | None:
    """Cost in USD, or None if the model has no known price (so unknowns are never silently free)."""
    p = (prices or DEFAULT_PRICES).get(model)
    if p is None:
        return None
    read = p.cache_read if p.cache_read is not None else p.input * 0.1
    write = p.cache_write if p.cache_write is not None else p.input * 1.25
    return (usage.input_tokens * p.input + usage.output_tokens * p.output
            + usage.cache_read_tokens * read + usage.cache_write_tokens * write) / 1_000_000


def _clip(value: Any, limit: int) -> Any:
    """JSON-safe copy with long strings shortened, so traces stay small."""
    if isinstance(value, str):
        return value if len(value) <= limit else value[:limit] + f"...[+{len(value) - limit} chars]"
    if isinstance(value, dict):
        return {str(k): _clip(v, limit) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clip(v, limit) for v in value]
    if value is None or isinstance(value, (int, float, bool)):
        return value
    if hasattr(value, "model_dump"):
        return _clip(value.model_dump(mode="json"), limit)
    return _clip(str(value), limit)


@dataclass
class Span:
    kind: str  # "model" | "tool"
    name: str
    agent: str = ""
    step: int = 0
    started_at: str = ""
    duration_ms: float = 0.0
    input: Any = None
    output: Any = None
    error: str | None = None
    usage: Usage | None = None
    cost_usd: float | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = dict(self.__dict__)
        d["usage"] = self.usage.to_dict() if self.usage else None
        return d


class _SpanCtx:
    def __init__(self, tracer: "Tracer", span: Span):
        self.tracer, self.span = tracer, span

    def __enter__(self) -> Span:
        self._t0 = time.perf_counter()
        return self.span

    def __exit__(self, exc_type, exc, tb) -> bool:
        s = self.span
        s.duration_ms = round((time.perf_counter() - self._t0) * 1000, 1)
        if exc is not None and s.error is None:
            s.error = f"{type(exc).__name__}: {exc}"
        s.input = _clip(s.input, self.tracer.max_chars)
        s.output = _clip(s.output, self.tracer.max_chars)
        self.tracer.spans.append(s)
        return False  # never swallow


class Tracer:
    def __init__(self, session_id: str, trace_id: str | None = None, max_chars: int = 4000):
        self.session_id = session_id
        self.trace_id = trace_id or uuid.uuid4().hex
        self.max_chars = max_chars
        self.started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.spans: list[Span] = []

    def span(self, kind: str, name: str, *, agent: str = "", step: int = 0, input: Any = None, **meta: Any) -> _SpanCtx:
        s = Span(kind=kind, name=name, agent=agent, step=step, input=input, meta=meta,
                 started_at=datetime.now(timezone.utc).isoformat(timespec="milliseconds"))
        return _SpanCtx(self, s)

    def totals(self) -> dict:
        usage, cost, unknown = Usage(), 0.0, False
        for s in self.spans:
            if s.kind == "model":
                if s.usage:
                    usage.add(s.usage)
                if s.cost_usd is None:
                    unknown = unknown or s.usage is not None
                else:
                    cost += s.cost_usd
        return {"model_calls": sum(s.kind == "model" for s in self.spans),
                "tool_calls": sum(s.kind == "tool" for s in self.spans),
                "usage": usage.to_dict(), "cost_usd": round(cost, 6), "cost_complete": not unknown,
                "duration_ms": round(sum(s.duration_ms for s in self.spans), 1)}

    def to_dict(self) -> dict:
        return {"trace_id": self.trace_id, "session_id": self.session_id, "started_at": self.started_at,
                "totals": self.totals(), "spans": [s.to_dict() for s in self.spans]}


class InMemoryTraceStore:
    """Keeps the most recent `max_sessions` sessions' traces, for GET /trace/{session_id}."""

    def __init__(self, max_sessions: int = 1000):
        self.max_sessions = max_sessions
        self._by_session: OrderedDict[str, list[Tracer]] = OrderedDict()

    def add(self, tracer: Tracer) -> None:
        self._by_session.setdefault(tracer.session_id, []).append(tracer)
        self._by_session.move_to_end(tracer.session_id)
        while len(self._by_session) > self.max_sessions:
            self._by_session.popitem(last=False)

    def get(self, session_id: str) -> list[dict] | None:
        traces = self._by_session.get(session_id)
        return [t.to_dict() for t in traces] if traces is not None else None
