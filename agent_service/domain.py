"""Per-turn objects shared by the City Care tools and hooks."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from agentcore import RunContext

from .hospital import Hospital

IST = timezone(timedelta(hours=5, minutes=30))
FOUR_HOURS = timedelta(hours=4)


@dataclass
class Plan:
    """A change the patient has been (or is about to be) asked to confirm. Built only from verified data."""

    kind: str  # book | reschedule | cancel | payment_link
    key: dict  # canonical identity of the action; a confirmation only counts for an identical key
    fields: dict  # what the read-back shows
    idem: str = ""  # idempotency key for bookings, fixed when the read-back is shown


@dataclass
class TurnDeps:
    """Handed to every tool and hook through RunContext.deps. `phone` is the verified WhatsApp number."""

    hospital: Hospital
    phone: str
    now: datetime
    confirmed: dict | None = None  # the pending action the patient just said yes to (this turn only)
    plans: dict[str, Plan] = field(default_factory=dict)  # tool name -> plan the gate approved
    notices: list[str] = field(default_factory=list)  # e.g. "slot_taken", shown before the next read-back
    new_links: list[str] = field(default_factory=list)  # payment URLs created this turn
    handoff: str | None = None  # set when the chat must go to staff (a key of replies.HANDOFF)


class HandoffRequired(Exception):
    def __init__(self, kind: str):
        super().__init__(kind)
        self.kind = kind


def deps_of(ctx: RunContext) -> TurnDeps:
    return ctx.deps


def lang_of(ctx: RunContext) -> str:
    return ctx.state.get("lang", "en")
