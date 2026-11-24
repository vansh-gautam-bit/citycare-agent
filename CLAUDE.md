# CLAUDE.md: City Care front-desk agent

Read `context.md` first (product, the 9 hospital rules, the mock hospital). `DESIGN.md` explains how this repo implements them.

## Layout

| Path | What | Rule |
|---|---|---|
| `agentcore/` | Generic agent runtime: loop, tools, hooks, tracing, sessions | **No hospital words or imports.** `tests/test_agentcore_session_and_independence.py` fails if any appear |
| `agent_service/` | City Care agents on top of agentcore | Hospital rules live here, **in code** (`tools.py`, `hooks.py`, `safety.py`) |
| `mock_hospital/` | Fake hospital system | **Never change how it behaves** |
| `evals/scenarios/01`-`10` | Given scenarios | **Never edit.** Ours are `11_` and up |
| `evals/run_evals.py` | Runner | Don't change how existing checks work |

## Commands (Windows; Python is in `.venv`)

```
.venv\Scripts\python -m pytest -q                       # must pass with NO API key
.venv\Scripts\python -m uvicorn mock_hospital.app:app --port 8001
.venv\Scripts\python -m uvicorn agent_service.app:app --port 8000     # no auto-reload: restart after code changes
.venv\Scripts\python -m evals.run_evals --runs 3 --chaos
```

## Principles (the reasons behind the code)

1. **Safety is enforced in code, never only in a prompt.** If a rule can be a hook or a tool check, it is. A prompt may help the model behave; assume it will sometimes not.
2. **Identity comes from `phone` on the request.** No tool takes a phone number. Every appointment or bill id is checked against the caller's own records before use.
3. **No change without a confirmed read-back.** Change tools pause (`Pause`) with a read-back generated from verified data; only an identical, earlier-turn, clear "yes" lets the call through (`hooks.write_gate`, `safety.classify_reply`).
4. **Never claim what didn't happen.** A write resolves to `done`, `not_done` or `unknown`. `unknown` is never reported as success and hands off to staff. Staff handoff happens only through the `request_staff` tool or a code rule: a model *saying* it is connecting staff does nothing.
5. **Every 503 or timeout on a write is ambiguous** (the mock saves, then fails, 5% of the time). Resolve by re-reading real state (`hospital.py`); book uses idempotency keys.
6. **When the model fails a rule in an eval, fix the cause in code or data, not by tuning the prompt to the scenario.** Hidden scenarios will differ from the visible ones.

## Working rules

- Test after each component. Unit tests use `tests/fake_claude.py` and the in-process mock; they cost nothing.
- Use the hospital clock (`GET /now`), never the machine clock.
- Check Claude API behaviour in the docs, not memory (Claude 5 models reject forced `tool_choice` and `temperature`; thinking is on by default).
- Budget: $30 for everything. Haiku 5.5 for all agents so far (measured, see `EVALS.md`). Check `/cost` in Claude Code.
- Never commit `.env` or an API key. `tools/pack_sessions.py` refuses to run if it finds one.
- Export each Claude Code session to `claude-sessions/NN-name.txt` (`/export`) before `/clear`.
