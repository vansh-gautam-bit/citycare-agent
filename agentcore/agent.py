"""Agent definition and the loop that runs it.

    loop -> model -> tool call -> [before-tool hooks] -> tool -> [after-tool hooks] -> model ...

Built on client.messages.create (see model.py), not an SDK tool runner. The loop never raises for
model, tool or hook failures; it always returns a RunResult whose `status` says what happened:

    completed   model finished its turn                    max_tokens  reply was cut off
    paused      a hook paused for user confirmation        refused     the model declined (safety classifier)
    blocked     a hook halted the run with a reply         max_steps   step limit reached
    timeout     the run's time budget ran out              error       the model API failed after retries
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Literal

from .hooks import Block, Hooks, Pause
from .model import ModelClient, ModelError, ModelRequest, ModelResponse
from .tools import ToolRegistry
from .tracing import ModelPrice, Tracer, Usage, cost_usd
from .types import RunContext, ToolResult

RunStatus = Literal["completed", "paused", "blocked", "max_tokens", "refused", "max_steps", "timeout", "error"]


@dataclass
class Agent:
    name: str
    model: str
    system: str  # keep stable: it is the cached prefix
    tools: ToolRegistry = field(default_factory=ToolRegistry)
    hooks: Hooks = field(default_factory=Hooks)
    max_steps: int = 8
    max_tokens: int = 2048
    dynamic_system: Callable[[RunContext], str] | None = None  # volatile facts, added after the cached prefix
    params: dict[str, Any] = field(default_factory=dict)  # passthrough request params (effort, ...)
    cache: bool = True


@dataclass
class Pending:
    """A tool call a hook paused. It was NOT executed."""
    tool_use_id: str
    tool: str
    args: dict
    message: str  # what the user was asked
    data: Any = None  # whatever the hook attached


@dataclass
class RunResult:
    status: RunStatus
    text: str
    messages: list[dict]  # full history including this run's turns; save it
    new_from: int  # index in `messages` where this run's messages start
    usage: Usage
    cost_usd: float | None
    steps: int
    trace_id: str
    pending: Pending | None = None
    error: str | None = None


class Runner:
    def __init__(self, model_client: ModelClient, *, prices: dict[str, ModelPrice] | None = None):
        self.model_client = model_client
        self.prices = prices

    async def run(self, agent: Agent, messages: list[dict], ctx: RunContext, *,
                  timeout_s: float | None = None) -> RunResult:
        msgs = list(messages)
        start = len(msgs)
        ctx.agent_name = agent.name
        ctx.run_id = ctx.run_id or uuid.uuid4().hex[:12]
        ctx.messages = msgs  # live view for hooks and tools
        tracer = ctx.tracer = ctx.tracer or Tracer(ctx.session_id)
        ctx.deadline = time.monotonic() + timeout_s if timeout_s else None
        total, cost, cost_known = Usage(), 0.0, True
        step = 0

        def finish(status: RunStatus, text: str = "", **kw: Any) -> RunResult:
            return RunResult(status=status, text=text, messages=msgs, new_from=start, usage=total,
                             cost_usd=round(cost, 6) if cost_known else None, steps=step,
                             trace_id=tracer.trace_id, **kw)

        async def handle_tools(uses: list[dict], truncated: bool) -> RunResult | None:
            """Run tool calls through hooks, append their results. A RunResult means the run must end."""
            results, halt, pause = await self._run_tools(agent, ctx, uses, truncated)
            msgs.append({"role": "user", "content": results})
            if pause is not None:
                msgs.append({"role": "assistant", "content": [{"type": "text", "text": pause[0].message}]})
                return finish("paused", pause[0].message, pending=pause[1])
            if halt is not None:
                msgs.append({"role": "assistant", "content": [{"type": "text", "text": halt}]})
                return finish("blocked", halt)
            return None

        # Resume: if the history ends with tool calls nobody has answered yet, run them first. The caller
        # uses this to carry out a call it already knows about (e.g. one the user just confirmed) while
        # every hook still runs on it.
        if msgs and msgs[-1]["role"] == "assistant" and isinstance(msgs[-1]["content"], list):
            open_uses = [b for b in msgs[-1]["content"] if b.get("type") == "tool_use"]
            if open_uses and (ended := await handle_tools(open_uses, False)) is not None:
                return ended

        while step < agent.max_steps:
            step += 1
            ctx.step = step
            remaining = None if ctx.deadline is None else ctx.deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                return finish("timeout")

            request = ModelRequest(
                model=agent.model, system=agent.system, messages=list(msgs), tools=agent.tools.definitions(),
                max_tokens=agent.max_tokens, cache=agent.cache, params=dict(agent.params),
                system_suffix=agent.dynamic_system(ctx) if agent.dynamic_system else "",
            )
            decision = await agent.hooks.run_before_model(ctx, request)
            if isinstance(decision, Block):
                return finish("blocked", decision.halt_reply or decision.reason)
            if isinstance(decision, Pause):
                return finish("paused", decision.message,
                              pending=Pending("", "", {}, decision.message, decision.data))

            try:
                with tracer.span("model", agent.model, agent=agent.name, step=step,
                                 input={"system": request.system, "system_suffix": request.system_suffix,
                                        "messages": request.messages, "tools": [t["name"] for t in request.tools],
                                        "params": request.params}) as span:
                    coro = self.model_client.create(request, timeout_s=remaining)
                    resp: ModelResponse = await (asyncio.wait_for(coro, remaining) if remaining else coro)
                    span.output = {"content": resp.content, "stop_reason": resp.stop_reason}
                    span.usage = resp.usage
                    span.cost_usd = cost_usd(resp.model or agent.model, resp.usage, self.prices)
                    span.meta.update(attempts=resp.attempts, request_id=resp.id)
            except asyncio.TimeoutError:
                return finish("timeout")
            except ModelError as e:
                return finish("error", error=str(e))

            total.add(resp.usage)
            if span.cost_usd is None:
                cost_known = False
            else:
                cost += span.cost_usd
            await agent.hooks.run_after_model(ctx, resp)

            sr = resp.stop_reason
            if sr == "refusal":  # not added to history: a refused turn would poison later requests
                return finish("refused", error=str((resp.stop_details or {}).get("category") or "refusal"))
            if resp.content:
                msgs.append({"role": "assistant", "content": resp.content})

            if sr in ("end_turn", "stop_sequence"):
                return finish("completed", resp.text)
            if sr == "pause_turn":  # model asked to be resumed; send the conversation back as is
                continue
            if sr == "max_tokens" and not resp.tool_uses:
                return finish("max_tokens", resp.text)
            if sr in ("tool_use", "max_tokens"):
                if (ended := await handle_tools(resp.tool_uses, truncated=(sr == "max_tokens"))) is not None:
                    return ended
                continue
            return finish("error", error=f"unexpected stop_reason {sr!r}")

        return finish("max_steps")

    # ------------------------------------------------------------------ tools

    async def _run_tools(self, agent: Agent, ctx: RunContext, uses: list[dict], truncated: bool):
        sequential = any((t := agent.tools.get(u["name"])) and t.side_effect for u in uses)
        if sequential:
            outs = [await self._one_tool(agent, ctx, u, truncated) for u in uses]
        else:
            outs = list(await asyncio.gather(*(self._one_tool(agent, ctx, u, truncated) for u in uses)))
        halt = next((h for _, h, _ in outs if h), None)
        pause = next(((p, Pending(u["id"], u["name"], u.get("input") or {}, p.message, p.data))
                      for u, (_, _, p) in zip(uses, outs) if p), None)
        return [block for block, _, _ in outs], halt, pause

    async def _one_tool(self, agent: Agent, ctx: RunContext, use: dict, truncated: bool):
        """-> (tool_result block, halt_reply | None, Pause | None)"""
        halt: str | None = None
        paused: Pause | None = None
        with ctx.tracer.span("tool", use["name"], agent=agent.name, step=ctx.step, input=use.get("input")) as span:
            span.meta["tool_use_id"] = use["id"]
            if truncated:
                result = ToolResult("Your reply was cut off before this tool call was complete, so it was not run. "
                                    "Call it again with a shorter input.", True)
            else:
                prepared = agent.tools.prepare(use["id"], use["name"], use.get("input"))
                if isinstance(prepared, ToolResult):
                    result = prepared
                else:
                    d = await agent.hooks.run_before_tool(ctx, prepared)
                    if isinstance(d, Block):
                        result, halt = ToolResult(d.reason, True), d.halt_reply
                        span.meta["hook"] = "block"
                    elif isinstance(d, Pause):
                        result = ToolResult("Not done yet: waiting for the user to confirm. Do not tell the user it "
                                            "was done.", False)
                        paused = d
                        span.meta["hook"] = "pause"
                    else:
                        result = await agent.tools.execute(prepared, ctx)
                        result = await agent.hooks.run_after_tool(ctx, prepared, result)
            span.output = result.text()
            span.meta["is_error"] = result.is_error
        block: dict[str, Any] = {"type": "tool_result", "tool_use_id": use["id"], "content": result.text()}
        if result.is_error:
            block["is_error"] = True
        return block, halt, paused
