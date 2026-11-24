"""The /chat pipeline.

    message -> emergency check (code) -> [handoff lock] -> router (Haiku) -> specialist agent (agentcore)
               \\-> emergency reply            \\-> staff reply                  booking | billing | info

Every path returns a valid ChatResponse. Nothing a model says can skip the emergency check, the
ownership and 4-hour checks, or the confirmation gate: those run in code (safety.py, tools.py, hooks.py).
Session facts (patient name, language, the pending action, failure count) live in the session state, so
they survive a switch from one agent to another. Each agent sees the same plain-text transcript.
"""

from __future__ import annotations

import json
import logging
import os
import re
import uuid
from dataclasses import dataclass, field

import anthropic

from agentcore import (Agent, Hooks, InMemorySessionStore, InMemoryTraceStore, ModelClient, RetryPolicy, RunContext,
                       Runner, SessionStore, Tracer)

from . import prompts, replies, safety
from .domain import TurnDeps
from .hooks import reset_failures_on_success, write_gate
from .hospital import Hospital, HospitalError
from .schemas import ChatRequest, ChatResponse
from .tools import billing_tools, booking_tools, info_tools

log = logging.getLogger("agent_service")

PHONE_RE = re.compile(r"^[6-9]\d{9}$")  # same rule as the hospital system
ROUTES = ("booking", "billing", "info", "human")
TRANSCRIPT_LIMIT = 24  # messages kept per session


@dataclass
class Models:
    router: str = field(default_factory=lambda: os.getenv("ROUTER_MODEL", "claude-haiku-5-5"))
    booking: str = field(default_factory=lambda: os.getenv("BOOKING_MODEL", "claude-haiku-5-5"))
    billing: str = field(default_factory=lambda: os.getenv("BILLING_MODEL", "claude-haiku-5-5"))
    info: str = field(default_factory=lambda: os.getenv("INFO_MODEL", "claude-haiku-5-5"))


@dataclass
class Out:
    reply: str
    agent: str
    handoff: bool = False
    emergency: bool = False


