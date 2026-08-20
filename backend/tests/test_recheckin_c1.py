"""C1 re-check ladder — unit tests for the rules that must not drift.

The invariants here are the ones the design doc and the Critical Alerts
entitlement justification depend on:
  * a re-check is NEVER sent to a device whose current status is not trapped
  * escalation is one-way, and MUCH WORSE reaches red from any band
  * BETTER never reduces severity automatically
  * the tap time is authoritative, the arrival time is kept alongside it
  * a non-response is written as a positive fact, not an absence
"""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from recheckin import (
    DEFAULT_LADDER,
    escalate,
    interval_minutes,
    prompt_text,
    record_answer,
    send_due_rechecks,
    silence_state,
)


def _run(coro):
    return asyncio.run(coro)


class FakeCollection:
    def __init__(self, rows=None):
        self.rows = list(rows or [])
        self.inserted = []

    def find(self, query=None, projection=None):
        rows = self.rows
        q = query or {}
        if "status" in q:
            rows = [r for r in rows if r.get("status") == q["status"]]
        if "user_id" in q and isinstance(q["user_id"], dict):
            wanted = set(q["user_id"].get("$in") or [])
            rows = [r for r in rows if r.get("user_id") in wanted]
        if "platform" in q and isinstance(q["platform"], str):
            rows = [r for r in rows if r.get("platform") == q["platform"]]

        class Cursor:
            def __init__(self, rows):
                self._rows = rows

            def sort(self, *a, **k):
                return self

            async def to_list(self, n):
                return [dict(r) for r in self._rows[:n]]

        return Cursor(rows)

    async def find_one(self, query=None, projection=None, sort=None):
        for r in self.rows:
            if all(r.get(k) == v for k, v in (query or {}).items()):
                return dict(r)
        return None

    async def insert_one(self, doc):
        self.inserted.append(doc)
        return type("R", (), {"inserted_id": "x"})()

    async def update_one(self, query, update, upsert=False):
        for r in self.rows:
            if all(r.get(k) == v for k, v in (query or {}).items()):
                r.update(update.get("$set") or {})
                return type("R", (), {"modified_count": 1})()
        return type("R", (), {"modified_count": 0})()

    async def count_documents(self, query):
        return len([r for r in self.rows
                    if all(r.get(k) == v for k, v in (query or {}).items())])


class FakeDb:
    def __init__(self, device_status=None, push_devices=None):
        self.device_status = FakeCollection(device_status)
        self.push_devices = FakeCollection(push_devices)
        self.status_events = FakeCollection()


NOW = datetime(2026, 6, 18, 12, 0, tzinfo=timezone.utc)


def _trapped(device_id="qg-1786015151886-2zbf6xjy", **over):
    row = {
        "device_id": device_id,
        "status": "trapped",
        "severity": "yellow",
        "battery_pct": 70,
        "updated_at": (NOW - timedelta(minutes=2)).isoformat(),
        "trapped_since": (NOW - timedelta(minutes=20)).isoformat(),
        "latitude": 35.9, "longitude": 14.5,
    }
    row.update(over)
    return row


def _ios(device_id="qg-1786015151886-2zbf6xjy"):
    return {"user_id": device_id, "platform": "ios", "device_token": "t" * 64}


class RecordingSender:
    def __init__(self, delivered=True):
        self.calls = []
        self.delivered = delivered

    async def __call__(self, *, db, devices, title, body, idempotency_key,
                       battery_saving=False, ladder_step=None):
        self.calls.append({"devices": devices, "title": title, "body": body,
                           "battery_saving": battery_saving})
        return {"events": [{"user_id": d["user_id"], "check_id": d["check_id"],
                            "delivered": self.delivered} for d in devices]}


# ── the ladder itself ────────────────────────────────────────────────────
@pytest.mark.parametrize("minutes_trapped,expected", [
    (5, 15), (59, 15), (61, 30), (239, 30), (241, 60), (719, 60), (721, 180),
    (60 * 48, 180),
])
def test_ladder_widens_with_time(minutes_trapped, expected):
    mins, saving = interval_minutes(timedelta(minutes=minutes_trapped), 80)
    assert mins == expected
    assert saving is False


def test_low_battery_doubles_and_says_so():
    mins, saving = interval_minutes(timedelta(minutes=10), 18)
    assert (mins, saving) == (30, True)
    _, body = prompt_text(timedelta(minutes=10), True)
    assert "save your battery" in body


def test_critical_battery_triples():
    assert interval_minutes(timedelta(minutes=10), 7) == (45, True)


def test_unknown_battery_does_not_widen():
    assert interval_minutes(timedelta(minutes=10), None) == (15, False)


