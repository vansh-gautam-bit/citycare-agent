"""City Care tools. Hospital rules live here, in code.

  * No tool takes a phone number. The caller's verified number comes from TurnDeps.
  * Every appointment id and bill id is checked against the caller's own records before use.
  * Changes (book / reschedule / cancel / payment link) are split in two:
      prepare_*  read-only: validates, enforces ownership and the 4-hour rule, builds a Plan
      execute_*  does the write, only for a Plan the confirmation gate (hooks.py) approved
    The tool function the model calls is a thin wrapper around execute_*.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from functools import wraps
from typing import Awaitable, Callable, Literal

from pydantic import BaseModel, Field

from agentcore import RunContext, Tool, ToolError, ToolRegistry, ToolResult

from .domain import FOUR_HOURS, IST, HandoffRequired, Plan, deps_of
from .hospital import Hospital, HospitalError, HospitalUnavailable

DATE = r"^\d{4}-\d{2}-\d{2}$"
TIME = r"^\d{2}:\d{2}$"


# ------------------------------------------------------------------ inputs (no phone number anywhere)

class NoInput(BaseModel):
    pass


class ListDoctorsIn(BaseModel):
    specialty: str | None = Field(None, description="e.g. Dermatology, General Medicine, Orthopaedics, Paediatrics")


class SearchSlotsIn(BaseModel):
    date: str = Field(pattern=DATE, description="YYYY-MM-DD")
    specialty: str | None = Field(None, description="Limit to one specialty")
    doctor_id: str | None = Field(None, description="Limit to one doctor, e.g. d2")


class BookIn(BaseModel):
    doctor_id: str
    date: str = Field(pattern=DATE, description="YYYY-MM-DD")
    time: str = Field(pattern=TIME, description="24-hour HH:MM, a free slot start such as 17:30")
    patient_name: str = Field(min_length=2, description="The patient's name, as they told you")


class RescheduleIn(BaseModel):
    appointment_id: str
    new_date: str = Field(pattern=DATE, description="YYYY-MM-DD")
    new_time: str = Field(pattern=TIME, description="24-hour HH:MM, a free slot start")


class CancelIn(BaseModel):
    appointment_id: str


class PaymentLinkIn(BaseModel):
    bill_id: str


class RequestStaffIn(BaseModel):
    reason: Literal["patient_asked_for_person", "within_4_hours", "patient_upset", "cannot_resolve"] = Field(
        description="Why staff must take over")


# ------------------------------------------------------------------ helpers

def hospital_tool(fn: Callable[..., Awaitable]):
    """Turn hospital outages into a model-readable error, and count them (two in a row -> staff)."""

    @wraps(fn)
    async def wrapper(args, ctx: RunContext):
        try:
            return await fn(args, ctx)
        except HospitalUnavailable:
            note_failure(ctx)
            raise ToolError("The hospital system is not responding right now. Do not guess or make anything up: "
                            "tell the patient you could not check, and offer to try again.")
        except HospitalError as e:
            raise ToolError(f"The hospital system returned an error ({e.code}).")

    return wrapper


def note_failure(ctx: RunContext) -> None:
    ctx.state["failures"] = ctx.state.get("failures", 0) + 1


def _iso(date: str, time: str) -> str:
    try:
        datetime.fromisoformat(f"{date}T{time}:00")
    except ValueError:
        raise ToolError(f"{date} {time} is not a valid date and time.") from None
    return f"{date}T{time}:00+05:30"


def _hhmm(iso: str) -> str:
    return iso[11:16]


async def _doctor(h: Hospital, doctor_id: str) -> dict:
    d = await h.doctor(doctor_id.strip().lower())
    if not d:
        raise ToolError(f"There is no doctor with id {doctor_id!r}. Use list_doctors.")
    return d


def _appt_view(a: dict, doctors: dict[str, dict], now: datetime) -> dict:
    d = datetime.fromisoformat(a["start"])
    view = {"id": a["id"], "doctor_id": a["doctor_id"], "doctor": doctors.get(a["doctor_id"], {}).get("name", "?"),
            "weekday": d.strftime("%A"), "date": a["start"][:10], "time": _hhmm(a["start"]), "status": a["status"]}
    if a["status"] == "booked" and d - now < FOUR_HOURS:
        view["staff_only"] = ("Less than 4 hours away: it cannot be changed or cancelled in chat. If the patient wants "
                              "that, call request_staff with reason within_4_hours.")
    return view


async def own_active_appointment(ctx: RunContext, appt_id: str) -> dict:
    """The caller's own booked appointment with this id, or ToolError. Never reveals whether another person has it."""
    deps = deps_of(ctx)
    for a in await deps.hospital.appointments(deps.phone):
        if a["id"].upper() == appt_id.strip().upper() and a["phone"] == deps.phone and a["status"] == "booked":
            return a
    raise ToolError("This patient has no active appointment with that id. Use my_appointments to see theirs. "
                    "Do not discuss any other person's appointments.")


