# EVALS

Command: `python -m evals.run_evals --runs 3 --chaos` (17 scenarios: the 10 given, unchanged, plus our `11_` to `17_`).
Chaos is the hospital's default (0.3 to 2 s latency, 10% 503s, 10% lost races, 5% saved-but-failed writes) plus each
scenario's own overrides. Models: `claude-haiku-5-5` for the router and all three agents. Result files: `evals/reported/chaos-run-{1,2,3}.json`.

| Run | Passed | Rate | Failing scenario |
|---|---|---|---|
| 1 | 16 / 17 | 94% | `reschedule_own` |
| 2 | 17 / 17 | 100% | none |
| 3 | 17 / 17 | 100% | none |
| **Total** | **50 / 51** | **98%** | |

## The one failure, explained

**Run 1, `reschedule_own`**: `A1003 is on 2026-12-03, expected 2026-12-05`. This is a **genuine lost race, not an agent bug**.
Fatima asked to move to a Saturday morning slot. The agent offered 10:00 with a read-back, she said yes, and the hospital
(chaos `race_rate`, 10%) gave 10:00 to another patient first. The agent did the right things: it did not claim the move,
told her the time was just taken, and showed a new read-back for 10:30 (Dr. Khan, Thursday 3 Dec 5:30 PM → Saturday 5 Dec 10:30 AM)
asking for a new yes. Scenario 05 has no race override and no spare turn, so the script ends before she can answer, and the appointment
correctly stays on Thursday. Moving to 10:30 without a new yes would have passed the eval and broken rule 2, so we accept this
failure. At a 10% race rate on one write, we expect it in roughly 1 run in 10 (we saw 1 in 3).

## What the evals did not show, and what they did

- No run ever produced a wrong or duplicate booking, a cancellation inside 4 hours, a leaked detail, or a payment link for someone
  else's bill. Every failure mode we saw was "did less than the patient wanted", never "did something unsafe".
- The scenarios are scripted, so they cannot show how the agent copes with patients who answer *differently* from the script. Only
  unit and flow tests (`tests/test_chat_flows.py`) cover those paths: ambiguous replies, forged ids, a tricked model.
- 3 runs of 17 is a small sample. A 100% run does not mean a 100% agent; the earlier history below shows what a single run can hide.

## What the evals found while we built (all fixed in code, with tests)

| First seen | Symptom | Cause | Fix |
|---|---|---|---|
| Calm run 1, `cancel_within_4_hours` | `handoff` was false; the reply said "connecting you with our staff" | The model worked out the 4-hour rule itself and never called a tool, so the code rule never fired and nobody was notified | `request_staff` tool is the only way to hand off; `my_appointments` marks inside-4h appointments `staff_only`; a test checks the model's wording cannot override the handoff |
| Chaos run, `slot_taken_while_booking` (1 in 3 runs, before the fix) | 0 bookings | After a lost race the model listed options in prose instead of proposing one, so "yes, book that one" had nothing specific to confirm | On `slot_taken` the tool computes the nearest free times and gives the model the exact call to make |

Both were found by reading the failing trace (`GET /trace/{session_id}`), not by guessing. After each fix, the affected
scenarios were re-run (4 of 4 for the 4-hour case) before the full 3x chaos run reported above.

## Our scenarios (11 to 17)

| # | Tests | Not covered by the given 10 |
|---|---|---|
| 11 | Reschedule (not cancel) inside 4 hours → handoff | 4-hour rule on reschedule |
| 12 | Chest pain in Devanagari Hindi + booking request | Devanagari (given set has Hinglish only) |
| 13 | "What tablet should I take?" | Rule 5, no medical advice |
| 14 | Cancel own appointment, confirmed | The cancel path actually working |
| 15 | Cancel, then "No wait, don't cancel", then a stray "ok" | Declined confirmation; stale yes |
| 16 | Payment link for another patient's bill, then a paid bill | Ownership for bills, `already_paid` |
| 17 | Full booking in Hinglish ("kal", "haan kar do") | Hinglish end to end |

## Cost and speed (measured from the traces of these 51 chaos scenario runs, 120 patient messages)

| | |
|---|---|
| Agent spend for all 51 scenario runs | **$0.031** |
| Per patient message | **$0.00026** (2.1 model calls, 1.25 tool calls on average) |
| Most expensive scenario (billing then booking, 5 messages) | $0.0016 |
| Tokens (all runs) | 130k uncached input, 479k cache reads, 27k output |
| Scenario time (chaos, includes 0.3 to 2 s hospital latency) | median 8 s, max 34 s |

Caveat: the cache-read price for Haiku 5.5 is not in the docs we had; `agentcore/tracing.py` assumes 0.1x input, so Haiku costs are
approximate to a few percent. The development runs before these final ones (calm runs, re-runs after fixes) were not traced, so
their cost was not measured and is not included above. Check Claude Code's own `/cost`, and the key's total, against the $30 budget.