def test_ladder_first_hour_is_four_wake_ups():
    assert DEFAULT_LADDER[0] == (1, 15)


# ── escalation rules ─────────────────────────────────────────────────────
def test_much_worse_reaches_red_from_green_in_one_tap():
    assert escalate("green", "much_worse") == "red"


def test_worse_moves_one_band():
    assert escalate("green", "worse") == "yellow"
    assert escalate("yellow", "worse") == "red"
    assert escalate("red", "worse") == "red"


def test_better_and_same_never_change_the_band():
    for answer in ("better", "same"):
        assert escalate("yellow", answer) == "yellow"


def test_worse_with_unknown_band_assumes_the_worst():
    assert escalate(None, "worse") == "red"


# ── two kinds of silence ─────────────────────────────────────────────────
def test_dark_after_45_minutes_of_nothing():
    row = _trapped(updated_at=(NOW - timedelta(minutes=50)).isoformat())
    assert silence_state(row, NOW) == "dark"


def test_silent_alive_when_phone_reports_but_nobody_answers():
    row = _trapped(recheck={"consecutive_missed": 2})
    assert silence_state(row, NOW) == "silent_alive"


def test_answering_recently_is_neither():
    assert silence_state(_trapped(), NOW) is None


# ── who may be prompted ──────────────────────────────────────────────────
def test_never_prompts_a_device_that_is_not_trapped():
    """The invariant the Critical Alerts entitlement justification rests on."""
    db = FakeDb(
        device_status=[
            _trapped("qg-1111111111111-aaaaaaaa", status="safe"),
            _trapped("qg-2222222222222-bbbbbbbb", status="rescued"),
        ],
        push_devices=[_ios("qg-1111111111111-aaaaaaaa"),
                      _ios("qg-2222222222222-bbbbbbbb")],
    )
    sender = RecordingSender()
    result = _run(send_due_rechecks(db, sender, now=NOW))
    assert result == {"due": 0, "sent": 0}
    assert sender.calls == []


def test_prompts_a_trapped_device_and_logs_the_send():
    db = FakeDb(device_status=[_trapped()], push_devices=[_ios()])
    sender = RecordingSender()
    result = _run(send_due_rechecks(db, sender, now=NOW))
    assert result["due"] == 1 and result["sent"] == 1
    assert sender.calls[0]["devices"][0]["check_id"]
    kinds = [e["kind"] for e in db.status_events.inserted]
    assert kinds == ["recheck_sent"]
    rc = db.device_status.rows[0]["recheck"]
    assert rc["pending_check_id"] and rc["interval_minutes"] == 15


def test_a_dark_phone_is_not_prompted():
    db = FakeDb(
        device_status=[_trapped(updated_at=(NOW - timedelta(hours=3)).isoformat())],
        push_devices=[_ios()],
    )
    sender = RecordingSender()
    assert _run(send_due_rechecks(db, sender, now=NOW))["due"] == 0


def test_not_due_yet_is_left_alone():
    db = FakeDb(
        device_status=[_trapped(recheck={
            "next_check_at": (NOW + timedelta(minutes=9)).isoformat()})],
        push_devices=[_ios()],
    )
    assert _run(send_due_rechecks(db, RecordingSender(), now=NOW))["due"] == 0


def test_unanswered_previous_check_is_recorded_as_missed():
    """"We asked and heard nothing" must be a positive fact in the record."""
    db = FakeDb(
        device_status=[_trapped(recheck={"pending_check_id": "abc123",
                                         "consecutive_missed": 0})],
        push_devices=[_ios()],
    )
    _run(send_due_rechecks(db, RecordingSender(), now=NOW))
    kinds = [e["kind"] for e in db.status_events.inserted]
    assert "recheck_missed" in kinds
    missed = next(e for e in db.status_events.inserted
                  if e["kind"] == "recheck_missed")
    assert missed["check_id"] == "abc123"
    assert db.device_status.rows[0]["recheck"]["consecutive_missed"] == 1


# ── answers ──────────────────────────────────────────────────────────────
def test_worse_escalates_and_flags_deteriorating():
    db = FakeDb(device_status=[_trapped(severity="green")])
    out = _run(record_answer(db, "qg-1786015151886-2zbf6xjy", "worse"))
    assert out["severity"] == "yellow" and out["deteriorating"] is True
    assert db.device_status.rows[0]["severity"] == "yellow"
    assert db.device_status.rows[0]["deteriorating"] is True


def test_much_worse_goes_straight_to_red():
    db = FakeDb(device_status=[_trapped(severity="green")])
    out = _run(record_answer(db, "qg-1786015151886-2zbf6xjy", "much_worse"))
    assert out["severity"] == "red"


