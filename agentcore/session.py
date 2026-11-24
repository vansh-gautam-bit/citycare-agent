"""Session store: conversation history + app state, behind a small interface.

Everything stored is plain JSON, and InMemorySessionStore round-trips it through JSON on every
save and load. That keeps it honest: a Redis implementation (SET key json, plus a Redis lock)
can replace it without changing the runtime, because nothing relies on shared object identity.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Protocol, runtime_checkable


@dataclass
class Session:
    id: str
    messages: list[dict] = field(default_factory=list)  # Claude-format messages
    state: dict[str, Any] = field(default_factory=dict)  # app-owned, must be JSON-serialisable

    def to_json(self) -> str:
        return json.dumps({"id": self.id, "messages": self.messages, "state": self.state}, default=str)

    @classmethod
    def from_json(cls, raw: str) -> "Session":
        d = json.loads(raw)
        return cls(id=d["id"], messages=d.get("messages", []), state=d.get("state", {}))


@runtime_checkable
class SessionStore(Protocol):
    async def load(self, session_id: str) -> Session:
        """Return the session, or a new empty one if it doesn't exist."""

    async def save(self, session: Session) -> None: ...

    async def delete(self, session_id: str) -> None: ...

    def lock(self, session_id: str) -> "AsyncContextManagerLike":
        """`async with store.lock(id):` serialises turns of one conversation (a Redis impl would use a lock key)."""


class AsyncContextManagerLike(Protocol):  # pragma: no cover
    async def __aenter__(self) -> Any: ...
    async def __aexit__(self, *exc) -> Any: ...


class InMemorySessionStore:
    def __init__(self) -> None:
        self._data: dict[str, str] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def load(self, session_id: str) -> Session:
        raw = self._data.get(session_id)
        return Session.from_json(raw) if raw else Session(id=session_id)

    async def save(self, session: Session) -> None:
        self._data[session.id] = session.to_json()

    async def delete(self, session_id: str) -> None:
        self._data.pop(session_id, None)
        self._locks.pop(session_id, None)

    @asynccontextmanager
    async def lock(self, session_id: str) -> AsyncIterator[None]:
        lk = self._locks.setdefault(session_id, asyncio.Lock())
        async with lk:
            yield