class ChatService:
    def __init__(self, hospital: Hospital, model_client: ModelClient, *, models: Models | None = None,
                 sessions: SessionStore | None = None, traces: InMemoryTraceStore | None = None,
                 info_text: str | None = None, run_timeout_s: float = 120.0):
        self.hospital = hospital
        self.runner = Runner(model_client)
        self.sessions = sessions or InMemorySessionStore()
        self.traces = traces or InMemoryTraceStore()
        self.run_timeout_s = run_timeout_s
        m = models or Models()
        gate = Hooks(before_tool=[write_gate], after_tool=[reset_failures_on_success])
        effort = {"output_config": {"effort": "low"}}
        self.router = Agent("router", m.router, prompts.ROUTER_SYSTEM, max_steps=1, max_tokens=400,
                            params={"thinking": {"type": "disabled"}, **effort})
        self.agents = {
            "booking": Agent("booking", m.booking, prompts.BOOKING_SYSTEM, tools=booking_tools(), hooks=gate,
                             max_steps=8, max_tokens=2048, dynamic_system=prompts.dynamic_system, params=dict(effort)),
            "billing": Agent("billing", m.billing, prompts.BILLING_SYSTEM, tools=billing_tools(), hooks=gate,
                             max_steps=6, max_tokens=2048, dynamic_system=prompts.dynamic_system, params=dict(effort)),
            "info": Agent("info", m.info, prompts.INFO_SYSTEM + (info_text or prompts.hospital_info()), tools=info_tools(), max_steps=3,
                          max_tokens=1024, dynamic_system=prompts.dynamic_system, params=dict(effort)),
        }

    @classmethod
    def from_env(cls) -> "ChatService":
        client = anthropic.AsyncAnthropic(max_retries=0)  # agentcore owns retries
        return cls(Hospital(), ModelClient(client, retry=RetryPolicy(max_retries=4), timeout_s=60))

    # ------------------------------------------------------------------ entry point

    async def handle(self, req: ChatRequest) -> ChatResponse:
        tracer = Tracer(req.session_id)
        async with self.sessions.lock(req.session_id):
            session = await self.sessions.load(req.session_id)
            st = session.state
            st.setdefault("turn", 0)
            try:
                out = await self._turn(req, session, tracer)
            except Exception:  # noqa: BLE001 - /chat must always answer
                log.exception("unhandled error in chat turn")
                out = Out(replies.handoff_reply("trouble", st.get("lang", "en")), "human", handoff=True)
            session.messages = (session.messages + [{"role": "user", "content": req.message},
                                                    {"role": "assistant", "content": out.reply}])[-TRANSCRIPT_LIMIT:]
            while session.messages and session.messages[0]["role"] != "user":
                session.messages.pop(0)
            st["last_agent"] = out.agent if out.agent in self.agents else st.get("last_agent")
            await self.sessions.save(session)
        self.traces.add(tracer)
        return ChatResponse(reply=out.reply, agent=out.agent, handoff=out.handoff, emergency=out.emergency,
                            trace_id=tracer.trace_id)

    # ------------------------------------------------------------------ one turn

    async def _turn(self, req: ChatRequest, session, tracer: Tracer) -> Out:
        st = session.state
        st["turn"] += 1
        lang = safety.update_language(st, req.message)

        if not PHONE_RE.match(req.phone):
            return self._handoff(st, "bad_phone", lock=True)
        if st.get("handoff_locked"):  # staff already own this chat
            return Out(replies.handoff_reply("locked", lang), "human", handoff=True)

        # 1. Emergency check: first, on every message, before any model sees it.
        with tracer.span("check", "emergency_keywords", input=req.message) as span:
            kind = safety.detect_emergency(req.message)
            span.output = kind
        if kind:
            return self._emergency(st, kind)

        deps = TurnDeps(hospital=self.hospital, phone=req.phone, now=await self.hospital.now())
        ctx = RunContext(session_id=req.session_id, state=st, deps=deps, tracer=tracer)

        # 2. Did the patient just confirm a read-back? Then no routing is needed: replay the stored call.
        pending = st.get("pending")
        confirm = None
        if pending and pending.get("turn", 0) < st["turn"] and safety.classify_reply(req.message) == "yes":
            confirm = pending
            agent_name = pending["agent"]
        else:
            st["pending"] = None  # a pending action is only valid for the very next message
            route = await self._route(ctx, session.messages, req.message)
            lang = safety.update_language(st, req.message, route.get("language"))
            if route.get("name") and not st.get("name"):
                st["name"] = route["name"]
            if route["emergency"]:  # the router's judgement is a second layer behind the keyword check
                return self._emergency(st, None)
            if route["route"] == "human":
                return self._handoff(st, "generic", lock=True)
            agent_name = route["route"]

        agent = self.agents[agent_name]
        msgs = session.messages + [{"role": "user", "content": req.message}]
        if confirm:
            deps.confirmed = confirm
            msgs.append({"role": "assistant", "content": [
                {"type": "tool_use", "id": "toolu_" + uuid.uuid4().hex[:20], "name": confirm["tool"], "input": confirm["args"]}]})

        failures_before = st.get("failures", 0)
        res = await self.runner.run(agent, msgs, ctx, timeout_s=self.run_timeout_s)
        return self._finish(st, agent_name, res, deps, lang, failures_before)

    # ------------------------------------------------------------------ helpers

    def _finish(self, st: dict, agent_name: str, res, deps: TurnDeps, lang: str, failures_before: int) -> Out:
        if deps.handoff:
            return self._handoff(st, deps.handoff, lock=True)
        if res.status in ("completed", "paused", "blocked") and res.text.strip():
            text = res.text.strip()
            if res.status == "completed":
                if safety.guard_reply(text):  # last line of defence against medical advice
                    text = replies.NO_ADVICE.get(lang, replies.NO_ADVICE["en"])
                for url in deps.new_links:
                    if url not in text:
                        text += f"\n{url}"
            if st.get("failures", 0) >= 2:  # rule 8: failed twice in a row on the same request
                return self._handoff(st, "trouble", lock=True)
            if st.get("failures", 0) == failures_before:  # a turn with no new failure ends the streak
                st["failures"] = 0
            return Out(text, agent_name)
        # The run did not produce an answer (model error, timeout, step limit, refusal, empty reply).
        log.warning("run ended with status=%s error=%s", res.status, res.error)
        st["failures"] = st.get("failures", 0) + 1
        if st["failures"] >= 2:
            return self._handoff(st, "trouble", lock=True)
        return Out(replies.RETRY.get(lang, replies.RETRY["en"]), agent_name)

    def _handoff(self, st: dict, kind: str, *, lock: bool) -> Out:
        st["pending"] = None
        if lock:
            st["handoff_locked"] = True
        return Out(replies.handoff_reply(kind, st.get("lang", "en")), "human", handoff=True)

    def _emergency(self, st: dict, kind: str | None) -> Out:
        st["pending"] = None  # stop everything else
        st["emergency"] = True
        return Out(replies.emergency_reply(st.get("lang", "en"), kind), "safety", handoff=True, emergency=True)

    async def _route(self, ctx: RunContext, transcript: list[dict], message: str) -> dict:
        st = ctx.state
        last = next((m["content"] for m in reversed(transcript) if m["role"] == "assistant"), "")
        content = (f"last_agent: {st.get('last_agent') or 'none'}\n"
                   f"last_assistant_message: {str(last)[:300]}\npatient_message: {message}")
        fallback = {"route": st.get("last_agent") or "human", "emergency": False, "language": None, "name": None}
        res = await self.runner.run(self.router, [{"role": "user", "content": content}],
                                    RunContext(session_id=ctx.session_id, tracer=ctx.tracer), timeout_s=30)
        if res.status != "completed":
            log.warning("router failed: %s %s", res.status, res.error)
            return fallback
        return parse_route(res.text, fallback)


def parse_route(text: str, fallback: dict) -> dict:
    m = re.search(r"\{.*\}", text, re.S)
    try:
        d = json.loads(m.group(0)) if m else {}
    except ValueError:
        d = {}
    route = d.get("route")
    if route not in ROUTES:
        return fallback
    name = d.get("name")
    name = " ".join(name.split())[:60] if isinstance(name, str) and re.fullmatch(r"[\w .'-]{2,60}", name.strip()) else None
    lang = d.get("language") if d.get("language") in ("en", "hi", "hinglish") else None
    return {"route": route, "emergency": d.get("emergency") is True, "language": lang, "name": name}
