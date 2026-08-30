"""BUG-2026-02-trapped-since-uses-old-alert (Paul, verbatim, dashboard pin):

  "On the dashboard, a person's pin says 'Trapped for 2 hours and 20 minutes'
   when they actually reported trapped less than 5 minutes earlier. I checked
   twice, 4 minutes apart, and the number went up by exactly 4 minutes — so
   it's a real clock, just starting from the wrong time. Can you find out
   why it's not using their most recent report as the starting point, and
   fix it?"

Root cause: `_trapped_since_map` walked back through the ENTIRE `status_events`
history for the device, so a "trapped" event from a PRIOR alert (that was
never explicitly closed by a `safe` / `rescued` event) was picked up as the
start of the CURRENT trapped spell. The current spell cannot begin before
the current alert did.

The doctrine encoded here (so this cannot silently regress):

  1. The trapped spell is bounded on the LEFT by the start of the current
     alert (latest `push_events` trigger). Trapped events strictly before
     that boundary are ignored — they belong to a prior incident.
  2. When there is NO alert on record we fall back to the original
     unbounded walk, so bootstrap / test-only fixtures without a
     push_events row still work.
  3. Legacy trigger rows without a `kind` field are treated as triggers
     (matches the convention in server.py:3925).
"""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest

import reports_export


NOW = datetime(2026, 2, 15, 12, 0, tzinfo=timezone.utc)


def _run(coro):
    return asyncio.run(coro)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


class _Cursor:
    def __init__(self, rows):
        self._rows = rows

    def sort(self, field, direction=1):
        reverse = direction == -1
        self._rows = sorted(self._rows, key=lambda r: r.get(field) or "", reverse=reverse)
        return self

    async def to_list(self, n):
        return [dict(r) for r in self._rows[:n]]


class _Coll:
    def __init__(self, rows=None):
        self._rows = list(rows or [])

    def find(self, query=None, projection=None):
        q = query or {}
        rows = list(self._rows)
        # Support {"device_id": {"$in": [...]}}
        did = q.get("device_id")
        if isinstance(did, dict) and "$in" in did:
            wanted = set(did["$in"])
            rows = [r for r in rows if r.get("device_id") in wanted]
        elif isinstance(did, str):
            rows = [r for r in rows if r.get("device_id") == did]
        # Support {"recorded_at": {"$gte": iso}}
        ra = q.get("recorded_at")
        if isinstance(ra, dict) and "$gte" in ra:
            bound = ra["$gte"]
            rows = [r for r in rows if (r.get("recorded_at") or "") >= bound]
        # Support the $or trigger filter used by _current_alert_start
        or_clause = q.get("$or")
        if or_clause:
            def _match_any(r):
                for sub in or_clause:
                    ok = True
                    for k, v in sub.items():
                        if isinstance(v, dict) and "$exists" in v:
                            has = k in r
                            if v["$exists"] and not has:
                                ok = False; break
                            if (not v["$exists"]) and has:
                                ok = False; break
                        else:
                            if r.get(k) != v:
                                ok = False; break
                    if ok:
                        return True
                return False
            rows = [r for r in rows if _match_any(r)]
        return _Cursor(rows)


class _Db:
    def __init__(self, status_events=None, push_events=None):
        self.status_events = _Coll(status_events)
        self.push_events = _Coll(push_events)


DEV = "qg-1786015151886-abcdefgh"


def _patch_db(monkeypatch, db):
    monkeypatch.setattr(reports_export, "db", db)