def _check_four_hours(ctx: RunContext, start_iso: str) -> None:
    if datetime.fromisoformat(start_iso) - deps_of(ctx).now < FOUR_HOURS:
        raise HandoffRequired("within_4h")


async def _check_free(h: Hospital, doctor_id: str, start_iso: str) -> None:
    if start_iso not in await h.free_slots(doctor_id, start_iso[:10]):
        raise ToolError(f"{start_iso[:10]} {_hhmm(start_iso)} is not a free slot for that doctor "
                        "(taken, closed, in the past, or more than 14 days ahead). Use search_slots to see free times.")


# ------------------------------------------------------------------ read tools

@hospital_tool
async def list_doctors(args: ListDoctorsIn, ctx: RunContext):
    docs = await deps_of(ctx).hospital.doctors()
    if args.specialty:
        docs = [d for d in docs if d["specialty"].lower() == args.specialty.strip().lower()] or docs
    return {"doctors": [{"id": d["id"], "name": d["name"], "specialty": d["specialty"]} for d in docs]}


@hospital_tool
async def search_slots(args: SearchSlotsIn, ctx: RunContext):
    h = deps_of(ctx).hospital
    docs = await h.doctors()
    if args.doctor_id:
        docs = [await _doctor(h, args.doctor_id)]
    elif args.specialty:
        docs = [d for d in docs if d["specialty"].lower() == args.specialty.strip().lower()]
        if not docs:
            raise ToolError("Unknown specialty. Use list_doctors to see the specialties.")
    slots = await asyncio.gather(*(h.free_slots(d["id"], args.date) for d in docs))
    day = datetime.fromisoformat(args.date)
    out = {"date": args.date, "weekday": day.strftime("%A"),
           "doctors": [{"doctor_id": d["id"], "doctor": d["name"], "specialty": d["specialty"],
                        "free_times": [_hhmm(s) for s in free]} for d, free in zip(docs, slots)]}
    if not any(d["free_times"] for d in out["doctors"]):
        out["note"] = "No free slots on this date (fully booked, closed, in the past, or beyond 14 days)."
    return out


@hospital_tool
async def my_appointments(args: NoInput, ctx: RunContext):
    deps = deps_of(ctx)
    docs = {d["id"]: d for d in await deps.hospital.doctors()}
    appts = [a for a in await deps.hospital.appointments(deps.phone) if a["phone"] == deps.phone]
    return {"appointments": [_appt_view(a, docs, deps.now) for a in appts]}


@hospital_tool
async def my_bills(args: NoInput, ctx: RunContext):
    deps = deps_of(ctx)
    bills = [b for b in await deps.hospital.bills(deps.phone) if b["phone"] == deps.phone]
    return {"bills": [{"bill_id": b["id"], "amount_inr": b["amount_inr"], "description": b["description"],
                       "date": b["date"], "status": b["status"]} for b in bills]}


# ------------------------------------------------------------------ prepare (read-only) -> Plan

async def prepare_book(args: BookIn, ctx: RunContext) -> Plan:
    deps = deps_of(ctx)
    doc = await _doctor(deps.hospital, args.doctor_id)
    start = _iso(args.date, args.time)
    await _check_free(deps.hospital, doc["id"], start)
    for a in await deps.hospital.appointments(deps.phone):
        if a["doctor_id"] == doc["id"] and a["start"] == start and a["status"] == "booked":
            raise ToolError("This patient already has a booking with that doctor at that time. Do not book it again.")
    name = " ".join(args.patient_name.split())
    return Plan("book", {"tool": "book_appointment", "doctor_id": doc["id"], "start": start, "name": name},
                {"doctor": doc["name"], "specialty": doc["specialty"], "start": start, "name": name})


async def prepare_reschedule(args: RescheduleIn, ctx: RunContext) -> Plan:
    deps = deps_of(ctx)
    appt = await own_active_appointment(ctx, args.appointment_id)
    _check_four_hours(ctx, appt["start"])  # the rule applies to the slot being given up
    start = _iso(args.new_date, args.new_time)
    if start == appt["start"]:
        raise ToolError("The appointment is already at that time.")
    doc = await _doctor(deps.hospital, appt["doctor_id"])
    await _check_free(deps.hospital, doc["id"], start)
    return Plan("reschedule", {"tool": "reschedule_appointment", "appointment_id": appt["id"], "start": start},
                {"doctor": doc["name"], "old_start": appt["start"], "start": start})