def test_better_is_recorded_but_never_downgrades():
    db = FakeDb(device_status=[_trapped(severity="red")])
    out = _run(record_answer(db, "qg-1786015151886-2zbf6xjy", "better"))
    assert out["severity"] == "red"
    assert db.device_status.rows[0]["severity"] == "red"
    assert db.device_status.rows[0]["reports_improving"] is True


def test_same_is_kept_as_evidence_of_a_conscious_reachable_person():
    db = FakeDb(device_status=[_trapped()])
    _run(record_answer(db, "qg-1786015151886-2zbf6xjy", "same"))
    ev = db.status_events.inserted[0]
    assert ev["kind"] == "recheck_answered" and ev["answer"] == "same"


def test_tap_time_is_authoritative_and_arrival_is_kept_alongside():
    db = FakeDb(device_status=[_trapped()])
    tapped = (datetime.now(timezone.utc) - timedelta(minutes=45)).isoformat()
    out = _run(record_answer(db, "qg-1786015151886-2zbf6xjy", "same",
                             answered_at=tapped))
    ev = db.status_events.inserted[0]
    assert ev["answered_at"] == out["answered_at"] == tapped
    assert ev["received_at"] != ev["answered_at"]
    assert ev["at"] == tapped          # every human surface reads `at`
    assert ev["queued_offline"] is True


def test_impossible_device_clock_is_flagged_not_corrected():
    db = FakeDb(device_status=[_trapped()])
    future = (datetime.now(timezone.utc) + timedelta(hours=3)).isoformat()
    _run(record_answer(db, "qg-1786015151886-2zbf6xjy", "same",
                       answered_at=future))
    ev = db.status_events.inserted[0]
    assert ev["device_clock_suspect"] is True
    assert ev["answered_at"] == future   # kept, never silently rewritten


def test_answering_clears_the_missed_counter_and_reschedules():
    db = FakeDb(device_status=[_trapped(recheck={"consecutive_missed": 3,
                                                "pending_check_id": "abc"})])
    out = _run(record_answer(db, "qg-1786015151886-2zbf6xjy", "same",
                             check_id="abc"))
    rc = db.device_status.rows[0]["recheck"]
    assert rc["consecutive_missed"] == 0 and rc["pending_check_id"] is None
    assert out["next_check_at"]


def test_unknown_answer_is_refused():
    db = FakeDb(device_status=[_trapped()])
    with pytest.raises(ValueError):
        _run(record_answer(db, "qg-1786015151886-2zbf6xjy", "fine"))


def test_unknown_device_is_refused():
    db = FakeDb(device_status=[])
    with pytest.raises(KeyError):
        _run(record_answer(db, "qg-0000000000000-zzzzzzzz", "same"))


# ── payload shape ────────────────────────────────────────────────────────
def test_recheck_payload_default_is_time_sensitive_not_critical():
    """#207 (Batch 7): re-checks used to fire at `critical` every time,
    which retrained users to mute the whole app. They now default to
    `time-sensitive` (still breaches Focus/DND, but respects the silent
    switch) and only escalate to Critical when the caller says so —
    once per person per incident, after three unanswered checks."""
    from apns import RECHECK_CATEGORY_ID, _build_recheck_payload
    p = _build_recheck_payload("t", "b", check_id="c1", device_id="d1")
    aps = p["aps"]
    assert aps["interruption-level"] == "time-sensitive"
    assert aps["sound"] == "recheck.wav"   # ~1s chime, NOT the 30s siren
    assert aps["category"] == RECHECK_CATEGORY_ID != "TREMOR_INFO"
    # v1.0.40 fix (#208 root cause): routing keys nested under `body`
    # because expo-notifications iOS reads content.data from
    # userInfo["body"]. Without this, a trapped-person "still ok?" tap
    # landed on /quake/unknown instead of /recheck — a life-safety miss.
    assert p["body"]["kind"] == "recheck" and p["body"]["action_url"] == "/recheck"
    assert p["body"]["check_id"] == "c1"


def test_recheck_payload_escalates_when_asked():
    """#207: the sweeper decides when to escalate. Payload builder
    honours it via `escalate=True`."""
    from apns import _build_recheck_payload
    p = _build_recheck_payload(
        "t", "b", check_id="c1", device_id="d1",
        consecutive_missed=3, escalate=True,
    )
    aps = p["aps"]
    assert aps["interruption-level"] == "critical"
    assert aps["sound"]["critical"] == 1
    assert aps["sound"]["name"] == "recheck.wav"


def test_critical_alert_payload_still_carries_no_category():
    """Nothing may compete for attention with I'M SAFE / I'M TRAPPED."""
    from apns import _build_critical_payload
    p = _build_critical_payload("t", "b", "/alert")
    assert "category" not in p["aps"]
