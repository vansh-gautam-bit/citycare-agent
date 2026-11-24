"""agentcore loop, tools, hooks, tracing. No API key needed (FakeClaude)."""

import asyncio

import anthropic
import httpx
import pytest
from pydantic import BaseModel

from agentcore import (Agent, Allow, Block, Hooks, ModelClient, Pause, RetryPolicy, RunContext, Runner,
                       ToolError, ToolRegistry, ToolResult)
from tests.fake_claude import FakeClaude, text, tool_use, tool_uses


class Echo(BaseModel):
    msg: str
    times: int = 1


async def _nosleep(_):
    return None


def make(script, registry=None, hooks=None, **agent_kw):
    reg = registry or ToolRegistry()
    fake = FakeClaude(script)
    mc = ModelClient(fake, retry=RetryPolicy(max_retries=2, base_delay_s=0), sleep=_nosleep)
    agent = Agent(name="t", model="claude-haiku-5-5", system="sys", tools=reg, hooks=hooks or Hooks(), **agent_kw)
    return fake, Runner(mc), agent


def run(runner, agent, user="hi", state=None, **kw):
    ctx = RunContext(session_id="s1", state=state or {})
    msgs = [{"role": "user", "content": user}]
    return asyncio.run(runner.run(agent, msgs, ctx, **kw)), ctx


def echo_registry(side_effect=False):
    reg = ToolRegistry()
    calls = []

    @reg.tool(description="echo", input_model=Echo, side_effect=side_effect)
    def echo(args: Echo, ctx: RunContext):
        calls.append(args.msg)
        return {"echo": args.msg * args.times}

    return reg, calls


# ---------------------------------------------------------------- registry

def test_definition_is_claude_shaped():
    reg, _ = echo_registry()
    d = reg.definitions()[0]
    assert d["name"] == "echo" and d["input_schema"]["required"] == ["msg"]
    assert d["input_schema"]["additionalProperties"] is False
    assert "title" not in d["input_schema"]


def test_invalid_input_unknown_tool_and_exceptions_become_error_results():
    reg = ToolRegistry()

    @reg.tool(description="boom", input_model=Echo)
    def boom(args):
        raise RuntimeError("secret internal detail")

    @reg.tool(description="polite", input_model=Echo)
    def polite(args):
        raise ToolError("Not allowed because X.")

    r = reg.prepare("1", "echo_missing", {})
    assert r.is_error and "Unknown tool" in r.content
    r = reg.prepare("1", "boom", {"times": "x"})
    assert r.is_error and "msg" in r.content and "times" in r.content
    call = reg.prepare("1", "boom", {"msg": "a"})
    res = asyncio.run(reg.execute(call, RunContext("s")))
    assert res.is_error and "secret" not in res.content  # internals not leaked to the model
    res = asyncio.run(reg.execute(reg.prepare("1", "polite", {"msg": "a"}), RunContext("s")))
    assert res.content == "Not allowed because X."


def test_tool_timeout():
    reg = ToolRegistry()

    @reg.tool(description="slow", input_model=Echo, timeout_s=0.05)
    async def slow(args):
        await asyncio.sleep(1)

    res = asyncio.run(reg.execute(reg.prepare("1", "slow", {"msg": "a"}), RunContext("s")))
    assert res.is_error and "timed out" in res.content


# ---------------------------------------------------------------- loop

def test_tool_loop_and_history_shape():
    reg, calls = echo_registry()
    fake, runner, agent = make([tool_use("echo", {"msg": "hey", "times": 2}), text("done")], reg)
    res, _ = run(runner, agent)
    assert res.status == "completed" and res.text == "done" and calls == ["hey"]
    second = fake.messages.calls[1]["messages"]
    assert second[-1]["role"] == "user" and second[-1]["content"][0]["type"] == "tool_result"
    assert "heyhey" in second[-1]["content"][0]["content"]
    assert [m["role"] for m in res.messages] == ["user", "assistant", "user", "assistant"]
    assert res.new_from == 1


def test_parallel_tool_calls_return_one_user_message():
    reg, calls = echo_registry()
    fake, runner, agent = make([tool_uses([("echo", {"msg": "a"}), ("echo", {"msg": "b"})]), text("ok")], reg)
    res, _ = run(runner, agent)
    results = fake.messages.calls[1]["messages"][-1]["content"]
    assert len(results) == 2 and sorted(calls) == ["a", "b"]


def test_step_limit():
    reg, _ = echo_registry()
    fake, runner, agent = make([tool_use("echo", {"msg": "x"}) for _ in range(5)], reg, max_steps=3)
    res, _ = run(runner, agent)
    assert res.status == "max_steps" and len(fake.messages.calls) == 3