async def prepare_cancel(args: CancelIn, ctx: RunContext) -> Plan:
    deps = deps_of(ctx)
    appt = await own_active_appointment(ctx, args.appointment_id)
    _check_four_hours(ctx, appt["start"])
    doc = await _doctor(deps.hospital, appt["doctor_id"])
    return Plan("cancel", {"tool": "cancel_appointment", "appointment_id": appt["id"]},
                {"doctor": doc["name"], "start": appt["start"]})


async def prepare_payment_link(args: PaymentLinkIn, ctx: RunContext) -> Plan:
    deps = deps_of(ctx)
    bill = next((b for b in await deps.hospital.bills(deps.phone)
                 if b["id"].upper() == args.bill_id.strip().upper() and b["phone"] == deps.phone), None)
    if not bill:
        raise ToolError("This patient has no bill with that id. Use my_bills to see theirs. "
                        "Do not discuss any other person's bills.")
    if bill["status"] == "paid":
        raise ToolError("That bill is already paid, so no payment link is needed.")
    existing = ctx.state.get("links", {}).get(bill["id"])
    if existing:
        raise ToolError(f"A payment link for this bill was already sent in this chat: {existing} . Share that link again.")
    return Plan("payment_link", {"tool": "send_payment_link", "bill_id": bill["id"]},
                {"bill_id": bill["id"], "amount": bill["amount_inr"], "description": bill["description"]})


async def request_staff(args: RequestStaffIn, ctx: RunContext):
    deps_of(ctx).handoff = "within_4h" if args.reason == "within_4_hours" else "generic"
    return {"status": "done", "message": "Staff have been alerted and will take over this chat. The patient is told by the system."}


PREPARE = {"book_appointment": prepare_book, "reschedule_appointment": prepare_reschedule,
           "cancel_appointment": prepare_cancel, "send_payment_link": prepare_payment_link}


# ------------------------------------------------------------------ execute (the write)

async def _alt_times(ctx: RunContext, doctor_id: str, start_iso: str) -> list[str]:
    """Nearest free times for the same doctor and day, later ones first (they are what a patient usually accepts)."""
    try:
        free = await deps_of(ctx).hospital.free_slots(doctor_id, start_iso[:10])
    except HospitalError:
        return []
    want = _hhmm(start_iso)
    later = sorted(_hhmm(t) for t in free if _hhmm(t) > want)
    earlier = sorted((_hhmm(t) for t in free if _hhmm(t) < want), reverse=True)
    return (later + earlier)[:3]


def _outcome(ctx: RunContext, res, ok_payload: Callable[[dict], dict], retry: Callable[[list[str]], str] | None = None,
             alternatives: list[str] | None = None) -> ToolResult:
    """Map a WriteResult to what the model sees. Anything but 'done' is an error the model must not call success."""
    deps = deps_of(ctx)
    if res.status == "done":
        ctx.state["failures"] = 0
        return ToolResult({"status": "done", **ok_payload(res.data or {})})
    if res.status == "unknown":
        deps.handoff = "unknown_outcome"
        return ToolResult({"status": "unknown", "instruction": "It is NOT known whether this happened. Do not say it did. "
                           "Staff are being asked to check."}, True)
    if res.code == "slot_taken":
        deps.notices.append("slot_taken")
        out = {"status": "not_done", "reason": "slot_taken",
               "instruction": "That slot was just taken by someone else, so nothing was changed."}
        if alternatives and retry:
            out["nearest_free_times_same_day"] = alternatives
            out["instruction"] += (" Do not list options in text and do not ask the patient first. Right now, " + retry(alternatives)
                                   + " The patient will be asked to confirm it.")
        else:
            out["instruction"] += (" Find nearby alternatives with search_slots (other days or doctors) and call the change "
                                   "tool for the best one.")
        return ToolResult(out, True)
    if res.code == "service_unavailable":
        note_failure(ctx)
    return ToolResult({"status": "not_done", "reason": res.code, "message": res.message,
                       "instruction": "Nothing was changed. Do not say it was done."}, True)


async def execute_book(plan: Plan, ctx: RunContext) -> ToolResult:
    deps, k = deps_of(ctx), plan.key
    res = await deps.hospital.book(k["doctor_id"], k["start"], k["name"], deps.phone, plan.idem)
    if res.status == "done":
        ctx.state["name"] = k["name"]
    alts = await _alt_times(ctx, k["doctor_id"], k["start"]) if res.code == "slot_taken" else None
    return _outcome(
        ctx, res, lambda a: {"appointment": {"id": a.get("id"), "doctor": plan.fields["doctor"],
                                             "date": k["start"][:10], "time": _hhmm(k["start"])}},
        retry=lambda t: (f"call book_appointment with doctor_id {k['doctor_id']}, date {k['start'][:10]}, time {t[0]}, "
                         f"patient_name {k['name']!r}."), alternatives=alts)


