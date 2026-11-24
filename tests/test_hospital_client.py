"""The async hospital client against the real mock app (in-process), including its fault modes."""

import asyncio

import httpx
import pytest

from agent_service.hospital import Hospital, HospitalUnavailable
from mock_hospital.app import app

START = "2026-12-04T17:00:00+05:30"  # Dr. Rao (d2), free all Friday


async def _nosleep(_):
    return None


def run(coro_fn):
    """Run `coro_fn(hospital, admin)` with a fresh mock state and chaos off; reset afterwards."""
    async def go():
        http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), timeout=10)
        h = Hospital("http://mock", http=http, sleep=_nosleep)
        await http.post("http://mock/_admin/reset")
        await http.post("http://mock/_admin/chaos", json={})

        async def chaos(**kw):
            await http.post("http://mock/_admin/chaos", json=kw)

        try:
            return await coro_fn(h, chaos)
        finally:
            await http.post("http://mock/_admin/chaos", json={})
            await http.post("http://mock/_admin/reset")
            await http.aclose()

    return asyncio.run(go())


def test_reads_and_clock():
    async def t(h, chaos):
        assert (await h.now()).isoformat() == "2026-12-01T09:00:00+05:30"
        assert len(await h.doctors()) == 6
        assert START in await h.free_slots("d2", "2026-12-04")
        assert [a["id"] for a in await h.appointments("9876543210")] == ["A1001"]
        assert (await h.appointment("A9999")) is None
    run(t)


def test_reads_retry_transient_503_then_give_up():
    async def t(h, chaos):
        await chaos(error_rate=1.0)
        with pytest.raises(HospitalUnavailable):
            await h.appointments("9876543210")
    run(t)


def test_book_saved_but_503_retries_with_same_key_and_never_double_books():
    async def t(h, chaos):
        await chaos(commit_then_fail_next=1)
        r = await h.book("d2", START, "Rohan", "9000000001", "key-1")
        assert r.status == "done" and r.data["start"] == START
        assert len(await h.appointments("9000000001")) == 1
    run(t)


def test_book_lost_race_is_not_done_and_not_retried_into_a_booking():
    async def t(h, chaos):
        await chaos(race_next=1)
        r = await h.book("d2", START, "Rohan", "9000000001", "key-2")
        assert r.status == "not_done" and r.code == "slot_taken"
        assert await h.appointments("9000000001") == []
    run(t)


def test_book_total_outage_is_not_done_or_unknown_never_done():
    async def t(h, chaos):
        await chaos(error_rate=1.0)
        r = await h.book("d2", START, "Rohan", "9000000001", "key-3")
        assert r.status == "unknown"  # cannot even verify
        await chaos()  # hospital back
        assert await h.appointments("9000000001") == []
    run(t)


def test_reschedule_saved_but_503_is_reported_done_not_slot_taken():
    async def t(h, chaos):
        await chaos(commit_then_fail_next=1)
        r = await h.reschedule("A1003", "2026-12-05T10:00:00+05:30")
        assert r.status == "done"
        assert (await h.appointment("A1003"))["start"] == "2026-12-05T10:00:00+05:30"
    run(t)


def test_reschedule_real_race_is_not_done_and_appointment_unchanged():
    async def t(h, chaos):
        await chaos(race_next=1)
        r = await h.reschedule("A1003", "2026-12-05T10:00:00+05:30")
        assert r.status == "not_done" and r.code == "slot_taken"
        assert (await h.appointment("A1003"))["start"] == "2026-12-03T17:30:00+05:30"
    run(t)


def test_cancel_saved_but_503_is_reported_done_not_not_active():
    async def t(h, chaos):
        await chaos(commit_then_fail_next=1)
        r = await h.cancel("A1001")
        assert r.status == "done" and (await h.appointment("A1001"))["status"] == "cancelled"
    run(t)


def test_cancel_already_cancelled_is_not_done():
    async def t(h, chaos):
        assert (await h.cancel("A1001")).status == "done"
        r = await h.cancel("A1001")
        assert r.status == "not_done" and r.code == "not_active"
    run(t)


def test_payment_link_and_already_paid():
    async def t(h, chaos):
        r = await h.payment_link("B1")
        assert r.status == "done" and r.data["url"].startswith("https://pay.citycare.example/b/B1/")
        paid = await h.payment_link("B2")
        assert paid.status == "not_done" and paid.code == "already_paid"
    run(t)
