# DESIGN

## 1. Shape of the system

```
POST /chat {session_id, phone, message}
   |
   |  per-session lock (turns of one chat never interleave)
   v
emergency check ── hit ──> safety reply (108, emergency:true, handoff:true)       [code, no model]
   |
   v
handoff lock ── staff already own this chat ──> "staff will reply" (no model)
   |
   v
pending read-back? ── short unambiguous "yes" ──> replay the stored call through the same hooks
   |  no                                          (no router needed)
   v
router (claude-haiku-5-5)  {route, emergency, language, name}
   |      emergency:true -> safety reply (second layer behind the keywords)
   |      human          -> handoff
   v
booking | billing | info   (each an agentcore Agent)
   |   loop: model -> tool_use -> [before_tool hooks] -> tool -> [after_tool hooks] -> model
   v
mock_hospital  (slow, flaky, no auth)
```

`agentcore/` knows nothing about hospitals (a test greps for hospital words and imports). `agent_service/` supplies the
agents, tools, prompts and every hospital rule.

## 2. agentcore (the reusable runtime)

| Piece | File | Decisions |
|---|---|---|
| Tool registry | `tools.py` | Pydantic model → Claude tool definition, in stable order (keeps prompt caching valid). Unknown tool, invalid input, `ToolError`, timeout, any exception → an `is_error` result the model can read. Internal exception text is not shown to the model. Only registered names are callable |
| Loop | `agent.py` | `client.messages.create` directly. Step limit, whole-run timeout, parallel tool calls in one user message (sequential if any call has side effects). Never raises: returns a `RunResult` with a status (`completed`, `paused`, `blocked`, `max_tokens`, `refused`, `max_steps`, `timeout`, `error`) |
| Stop reasons | `agent.py` | `end_turn`/`stop_sequence` finish; `tool_use` runs tools; `pause_turn` resumes; `max_tokens` is reported and a *truncated tool call is never run*; `refusal` is not stored in history; anything else (including `model_context_window_exceeded`) ends cleanly as `error` |
| Model client | `model.py` | Backoff with jitter for 429, 5xx, 529, 408/409, connection errors and timeouts, honouring `retry-after`; client errors fail fast. Works with sync, async and fake clients. Build the SDK client with `max_retries=0` so retries aren't multiplied |
| Hooks | `hooks.py` | `before_model`, `after_model`, `before_tool`, `after_tool`. A before-hook returns `Allow`, `Block(reason, halt_reply=None)` or `Pause(message, data)`. **A hook that raises blocks** (fail closed). `Pause` ends the run with the call *not executed* and the history still valid |
| Resume | `agent.py` | If the history ends with an unanswered `tool_use`, the runner executes it (through all hooks) before calling the model. This is how a confirmed action is replayed deterministically |
| Tracing | `tracing.py` | One span per model/tool call: input, output, duration, tokens (incl. cache read/write), cost. Unknown model price → `None`, never a silent 0 |
| Sessions | `session.py` | `SessionStore` protocol (`load/save/delete/lock`). The in-memory store round-trips through JSON on every call, so a Redis store is a drop-in |

## 3. How each hospital rule is enforced

| Rule (`context.md` §5) | Enforced by | Prompt role |
|---|---|---|
| 1 Own data only | No tool has a `phone` parameter (a test checks every schema). Identity = `TurnDeps.phone` from the request. `own_active_appointment` / `prepare_payment_link` check every id against the caller's own list, and the error never reveals whether someone else has it | Tells the model to refuse politely |
| 2 Confirm before changing | `hooks.write_gate`: first call → `Pause` with a read-back built from verified data and a stored `pending` (with an idempotency key). Only an identical plan + an earlier-turn + `classify_reply == "yes"` is `Allow`ed. Pending lives one message. "Yes but 6pm", "any doctor is fine", digits, "also" are *not* yes | "Call the tool; the system asks" |
| 3 Four-hour rule | `tools._check_four_hours` in every reschedule/cancel prepare, against the hospital clock, on the slot being given up; exactly 4h is allowed. A block ends the run with a handoff. `my_appointments` also marks such appointments `staff_only` | Not decide yourself |
| 4 Emergencies | `safety.detect_emergency` runs first on every message: English, Devanagari and Hinglish, with negation ("no chest pain") and past-tense ("years ago") handling. Router `emergency` flag is a second layer. Reply text is fixed in `replies.py` (no advice) | None needed |
| 5 No medical advice | Prompt, plus `safety.guard_reply` replaces any reply that names a medicine or says "probably nothing" | Main control |
| 6 Facts only from `hospital_info.md` | Info agent's prompt contains the file verbatim and nothing else; its only tool is `request_staff` | Main control |
| 7 Language | `safety.update_language` (sticky: "Yes." keeps Hinglish), router language, and every code-generated reply exists in en / hi / hinglish | Style |
| 8 Handoff | `request_staff` tool (the only way staff are notified), the 4-hour rule, a router `human` route, `unknown` write outcome, and **two failed steps in a row** (`state.failures`). After a handoff the chat is locked to staff | When to call it |
| 9 Never claim what didn't happen | Writes return `done` / `not_done` / `unknown` (below). Payment URLs are appended by code if the model forgot them. `unknown` → handoff text from code, overriding the model's | "Only say done if status is done" |