def test_max_tokens_refusal_pause_turn_and_unknown_stop_reason():
    _, runner, agent = make([text("cut", stop_reason="max_tokens")])
    res, _ = run(runner, agent)
    assert res.status == "max_tokens" and res.text == "cut"

    _, runner, agent = make([text("", stop_reason="refusal")])
    res, _ = run(runner, agent)
    assert res.status == "refused" and len(res.messages) == 1  # refused turn not stored

    _, runner, agent = make([text("partial", stop_reason="pause_turn"), text("finished")])
    res, _ = run(runner, agent)
    assert res.status == "completed" and res.text == "finished"

    # a real stop reason the loop has no special handling for must end cleanly, not crash or spin
    _, runner, agent = make([text("x", stop_reason="model_context_window_exceeded")])
    res, _ = run(runner, agent)
    assert res.status == "error" and "model_context_window_exceeded" in res.error


def test_truncated_tool_call_is_not_executed():
    reg, calls = echo_registry()
    msg = tool_use("echo", {"msg": "x"})
    msg.stop_reason = "max_tokens"
    _, runner, agent = make([msg, text("ok")], reg)
    res, _ = run(runner, agent)
    assert calls == [] and res.status == "completed"


def test_run_timeout():
    class Slow:
        class messages:
            @staticmethod
            async def create(**kw):
                await asyncio.sleep(2)

    mc = ModelClient(Slow, retry=RetryPolicy(max_retries=0))
    agent = Agent(name="t", model="claude-haiku-5-5", system="s")
    res = asyncio.run(Runner(mc).run(agent, [{"role": "user", "content": "hi"}], RunContext("s"), timeout_s=0.1))
    assert res.status == "timeout"


# ---------------------------------------------------------------- retries

def _status_error(cls, code, headers=None):
    req = httpx.Request("POST", "https://api.example")
    return cls("err", response=httpx.Response(code, request=req, headers=headers or {}), body=None)


def test_retries_rate_limit_then_succeeds_and_records_attempts():
    err = _status_error(anthropic.RateLimitError, 429, {"retry-after": "0"})
    _, runner, agent = make([err, anthropic.APIConnectionError(request=httpx.Request("POST", "https://x")),
                             text("hello")])
    res, ctx = run(runner, agent)
    assert res.status == "completed" and ctx.tracer.spans[0].meta["attempts"] == 3


def test_gives_up_after_retry_budget_and_does_not_retry_client_errors():
    boom = _status_error(anthropic.InternalServerError, 500)
    fake, runner, agent = make([boom, boom, boom, boom])
    res, _ = run(runner, agent)
    assert res.status == "error" and len(fake.messages.calls) == 3  # 1 + max_retries(2)

    bad = _status_error(anthropic.BadRequestError, 400)
    fake, runner, agent = make([bad, text("never")])
    res, _ = run(runner, agent)
    assert res.status == "error" and len(fake.messages.calls) == 1


# ---------------------------------------------------------------- hooks

def test_before_tool_block_stops_the_tool_and_tells_the_model():
    reg, calls = echo_registry()
    hooks = Hooks(before_tool=[lambda ctx, call: Block("Policy says no.")])
    fake, runner, agent = make([tool_use("echo", {"msg": "x"}), text("sorry")], reg, hooks)
    res, _ = run(runner, agent)
    assert calls == [] and res.text == "sorry"
    tr = fake.messages.calls[1]["messages"][-1]["content"][0]
    assert tr["is_error"] is True and tr["content"] == "Policy says no."


def test_block_with_halt_reply_ends_the_run():
    reg, calls = echo_registry()
    hooks = Hooks(before_tool=[lambda ctx, call: Block("no", halt_reply="Handing you to staff.")])
    fake, runner, agent = make([tool_use("echo", {"msg": "x"}), text("must not be asked")], reg, hooks)
    res, _ = run(runner, agent)
    assert res.status == "blocked" and res.text == "Handing you to staff." and len(fake.messages.calls) == 1
    assert res.messages[-1]["role"] == "assistant"


def test_pause_does_not_execute_and_returns_pending():
    reg, calls = echo_registry(side_effect=True)

    async def gate(ctx, call):  # async hooks work too
        return Pause("Shall I do it? Reply yes.", data={"why": "confirm"})

    fake, runner, agent = make([tool_use("echo", {"msg": "x"}), text("never")], reg, Hooks(before_tool=[gate]))
    res, _ = run(runner, agent)
    assert res.status == "paused" and res.text == "Shall I do it? Reply yes." and calls == []
    assert res.pending.tool == "echo" and res.pending.args == {"msg": "x"} and res.pending.data == {"why": "confirm"}
    roles = [m["role"] for m in res.messages]
    assert roles == ["user", "assistant", "user", "assistant"]  # valid alternation for the next turn


