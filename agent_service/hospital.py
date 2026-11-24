"""Async client for the hospital system, built for a system that is slow, flaky and has no auth.

What it adds over the starter client:
  * timeouts, and retries (with backoff) of transient failures: 503s, timeouts, connection errors
  * reads raise HospitalUnavailable only after the retry budget is spent
  * writes never raise for hospital-side trouble. They return a WriteResult whose status is
      done      it happened (we know, from the response or from re-reading the real state)
      not_done  it did not happen (slot taken, rule violation, bad input, or a failure we verified)
      unknown   we could not find out. The caller must not say it happened, and should hand off.

The mock returns the same 503 for "failed before saving" and "saved, then failed", so every
503 or timeout on a write is ambiguous. Each write resolves that its own way:
  book        retried with the SAME idempotency key (safe), then verified by listing appointments
  reschedule  after any failure, GET the appointment: if it is already at the new time it worked
  cancel      after any failure, GET the appointment: if it is cancelled it worked
  payment link  the mock cannot save-then-fail a link, so a retry is safe (worst case: an extra link)
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Awaitable, Callable, Literal

import httpx

DEFAULT_URL = "http://localhost:8001"


class HospitalError(Exception):
    def __init__(self, code: str, message: str = "", status: int | None = None):
        super().__init__(f"{code}: {message}")
        self.code, self.message, self.status = code, message, status


class HospitalUnavailable(HospitalError):
    """The hospital system could not be reached or kept failing (503, timeout, connection error)."""

    def __init__(self, message: str = "hospital system unavailable"):
        super().__init__("service_unavailable", message, 503)


@dataclass
class WriteResult:
    status: Literal["done", "not_done", "unknown"]
    data: dict | None = None
    code: str | None = None  # slot_taken, not_active, already_paid, invalid_slot, service_unavailable, ...
    message: str = ""


class _Transient(Exception):
    pass


class Hospital:
    def __init__(self, base_url: str | None = None, *, http: httpx.AsyncClient | None = None,
                 timeout_s: float = 8.0, retries: int = 3, backoff_s: float = 0.25,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep):
        self.base_url = (base_url or os.getenv("HOSPITAL_URL", DEFAULT_URL)).rstrip("/")
        self._http = http or httpx.AsyncClient(timeout=timeout_s)
        self._owns_http = http is None
        self.retries, self.backoff_s, self._sleep = retries, backoff_s, sleep
        self._doctors: list[dict] | None = None

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    # ------------------------------------------------------------ plumbing

    async def _send(self, method: str, path: str, *, params: dict | None = None, json: dict | None = None):
        """One attempt. Returns (status, body). Raises _Transient for 5xx, timeouts, connection errors."""
        try:
            r = await self._http.request(method, f"{self.base_url}{path}", params=params, json=json)
        except (httpx.TimeoutException, httpx.TransportError) as e:
            raise _Transient(type(e).__name__) from e
        try:
            body = r.json()
        except ValueError:
            body = {}
        if r.status_code >= 500:
            raise _Transient(f"HTTP {r.status_code}")
        return r.status_code, body if isinstance(body, dict) else {}

    async def _pause(self, attempt: int) -> None:
        await self._sleep(self.backoff_s * (2 ** attempt))

    async def _read(self, path: str, **params: Any) -> dict:
        for attempt in range(self.retries + 1):
            try:
                status, body = await self._send("GET", path, params=params or None)
            except _Transient:
                if attempt < self.retries:
                    await self._pause(attempt)
                continue
            if status >= 400:
                raise HospitalError(body.get("error", "error"), body.get("message", ""), status)
            return body
        raise HospitalUnavailable()

    # ------------------------------------------------------------ reads

    async def now(self) -> datetime:
        return datetime.fromisoformat((await self._read("/now"))["now"])

    async def doctors(self) -> list[dict]:
        if self._doctors is None:
            self._doctors = (await self._read("/doctors"))["doctors"]
        return self._doctors

    async def doctor(self, doctor_id: str) -> dict | None:
        return next((d for d in await self.doctors() if d["id"] == doctor_id), None)

    async def free_slots(self, doctor_id: str, date: str) -> list[str]:
        return (await self._read(f"/doctors/{doctor_id}/slots", date=date))["free"]

    async def appointments(self, phone: str) -> list[dict]:
        return (await self._read("/appointments", phone=phone))["appointments"]

    async def appointment(self, appt_id: str) -> dict | None:
        try:
            return await self._read(f"/appointments/{appt_id}")
        except HospitalError as e:
            if e.status == 404:
                return None
            raise

    async def bills(self, phone: str) -> list[dict]:
        return (await self._read("/bills", phone=phone))["bills"]

    # ------------------------------------------------------------ writes

    async def book(self, doctor_id: str, start: str, patient_name: str, phone: str, idempotency_key: str) -> WriteResult:
        body = {"doctor_id": doctor_id, "start": start, "patient_name": patient_name, "phone": phone,
                "idempotency_key": idempotency_key}
        for attempt in range(self.retries + 1):
            try:
                status, data = await self._send("POST", "/appointments", json=body)
            except _Transient:
                await self._pause(attempt)
                continue  # same idempotency key: a retry can never double-book
            if status in (200, 201):
                return WriteResult("done", data)
            return WriteResult("not_done", code=data.get("error"), message=data.get("message", ""))
        # Retries exhausted. Look at the real state before giving an answer.
        try:
            for a in await self.appointments(phone):
                if a["doctor_id"] == doctor_id and a["start"] == start and a["status"] == "booked":
                    return WriteResult("done", a)
        except HospitalUnavailable:
            return WriteResult("unknown", message="could not confirm whether the booking was saved")
        return WriteResult("not_done", code="service_unavailable", message="the hospital system kept failing")

    async def reschedule(self, appt_id: str, new_start: str) -> WriteResult:
        last_check_failed = False
        for attempt in range(self.retries + 1):
            try:
                status, data = await self._send("POST", f"/appointments/{appt_id}/reschedule", json={"new_start": new_start})
            except _Transient:
                state = await self._try_get(appt_id)
                if state and state["status"] == "booked" and state["start"] == new_start:
                    return WriteResult("done", state)  # saved, then the 503
                last_check_failed = state is None
                await self._pause(attempt)
                continue
            if status == 200:
                return WriteResult("done", data)
            if data.get("error") == "slot_taken":
                # Could be a real race, or our own earlier attempt that did save (the slot is now "taken" by us).
                state = await self._try_get(appt_id)
                if state and state["status"] == "booked" and state["start"] == new_start:
                    return WriteResult("done", state)
            return WriteResult("not_done", code=data.get("error"), message=data.get("message", ""))
        if last_check_failed:
            return WriteResult("unknown", message="could not confirm whether the change was saved")
        return WriteResult("not_done", code="service_unavailable", message="the hospital system kept failing")

    async def cancel(self, appt_id: str) -> WriteResult:
        last_check_failed = False
        for attempt in range(self.retries + 1):
            try:
                status, data = await self._send("POST", f"/appointments/{appt_id}/cancel")
            except _Transient:
                state = await self._try_get(appt_id)
                if state and state["status"] == "cancelled":
                    return WriteResult("done", state)  # saved, then the 503
                last_check_failed = state is None
                await self._pause(attempt)
                continue
            if status == 200:
                return WriteResult("done", data)
            if data.get("error") == "not_active" and attempt > 0:
                state = await self._try_get(appt_id)  # an earlier attempt of ours may have saved it
                if state and state["status"] == "cancelled":
                    return WriteResult("done", state)
            return WriteResult("not_done", code=data.get("error"), message=data.get("message", ""))
        if last_check_failed:
            return WriteResult("unknown", message="could not confirm whether the cancellation was saved")
        return WriteResult("not_done", code="service_unavailable", message="the hospital system kept failing")

    async def payment_link(self, bill_id: str) -> WriteResult:
        for attempt in range(self.retries + 1):
            try:
                status, data = await self._send("POST", f"/bills/{bill_id}/payment-link")
            except _Transient:
                await self._pause(attempt)
                continue
            if status in (200, 201):
                return WriteResult("done", data)
            return WriteResult("not_done", code=data.get("error"), message=data.get("message", ""))
        return WriteResult("not_done", code="service_unavailable", message="the hospital system kept failing")

    async def _try_get(self, appt_id: str) -> dict | None:
        """Re-read one appointment to learn what really happened. None if we still can't tell."""
        try:
            return await self.appointment(appt_id)
        except HospitalError:
            return None
