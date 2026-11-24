"""The confirmation gate: no change is made unless the patient has confirmed an identical read-back.

How it works (all in code, the model cannot talk its way around it):

  1. The model calls a change tool (book_appointment, ...). This hook runs first.
  2. It validates and builds a Plan from verified data (ownership, 4-hour rule, free slot).
        invalid / not theirs  -> the call is blocked and the model is told why
        inside 4 hours        -> blocked, and the run ends with a handoff to staff
  3. If the patient has NOT just confirmed this exact plan: the call is paused. The patient sees a
     read-back generated here from the verified plan, and the proposal is stored as `pending`.
  4. Next turn, a short unambiguous "yes" (service.py) replays the stored call. This hook then sees
     the confirmation matches the plan exactly and allows it. A pending action is valid for the very
     next message only, and never on the turn it was proposed.
"""

from __future__ import annotations

import uuid

from agentcore import Allow, Block, Pause, RunContext, ToolCall, ToolError, ToolResult

from . import replies
from .domain import HandoffRequired, deps_of, lang_of
from .hospital import HospitalError, HospitalUnavailable
from .tools import PREPARE, note_failure


async def write_gate(ctx: RunContext, call: ToolCall):
    prepare = PREPARE.get(call.name)
    if prepare is None:
        return Allow()  # read-only tool
    deps, lang = deps_of(ctx), lang_of(ctx)
    try:
        plan = await prepare(call.args, ctx)
    except ToolError as e:
        return Block(str(e))
    except HandoffRequired as h:
        deps.handoff = h.kind
        return Block("This must be handled by hospital staff, not by chat.",
                     halt_reply=replies.handoff_reply(h.kind, lang))
    except HospitalUnavailable:
        note_failure(ctx)
        return Block("The hospital system is not responding, so this could not be checked. Do not guess; "
                     "tell the patient you could not check right now.")
    except HospitalError as e:
        return Block(f"The hospital system returned an error ({e.code}); nothing was changed.")

    confirmed = deps.confirmed
    if confirmed and confirmed.get("key") == plan.key and confirmed.get("turn", 0) < ctx.state.get("turn", 0):
        plan.idem = confirmed["idem"]
        deps.plans[call.name] = plan
        return Allow()

    # Not confirmed yet: remember exactly what is being proposed, and show the patient the read-back.
    ctx.state["pending"] = {"tool": call.name, "args": call.raw_input, "key": plan.key, "agent": ctx.agent_name,
                            "turn": ctx.state.get("turn", 0), "idem": uuid.uuid4().hex}
    prefix = replies.SLOT_TAKEN_NOTICE.get(lang, replies.SLOT_TAKEN_NOTICE["en"]) if "slot_taken" in deps.notices else ""
    return Pause(prefix + replies.readback(plan.kind, lang, plan.fields), data=ctx.state["pending"])


def reset_failures_on_success(ctx: RunContext, call: ToolCall, result: ToolResult):
    """Two failures in a row hand the chat to staff, so a success breaks the streak."""
    if not result.is_error:
        ctx.state["failures"] = 0
    return None
