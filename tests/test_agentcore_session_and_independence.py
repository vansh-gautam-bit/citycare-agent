"""Session store contract + the rule that agentcore stays hospital-free."""

import asyncio
import re
from pathlib import Path

from agentcore import InMemorySessionStore, InMemoryTraceStore, Session, SessionStore, Tracer


def test_session_store_round_trips_json_and_isolates_copies():
    async def go():
        store = InMemorySessionStore()
        assert isinstance(store, SessionStore)
        s = await store.load("a")
        assert s.messages == [] and s.state == {}
        s.messages.append({"role": "user", "content": "hi"})
        s.state["k"] = {"n": 1}
        await store.save(s)
        s.messages.append({"role": "assistant", "content": "mutated after save"})  # must not leak into store
        again = await store.load("a")
        assert len(again.messages) == 1 and again.state == {"k": {"n": 1}}
        await store.delete("a")
        assert (await store.load("a")).messages == []

    asyncio.run(go())


def test_session_lock_serialises_turns_of_one_session():
    async def go():
        store = InMemorySessionStore()
        order = []

        async def turn(name):
            async with store.lock("s"):
                order.append(f"{name}-start")
                await asyncio.sleep(0.02)
                order.append(f"{name}-end")

        await asyncio.gather(turn("a"), turn("b"))
        assert order in (["a-start", "a-end", "b-start", "b-end"], ["b-start", "b-end", "a-start", "a-end"])

    asyncio.run(go())


def test_trace_store_returns_traces_by_session_and_is_bounded():
    ts = InMemoryTraceStore(max_sessions=2)
    for sid in ("a", "b", "c"):
        ts.add(Tracer(sid))
    assert ts.get("a") is None and ts.get("c")[0]["session_id"] == "c"


def test_agentcore_has_no_hospital_code():
    root = Path(__file__).resolve().parent.parent / "agentcore"
    banned_imports = re.compile(r"^\s*(from|import)\s+(agent_service|mock_hospital|evals)\b", re.M)
    banned_words = re.compile(r"hospital|patient|appointment|doctor|city\s?care|hinglish|emergency|\bbill", re.I)
    files = list(root.glob("*.py"))
    assert files
    for f in files:
        src = f.read_text(encoding="utf-8")
        assert not banned_imports.search(src), f"{f.name} imports application code"
        assert not banned_words.search(src), f"{f.name} mentions a hospital concept: {banned_words.search(src).group()}"