def test_hook_that_raises_fails_closed():
    reg, calls = echo_registry()

    def bad(ctx, call):
        raise ValueError("oops")

    _, runner, agent = make([tool_use("echo", {"msg": "x"}), text("ok")], reg, Hooks(before_tool=[bad]))
    run(runner, agent)
    assert calls == []


def test_after_tool_can_replace_result_and_hooks_see_validated_args_and_ctx():
    reg, _ = echo_registry()
    seen = {}

    def before(ctx, call):
        seen["args"] = call.args
        seen["who"] = ctx.state["who"]
        return Allow()

    hooks = Hooks(before_tool=[before], after_tool=[lambda ctx, call, r: ToolResult("redacted")])
    fake, runner, agent = make([tool_use("echo", {"msg": "x"}), text("ok")], reg, hooks)
    run(runner, agent, state={"who": "p1"})
    assert seen["args"].msg == "x" and seen["who"] == "p1"
    assert fake.messages.calls[1]["messages"][-1]["content"][0]["content"] == "redacted"


def test_before_model_can_block():
    hooks = Hooks(before_model=[lambda ctx, req: Block("budget", halt_reply="Too long.")])
    fake, runner, agent = make([text("never")], hooks=hooks)
    res, _ = run(runner, agent)
    assert res.status == "blocked" and res.text == "Too long." and fake.messages.calls == []


# ---------------------------------------------------------------- tracing + request shape

def test_trace_records_model_and_tool_spans_with_tokens_and_cost():
    reg, _ = echo_registry()
    _, runner, agent = make([tool_use("echo", {"msg": "x"}, input_tokens=1_000_000, output_tokens=0),
                             text("ok", input_tokens=0, output_tokens=1_000_000)], reg)
    res, ctx = run(runner, agent)
    kinds = [s.kind for s in ctx.tracer.spans]
    assert kinds == ["model", "tool", "model"]
    tool = ctx.tracer.spans[1]
    assert tool.input == {"msg": "x"} and "echo" in tool.output and tool.duration_ms >= 0
    # fake responses report model "fake-model" -> unknown price -> cost is None, not a silent 0
    assert res.cost_usd is None and ctx.tracer.totals()["cost_complete"] is False
    assert res.usage.input_tokens == 1_000_000 and res.usage.output_tokens == 1_000_000


def test_cost_uses_price_table_for_known_models():
    from agentcore import Usage, cost_usd
    u = Usage(input_tokens=1_000_000, output_tokens=1_000_000, cache_read_tokens=1_000_000)
    assert cost_usd("claude-haiku-5-5", u) == pytest.approx(0.10 + 0.50 + 0.01)
    assert cost_usd("claude-sonnet-5-5", u) == pytest.approx(2 + 10 + 0.10)
    assert cost_usd("nope", u) is None


def test_request_shape_caching_and_no_forced_tool_choice():
    reg, _ = echo_registry()
    fake, runner, agent = make([text("ok")], reg, dynamic_system=lambda ctx: "Today is X.")
    run(runner, agent)
    kw = fake.messages.calls[0]
    assert kw["system"][0]["cache_control"] == {"type": "ephemeral"} and "cache_control" not in kw["system"][1]
    assert kw["system"][1]["text"] == "Today is X."
    assert kw["tools"][-1]["cache_control"] == {"type": "ephemeral"}
    assert "tool_choice" not in kw and "temperature" not in kw


# ---------------------------------------------------------------- resume

def test_resume_runs_unanswered_tool_calls_through_hooks_then_asks_the_model():
    reg, calls = echo_registry(side_effect=True)
    seen = []
    hooks = Hooks(before_tool=[lambda ctx, call: seen.append(call.name)])
    fake, runner, agent = make([text("All done.")], reg, hooks)
    history = [{"role": "user", "content": "yes"},
               {"role": "assistant", "content": [{"type": "tool_use", "id": "toolu_x", "name": "echo", "input": {"msg": "m"}}]}]
    ctx = RunContext(session_id="s1")
    res = asyncio.run(runner.run(agent, history, ctx))
    assert calls == ["m"] and seen == ["echo"] and res.text == "All done."
    sent = fake.messages.calls[0]["messages"]
    assert sent[-1]["content"][0]["tool_use_id"] == "toolu_x"  # the result was in the model's very first request


def test_resume_respects_a_blocking_hook():
    reg, calls = echo_registry(side_effect=True)
    hooks = Hooks(before_tool=[lambda ctx, call: Block("no", halt_reply="Staff will help.")])
    fake, runner, agent = make([text("never")], reg, hooks)
    history = [{"role": "user", "content": "yes"},
               {"role": "assistant", "content": [{"type": "tool_use", "id": "toolu_x", "name": "echo", "input": {"msg": "m"}}]}]
    res = asyncio.run(runner.run(agent, history, RunContext(session_id="s1")))
    assert calls == [] and res.status == "blocked" and fake.messages.calls == []
