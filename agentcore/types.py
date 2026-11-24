"""Small shared types. Nothing here knows about any particular application."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

if TYPE_CHECKING:  # pragma: no cover
    from .tools import Tool
    from .tracing import Tracer


@dataclass
class RunContext:
    """Everything a tool or hook may need about the current run.

    The application owns `state` and `deps`; the runtime never looks inside them.
    Put the caller's verified identity, clients, flags etc. there. Tools and hooks
    read them from here instead of trusting anything the model typed.
    """

    session_id: str
    run_id: str = ""
    state: dict[str, Any] = field(default_factory=dict)
    deps: Any = None
    agent_name: str = ""
    step: int = 0
    messages: list[dict] = field(default_factory=list)  # live view of the conversation so far
    tracer: "Tracer | None" = None
    deadline: float | None = None  # monotonic timestamp, set by the runner


@dataclass
class ToolResult:
    content: Any
    is_error: bool = False

    def text(self) -> str:
        c = self.content
        if isinstance(c, str):
            return c
        if isinstance(c, BaseModel):
            c = c.model_dump(mode="json")
        try:
            return json.dumps(c, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            return str(c)


@dataclass
class ToolCall:
    """One tool_use block, after the registry has found the tool and validated the input."""

    id: str
    name: str
    raw_input: dict
    tool: "Tool"
    args: BaseModel