class TestTrappedSinceBoundedByAlert:
    """The current trapped spell begins with the first trapped event
    ON OR AFTER the current alert start — never before it."""

    def test_prior_alert_trapped_event_is_ignored(self, monkeypatch):
        """The Paul-reported scenario, verbatim: prior alert had a trapped
        event 2h 20m ago; a fresh alert 10 minutes ago; the person reported
        trapped again 4 minutes ago. Timer must show ~4 minutes, NOT 2h 20m."""
        prior_alert   = NOW - timedelta(hours=3)
        prior_trapped = NOW - timedelta(hours=2, minutes=20)
        current_alert = NOW - timedelta(minutes=10)
        fresh_trapped = NOW - timedelta(minutes=4)
        db = _Db(
            status_events=[
                {"device_id": DEV, "status": "trapped",
                 "recorded_at": _iso(prior_trapped)},
                {"device_id": DEV, "status": "trapped",
                 "recorded_at": _iso(fresh_trapped)},
            ],
            push_events=[
                {"kind": "trigger", "created_at": _iso(prior_alert)},
                {"kind": "trigger", "created_at": _iso(current_alert)},
            ],
        )
        _patch_db(monkeypatch, db)
        out = _run(reports_export._trapped_since_map([DEV]))
        assert out.get(DEV) == _iso(fresh_trapped), (
            f"Expected trapped_since to be the fresh 4-min-old event "
            f"(current alert boundary at {_iso(current_alert)}) but got "
            f"{out.get(DEV)!r} — the prior alert's stale event bled through."
        )

    def test_stand_down_after_prior_alert_still_bounded_by_new_trigger(self, monkeypatch):
        """Prior alert → stand-down → new alert. The new alert start is
        what bounds the spell, not the older stand-down or the older
        trigger. A stand-down alone must NOT be treated as the current
        alert start (its purpose is to close, not open, an incident)."""
        prior_alert    = NOW - timedelta(hours=4)
        prior_trapped  = NOW - timedelta(hours=3, minutes=50)
        stood_down     = NOW - timedelta(hours=3)
        current_alert  = NOW - timedelta(minutes=15)
        fresh_trapped  = NOW - timedelta(minutes=3)
        db = _Db(
            status_events=[
                {"device_id": DEV, "status": "trapped",
                 "recorded_at": _iso(prior_trapped)},
                {"device_id": DEV, "status": "trapped",
                 "recorded_at": _iso(fresh_trapped)},
            ],
            push_events=[
                {"kind": "trigger", "created_at": _iso(prior_alert)},
                {"kind": "alert_stood_down", "created_at": _iso(stood_down)},
                {"kind": "trigger", "created_at": _iso(current_alert)},
            ],
        )
        _patch_db(monkeypatch, db)
        out = _run(reports_export._trapped_since_map([DEV]))
        assert out.get(DEV) == _iso(fresh_trapped)

    def test_same_alert_multiple_trapped_events_uses_first(self, monkeypatch):
        """Within the SAME alert, multiple trapped events for one device
        with no non-trapped event between them are ONE spell — the timer
        starts at the earliest of them. Regression fence for the (correct)
        pre-existing behaviour."""
        current_alert = NOW - timedelta(hours=1)
        first_trapped = NOW - timedelta(minutes=45)
        later_trapped = NOW - timedelta(minutes=5)
        db = _Db(
            status_events=[
                {"device_id": DEV, "status": "trapped",
                 "recorded_at": _iso(first_trapped)},
                {"device_id": DEV, "status": "trapped",
                 "recorded_at": _iso(later_trapped)},
            ],
            push_events=[
                {"kind": "trigger", "created_at": _iso(current_alert)},
            ],
        )
        _patch_db(monkeypatch, db)
        out = _run(reports_export._trapped_since_map([DEV]))
        assert out.get(DEV) == _iso(first_trapped)

    def test_safe_event_between_two_trapped_still_breaks_spell(self, monkeypatch):
        """The pre-existing rule that a non-trapped event breaks the
        walk-back MUST survive the fix. Prior behaviour: safe → trapped
        starts a new spell at the second trapped event."""
        current_alert  = NOW - timedelta(hours=2)
        first_trapped  = NOW - timedelta(hours=1, minutes=30)
        safe_event     = NOW - timedelta(hours=1)
        second_trapped = NOW - timedelta(minutes=6)
        db = _Db(
            status_events=[
                {"device_id": DEV, "status": "trapped",
                 "recorded_at": _iso(first_trapped)},
                {"device_id": DEV, "status": "safe",
                 "recorded_at": _iso(safe_event)},
                {"device_id": DEV, "status": "trapped",
                 "recorded_at": _iso(second_trapped)},
            ],
            push_events=[
                {"kind": "trigger", "created_at": _iso(current_alert)},
            ],
        )
        _patch_db(monkeypatch, db)
        out = _run(reports_export._trapped_since_map([DEV]))
        assert out.get(DEV) == _iso(second_trapped)

    def test_no_push_events_falls_back_to_unbounded_walk(self, monkeypatch):
        """Bootstrap / test fixtures that only seed status_events (no
        push_events row) must still get a trapped_since — otherwise the
        pre-alert dashboard smoke tests would go blank overnight."""
        old_trapped = NOW - timedelta(minutes=8)
        db = _Db(
            status_events=[
                {"device_id": DEV, "status": "trapped",
                 "recorded_at": _iso(old_trapped)},
            ],
            push_events=[],
        )
        _patch_db(monkeypatch, db)
        out = _run(reports_export._trapped_since_map([DEV]))
        assert out.get(DEV) == _iso(old_trapped)

    def test_legacy_trigger_without_kind_field_still_bounds(self, monkeypatch):
        """Rows written before we started stamping `kind: "trigger"` are
        triggers by convention (server.py:3925). They must count as an
        alert start."""
        legacy_alert = NOW - timedelta(minutes=20)
        prior_trapped = NOW - timedelta(hours=5)      # BEFORE the legacy alert
        fresh_trapped = NOW - timedelta(minutes=2)    # AFTER  the legacy alert
        db = _Db(
            status_events=[
                {"device_id": DEV, "status": "trapped",
                 "recorded_at": _iso(prior_trapped)},
                {"device_id": DEV, "status": "trapped",
                 "recorded_at": _iso(fresh_trapped)},
            ],
            push_events=[
                # NB: no "kind" field on this row.
                {"created_at": _iso(legacy_alert)},
            ],
        )
        _patch_db(monkeypatch, db)
        out = _run(reports_export._trapped_since_map([DEV]))
        assert out.get(DEV) == _iso(fresh_trapped)

    def test_only_prior_alert_trapped_no_current_report(self, monkeypatch):
        """Edge case: a device is currently 'trapped' in device_status but
        the only trapped event in status_events is from BEFORE the current
        alert. That is not a current-alert spell — we return nothing for
        that device, and the dashboard falls back to 'Updated … ago'
        rather than lying about how long they've been trapped."""
        current_alert = NOW - timedelta(minutes=10)
        prior_trapped = NOW - timedelta(hours=6)
        db = _Db(
            status_events=[
                {"device_id": DEV, "status": "trapped",
                 "recorded_at": _iso(prior_trapped)},
            ],
            push_events=[
                {"kind": "trigger", "created_at": _iso(current_alert)},
            ],
        )
        _patch_db(monkeypatch, db)
        out = _run(reports_export._trapped_since_map([DEV]))
        assert DEV not in out, (
            "Devices whose only trapped event predates the current alert "
            "must NOT get a stale trapped_since — that's exactly the bug "
            "Paul reported (2h20m clock shown for a 5-min report)."
        )


