"""Tool registry: Python functions + Pydantic input schemas -> Claude tool definitions.

Rules the runtime follows so a tool can never crash a run:
  * unknown tool, invalid input, a ToolError, a timeout or any other exception all become an
    `is_error` tool result the model can read and react to;
  * only registered names are callable (no getattr dispatch);
  * the handler gets the validated model, plus the RunContext if it asks for it.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from dataclasses import dataclass
from typing import Any, Callable

from pydantic import BaseModel, ValidationError

from .types import RunContext, ToolCall, ToolResult

log = logging.getLogger("agentcore.tools")


class ToolError(Exception):
    """Raise from a tool to give the model a clear, safe message. The text is shown to the model."""


@dataclass
class Tool:
    name: str
    description: str
    input_model: type[BaseModel]
    handler: Callable[..., Any]  # (args) or (args, ctx); sync or async
    side_effect: bool = False  # True if it changes something outside the process
    timeout_s: float = 30.0

    def definition(self) -> dict:
        schema = _clean_schema(self.input_model.model_json_schema())
        schema.setdefault("type", "object")
        schema.setdefault("properties", {})
        schema["additionalProperties"] = False
        return {"name": self.name, "description": self.description, "input_schema": schema}

    async def invoke(self, args: BaseModel, ctx: RunContext) -> Any:
        wants_ctx = len(inspect.signature(self.handler).parameters) >= 2
        call = (lambda: self.handler(args, ctx)) if wants_ctx else (lambda: self.handler(args))
        if inspect.iscoroutinefunction(self.handler):
            return await asyncio.wait_for(call(), self.timeout_s)
        out = await asyncio.wait_for(asyncio.to_thread(call), self.timeout_s)
        return await out if inspect.isawaitable(out) else out


def _clean_schema(node: Any) -> Any:
    """Drop the noisy `title` keys pydantic adds (keeps property names called "title")."""
    if isinstance(node, dict):
        out = {}
        for k, v in node.items():
            if k == "title" and isinstance(v, str):
                continue
            out[k] = _clean_props(v) if k == "properties" and isinstance(v, dict) else _clean_schema(v)
        return out
    if isinstance(node, list):
        return [_clean_schema(i) for i in node]
    return node


def _clean_props(props: dict) -> dict:
    return {name: _clean_schema(sub) for name, sub in props.items()}


class ToolRegistry:
    def __init__(self, tools: list[Tool] | None = None):
        self._tools: dict[str, Tool] = {}
        for t in tools or []:
            self.register(t)

    def register(self, tool: Tool) -> Tool:
        if tool.name in self._tools:
            raise ValueError(f"tool {tool.name!r} already registered")
        self._tools[tool.name] = tool
        return tool

    def tool(self, *, name: str | None = None, description: str, input_model: type[BaseModel],
             side_effect: bool = False, timeout_s: float = 30.0):
        """Decorator form: @registry.tool(description=..., input_model=Args)"""
        def deco(fn: Callable[..., Any]):
            self.register(Tool(name or fn.__name__, description, input_model, fn, side_effect, timeout_s))
            return fn
        return deco

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    @property
    def names(self) -> list[str]:
        return list(self._tools)

    def definitions(self) -> list[dict]:
        """Claude tool definitions, in registration order (stable order keeps prompt caching valid)."""
        return [t.definition() for t in self._tools.values()]

    def prepare(self, tool_use_id: str, name: str, raw_input: Any) -> ToolCall | ToolResult:
        """Find the tool and validate its input. Returns a ToolCall, or an error ToolResult."""
        tool = self._tools.get(name)
        if tool is None:
            return ToolResult(f"Unknown tool {name!r}. Available tools: {', '.join(self.names) or 'none'}.", True)
        if not isinstance(raw_input, dict):
            return ToolResult(f"Invalid input for {name}: expected a JSON object.", True)
        try:
            args = tool.input_model.model_validate(raw_input)
        except ValidationError as e:
            problems = "; ".join(
                f"{'.'.join(str(p) for p in err['loc']) or 'input'}: {err['msg']}" for err in e.errors()
            )
            return ToolResult(f"Invalid input for {name}: {problems}. Fix the input and call the tool again.", True)
        return ToolCall(id=tool_use_id, name=name, raw_input=raw_input, tool=tool, args=args)

    async def execute(self, call: ToolCall, ctx: RunContext) -> ToolResult:
        """Run a prepared call. Never raises (except cancellation)."""
        try:
            out = await call.tool.invoke(call.args, ctx)
        except ToolError as e:
            return ToolResult(str(e), True)
        except asyncio.TimeoutError:
            return ToolResult(f"{call.name} timed out after {call.tool.timeout_s:g}s.", True)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - a tool bug must not crash the run
            log.exception("tool %s failed", call.name)
            return ToolResult(f"{call.name} failed with an internal error ({type(e).__name__}).", True)
        return out if isinstance(out, ToolResult) else ToolResult(out)
