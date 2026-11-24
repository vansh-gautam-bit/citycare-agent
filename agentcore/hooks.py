"""Hooks around model and tool calls.

A "before" hook returns one of:
    Allow()                          carry on
    Block(reason, halt_reply=None)   don't do it. The model is told why (tool results are marked
                                     is_error). If halt_reply is set the run ends right there and
                                     that text is the final reply.
    Pause(message, data=None)        don't do it yet: end the run and ask the user. The call is
                                     NOT executed; `data` is handed back in RunResult.pending so the
                                     application can resume (for example, on the user's next "yes").

Hooks may be plain or async functions. If a before-hook raises, the call is blocked (fail closed).
The first non-Allow decision wins, in registration order.
"""

from __future__ import annotations

import inspect
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Union

from .types import RunContext, ToolCall, ToolResult

log = logging.getLogger("agentcore.hooks")


@dataclass(frozen=True)
class Allow:
    pass


@dataclass(frozen=True)
class Block:
    reason: str
    halt_reply: str | None = None


@dataclass(frozen=True)
class Pause:
    message: str
    data: Any = None


Decision = Union[Allow, Block, Pause]
_Maybe = Union[Any, Awaitable[Any]]

BeforeModelHook = Callable[[RunContext, Any], _Maybe]  # (ctx, ModelRequest) -> Decision | None
AfterModelHook = Callable[[RunContext, Any], _Maybe]  # (ctx, ModelResponse) -> None
BeforeToolHook = Callable[[RunContext, ToolCall], _Maybe]  # -> Decision | None
AfterToolHook = Callable[[RunContext, ToolCall, ToolResult], _Maybe]  # -> ToolResult | None


async def _call(fn: Callable, *args):
    out = fn(*args)
    return await out if inspect.isawaitable(out) else out


@dataclass
class Hooks:
    before_model: list[BeforeModelHook] = field(default_factory=list)
    after_model: list[AfterModelHook] = field(default_factory=list)
    before_tool: list[BeforeToolHook] = field(default_factory=list)
    after_tool: list[AfterToolHook] = field(default_factory=list)

    async def run_before_model(self, ctx: RunContext, request: Any) -> Decision:
        return await self._decide(self.before_model, ctx, request)

    async def run_before_tool(self, ctx: RunContext, call: ToolCall) -> Decision:
        return await self._decide(self.before_tool, ctx, call)

    async def _decide(self, hooks: list[Callable], *args) -> Decision:
        for hook in hooks:
            try:
                d = await _call(hook, *args)
            except Exception as e:  # noqa: BLE001 - fail closed
                log.exception("before-hook %s raised", getattr(hook, "__name__", hook))
                return Block(f"A safety check failed ({type(e).__name__}); the action was not performed.")
            if d is None or isinstance(d, Allow):
                continue
            if isinstance(d, (Block, Pause)):
                return d
            return Block(f"A safety check returned an invalid decision ({type(d).__name__}).")
        return Allow()

    async def run_after_model(self, ctx: RunContext, response: Any) -> None:
        for hook in self.after_model:
            try:
                await _call(hook, ctx, response)
            except Exception:  # noqa: BLE001 - observers must not break the run
                log.exception("after_model hook %s raised", getattr(hook, "__name__", hook))

    async def run_after_tool(self, ctx: RunContext, call: ToolCall, result: ToolResult) -> ToolResult:
        for hook in self.after_tool:
            try:
                replaced = await _call(hook, ctx, call, result)
            except Exception as e:  # noqa: BLE001 - fail closed: don't hand unchecked output to the model
                log.exception("after_tool hook %s raised", getattr(hook, "__name__", hook))
                return ToolResult(f"{call.name} result withheld by a safety check ({type(e).__name__}).", True)
            if isinstance(replaced, ToolResult):
                result = replaced
        return result