async def execute_reschedule(plan: Plan, ctx: RunContext) -> ToolResult:
    k = plan.key
    res = await deps_of(ctx).hospital.reschedule(k["appointment_id"], k["start"])
    appt = await _try_doctor_id(ctx, k["appointment_id"]) if res.code == "slot_taken" else None
    alts = await _alt_times(ctx, appt, k["start"]) if appt else None
    return _outcome(
        ctx, res, lambda a: {"appointment": {"id": k["appointment_id"], "doctor": plan.fields["doctor"],
                                             "date": k["start"][:10], "time": _hhmm(k["start"])}},
        retry=lambda t: (f"call reschedule_appointment with appointment_id {k['appointment_id']}, new_date "
                         f"{k['start'][:10]}, new_time {t[0]}."), alternatives=alts)


async def _try_doctor_id(ctx: RunContext, appt_id: str) -> str | None:
    try:
        a = await deps_of(ctx).hospital.appointment(appt_id)
    except HospitalError:
        return None
    return a["doctor_id"] if a else None


async def execute_cancel(plan: Plan, ctx: RunContext) -> ToolResult:
    res = await deps_of(ctx).hospital.cancel(plan.key["appointment_id"])
    return _outcome(ctx, res, lambda a: {"cancelled": {"id": plan.key["appointment_id"], "doctor": plan.fields["doctor"],
                                                       "date": plan.fields["start"][:10], "time": _hhmm(plan.fields["start"])}})


async def execute_payment_link(plan: Plan, ctx: RunContext) -> ToolResult:
    deps = deps_of(ctx)
    res = await deps.hospital.payment_link(plan.key["bill_id"])
    if res.status == "done":
        url = (res.data or {})["url"]
        ctx.state.setdefault("links", {})[plan.key["bill_id"]] = url
        deps.new_links.append(url)
    return _outcome(ctx, res, lambda d: {"payment_link": d.get("url"), "bill_id": plan.key["bill_id"],
                                         "instruction": "Include this exact URL in your reply."})


EXECUTE = {"book_appointment": execute_book, "reschedule_appointment": execute_reschedule,
           "cancel_appointment": execute_cancel, "send_payment_link": execute_payment_link}


def _write_tool(name: str):
    """The model-facing function for a change tool: runs only with a Plan the gate approved."""

    @hospital_tool
    async def run(args, ctx: RunContext):
        deps = deps_of(ctx)
        plan = deps.plans.pop(name, None)
        if plan is None:  # defence in depth: the gate always sets this before the tool runs
            raise ToolError("This change was not confirmed by the patient, so it was not made.")
        try:
            return await EXECUTE[name](plan, ctx)
        finally:
            ctx.state["pending"] = None
            deps.confirmed = None

    return run


# ------------------------------------------------------------------ registries

def staff_tool() -> Tool:
    return Tool("request_staff", "Hand this chat to hospital staff. This is the ONLY way staff are actually notified: "
                "never tell the patient you are connecting them with staff unless you called this in the same turn.",
                RequestStaffIn, request_staff)


def booking_tools() -> ToolRegistry:
    t = lambda name, desc, model, fn, side=False: Tool(name, desc, model, fn, side_effect=side, timeout_s=40)  # noqa: E731
    return ToolRegistry([
        t("list_doctors", "List the hospital's doctors, optionally for one specialty.", ListDoctorsIn, list_doctors),
        t("search_slots", "Free 30-minute slots on one date, for one doctor, one specialty, or all doctors. "
          "Always use this before offering or booking a time.", SearchSlotsIn, search_slots),
        t("my_appointments", "This patient's own appointments (ids, doctor, date, time, status).", NoInput, my_appointments),
        t("book_appointment", "Book a slot for this patient. Does NOT book immediately: the patient is first shown a "
          "read-back and must reply YES. Calling it ends your turn.", BookIn, _write_tool("book_appointment"), True),
        t("reschedule_appointment", "Move one of this patient's appointments to a new free time with the same doctor. "
          "Needs the patient's YES first; calling it ends your turn.", RescheduleIn, _write_tool("reschedule_appointment"), True),
        t("cancel_appointment", "Cancel one of this patient's appointments. Needs the patient's YES first; "
          "calling it ends your turn.", CancelIn, _write_tool("cancel_appointment"), True),
        staff_tool(),
    ])


def billing_tools() -> ToolRegistry:
    return ToolRegistry([
        Tool("my_bills", "This patient's own bills (id, amount in rupees, description, paid or unpaid).", NoInput,
             my_bills, timeout_s=40),
        Tool("send_payment_link", "Create a payment link for one of this patient's unpaid bills. Needs the patient's "
             "YES first; calling it ends your turn.", PaymentLinkIn, _write_tool("send_payment_link"),
             side_effect=True, timeout_s=40),
        staff_tool(),
    ])


def info_tools() -> ToolRegistry:
    return ToolRegistry([staff_tool()])