## 4. Bad days: ambiguous writes (`hospital.py`)

The mock returns the same 503 whether it failed *before* or *after* saving, so every 503 or timeout on a write is ambiguous.

| Write | Strategy | Why |
|---|---|---|
| Book | Retry with the **same idempotency key** (key fixed when the read-back is shown). If retries run out, list the patient's appointments: found → `done`; not found → `not_done`; can't list → `unknown` | A retry can never double-book. Keys are per *proposal*, not per (phone, doctor, time), because the mock returns a *cancelled* booking for a reused key |
| Reschedule | After any failure, `GET` the appointment: already at the new time → `done`. A retry that gets `409` is checked the same way | Naive retry reports "slot taken" for a move that actually worked |
| Cancel | After any failure, `GET`: cancelled → `done`. `not_active` on a retry is checked | Naive retry reports "not active" |
| Payment link | Plain retry (the mock can't save-then-fail here); a link already sent in this chat is reused, not re-created | Worst case is an extra link |
| Reads | Retry transient failures with backoff, then `HospitalUnavailable` → a tool error telling the model not to guess | |
| Lost race (409) | Not retried. The tool computes the nearest free times itself and tells the model the exact call to make; the patient then sees a new read-back | Found in a chaos run: the model listed options in prose instead of proposing one |

## 5. Handoffs between agents

Every agent sees the same plain-text transcript, and session state carries the facts: `name`, `lang`, `last_agent`, `pending`,
`failures`, sent `links`. Tool-call history is *not* shared (different agents have different tools, and history with unknown
tools is fragile), so a new agent re-reads what it needs. The router sees `last_agent` and the last reply, so "Yes." follows the
agent that asked. Scenario 10 relies on this: the name given to billing reaches booking.

## 6. Decisions and trade-offs

- **Pause instead of "propose then confirm" tools.** The read-back is produced by code from verified data, so it always contains
  doctor, day, date and time (or bill and amount) and always matches what will run. The cost: the model's own wording is
  replaced for that one message (the read-back is localised from templates).
- **Deterministic confirm.** On a clear "yes" the stored call is replayed by the runner; the model only phrases the result. The
  model can't forget to call the tool, and can't call something different.
- **Conservative "yes".** Anything with a digit, a change word, or a refusal word is not a confirmation. The patient may have to
  say yes twice in a messy conversation; that is deliberate (rule 2 over convenience).
- **Emergency keywords + LLM.** Keywords are instant, free and reliable for the listed symptoms but miss paraphrases; the router's
  flag catches some of those. Known gap: a message with a past marker ("last year") suppresses the keyword layer even if it
  also describes a current symptom.
- **After a handoff the chat is locked.** Safe and simple; a patient who wants something else must wait for staff.
- **Alternative proposals are code-assisted, not model-decided**, because the model skipped the instruction once (see §4).

## 7. Known limits

- Concurrent turns of one session are serialised by a lock; two *instances* would need a shared store (the Redis-shaped interface exists).
- Reschedule keeps the same doctor (the hospital API can't change it); a doctor change is cancel + book, one confirmed step at a time.
- Scenario 05 under chaos has no spare turn: a lost race on the move (10%) cannot be recovered inside the script. The agent proposes a new slot and waits for a new yes instead of moving without one.
- Traces are in memory (bounded to 1000 sessions); persist them for production.

## 8. Cost and model choice (measured)

All four model roles use `claude-haiku-5-5` ($0.10 / $0.50 per million tokens). Over 51 chaos scenario runs (120 patient messages)
the agents cost **$0.031 in total, $0.00026 per message**, with 2.1 model calls per message. Three things keep it low: the router
is tiny (thinking off, low effort, ~400 output tokens max), the system prompt and tool list are a stable cached prefix (479k cache-read
tokens vs 130k uncached input), and the model is only used where judgement is needed (emergency keywords, confirmation replay, handoff
text and read-backs are code).

Sonnet 5.5 ($2 / $10) would be about 20x per token. We did not measure it because Haiku passes 50 of 51 with the one failure
being a real race, so there was nothing to buy. If hidden scenarios show Haiku slipping on the booking agent (the most tool-heavy),
`BOOKING_MODEL=claude-sonnet-5-5` is a one-line switch and would still cost roughly half a cent per message.