class TestCurrentAlertStartHelper:
    """Guards the tight contract of the helper the trapped-since fix
    depends on."""

    def test_returns_latest_trigger_created_at(self, monkeypatch):
        older = NOW - timedelta(hours=5)
        newer = NOW - timedelta(minutes=8)
        db = _Db(push_events=[
            {"kind": "trigger", "created_at": _iso(older)},
            {"kind": "trigger", "created_at": _iso(newer)},
        ])
        _patch_db(monkeypatch, db)
        got = _run(reports_export._current_alert_start())
        assert got == newer

    def test_ignores_alert_stood_down_rows(self, monkeypatch):
        """A stand-down is NOT an alert start. Historically
        `_last_alert_start` conflated these — see #135."""
        trigger   = NOW - timedelta(hours=1)
        stand_down = NOW - timedelta(minutes=5)   # newer than the trigger
        db = _Db(push_events=[
            {"kind": "trigger", "created_at": _iso(trigger)},
            {"kind": "alert_stood_down", "created_at": _iso(stand_down)},
        ])
        _patch_db(monkeypatch, db)
        got = _run(reports_export._current_alert_start())
        assert got == trigger, (
            "The current alert start is the latest TRIGGER, never a stand-down."
        )

    def test_none_when_no_push_events(self, monkeypatch):
        db = _Db(push_events=[])
        _patch_db(monkeypatch, db)
        got = _run(reports_export._current_alert_start())
        assert got is None
