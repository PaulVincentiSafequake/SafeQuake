"""#326 follow-up (2026-01 — iteration 55 testing_agent finding):

The original /api/devices sort was `updated_at desc`, with silent-since-alert
stubs (created by the trigger broadcast, no updated_at yet because the phone
has never checked in) sorting LAST because their updated_at is absent.
Combined with the hard limit=1000 pagination cap, that meant a broadcast
recipient stub could be TRUNCATED off the response whenever the DB held
more than 1000 device_status rows — silently making the P0 red pins
invisible on the operator's map. Paul's rule: "silence must never be
invisible", including through pagination.

The fix (server.py line 544-563): sort key coalesces to
`max(updated_at, last_alerted_at)`, so a broadcast timestamp is treated as
a first-class recency signal. Silent-since-alert stubs sort at the TOP
of the response (their last_alerted_at is the newest signal), which means
they survive any limit-based truncation and always appear on the map.

This test locks that behaviour end-to-end.
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import requests
from dotenv import load_dotenv

load_dotenv("/app/backend/.env")


BASE = "http://localhost:8001"
ADMIN_TOKEN = os.environ["ADMIN_TRIGGER_PASSWORD"]
HDR_ADMIN = {"X-Admin-Token": ADMIN_TOKEN, "Content-Type": "application/json"}


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _new_id(tag: str) -> str:
    """Deterministic-ish device id that WON'T match _is_test_device — we
    want these rows to be counted as real board rows."""
    return f"dev-326sort-{tag}-{uuid.uuid4().hex[:12]}"


def _get_devices(limit=1000, since=None):
    params = {"limit": limit}
    if since:
        params["since"] = since
    r = requests.get(f"{BASE}/api/devices", headers=HDR_ADMIN,
                     params=params, timeout=60)
    assert r.status_code == 200, r.text
    return r.json()


def _find_device(payload, device_id):
    for row in payload.get("devices", []):
        if row.get("device_id") == device_id:
            return row
    return None


# ── Direct-to-Mongo helpers (skip POST /status validators/time logic) ─
def _bulk_upsert_stale(run_async, ids, stale_iso):
    """Insert N rows with old updated_at + no last_alerted_at."""
    import deps
    from pymongo import UpdateOne

    async def _do():
        ops = [
            UpdateOne(
                {"device_id": did},
                {"$set": {"device_id": did, "status": "safe",
                          "updated_at": stale_iso}},
                upsert=True,
            )
            for did in ids
        ]
        # chunk to avoid a single 1000+ op batch trip
        for i in range(0, len(ops), 500):
            await deps.db.device_status.bulk_write(ops[i:i + 500], ordered=False)
    run_async(_do)


def _upsert_silent_stub(run_async, did, alerted_iso):
    """Silent-since-alert stub: has last_alerted_at, NO updated_at."""
    import deps

    async def _do():
        # $unset updated_at explicitly in case a prior test left one behind.
        await deps.db.device_status.update_one(
            {"device_id": did},
            {"$set": {"device_id": did, "status": "not_responding",
                      "last_alerted_at": alerted_iso},
             "$unset": {"updated_at": ""}},
            upsert=True,
        )
    run_async(_do)


def _bulk_delete(run_async, ids):
    import deps

    async def _do():
        await deps.db.device_status.delete_many({"device_id": {"$in": ids}})
        await deps.db.push_devices.delete_many({"user_id": {"$in": ids}})
        await deps.db.status_events.delete_many({"device_id": {"$in": ids}})
    run_async(_do)


# ── THE FIX: silent-since-alert stubs survive a 1000-row truncation ────
class TestSortSurvivesPaginationTruncation:
    """This is exactly the P0 scenario the original report described:
    a large device_status collection PLUS a handful of silent-since-alert
    stubs. Under the OLD sort, the stubs sorted last and got clipped.
    Under the NEW sort (max(updated_at, last_alerted_at)) the stubs sort
    at the TOP because their last_alerted_at is fresher than every
    other row's updated_at."""

    N_STALE = 1050          # more than default limit=1000
    N_SILENT_STUBS = 5      # the P0 recipients that must not fall off

    def test_silent_stubs_are_returned_when_limit_1000(self, run_async):
        stale_ids = [_new_id(f"stale{i}") for i in range(self.N_STALE)]
        stub_ids = [_new_id(f"stub{i}") for i in range(self.N_SILENT_STUBS)]

        # Stale rows: updated_at ~1 year ago, no last_alerted_at
        stale_ts = _iso(_now() - timedelta(days=365))
        # Silent stubs: last_alerted_at ~10 seconds ago, no updated_at
        alerted_ts = _iso(_now() - timedelta(seconds=10))

        _bulk_upsert_stale(run_async, stale_ids, stale_ts)
        for did in stub_ids:
            _upsert_silent_stub(run_async, did, alerted_ts)

        try:
            payload = _get_devices(limit=1000)
            devices = payload.get("devices") or []
            # Sanity: we filled up to the cap.
            assert len(devices) == 1000, (
                f"expected exactly 1000 devices back (hit the cap), got {len(devices)}"
            )

            by_id = {row["device_id"]: row for row in devices}

            # P0: every silent-since-alert stub MUST be in the response.
            missing = [did for did in stub_ids if did not in by_id]
            assert not missing, (
                f"P0 SORT-TRUNCATION REGRESSION: {len(missing)} of "
                f"{self.N_SILENT_STUBS} silent-since-alert stubs were "
                f"paginated off the /api/devices response — first missing: "
                f"{missing[:3]}. 'Silence must never be invisible' (Paul)."
            )

            # And they must render as red silent-since-alert on the map.
            for did in stub_ids:
                row = by_id[did]
                assert row["map_color"] == "red", (
                    f"stub {did}: expected map_color=red, got {row['map_color']!r}"
                )
                assert row["silent_since_alert"] is True, (
                    f"stub {did}: silent_since_alert should be True"
                )
                assert row["last_alerted_at"] == alerted_ts, (
                    f"stub {did}: last_alerted_at mismatch"
                )

            # Extra: stubs should sort near the TOP of the response because
            # their last_alerted_at is a fresh recency signal. Allow for a
            # small amount of pre-existing DB pollution with equally-fresh
            # timestamps by checking the stubs land within the first 50
            # rows rather than the exact first 5. If any stub sinks below
            # position 50 in a 1000-row cap, the sort is broken.
            stub_positions = [i for i, row in enumerate(devices)
                              if row["device_id"] in set(stub_ids)]
            assert len(stub_positions) == self.N_SILENT_STUBS
            worst = max(stub_positions)
            assert worst < 50, (
                f"silent-since-alert stubs sank too deep in the sort — "
                f"worst position {worst} in a 1000-row response. The "
                f"max(updated_at, last_alerted_at) sort should keep them "
                f"near the top so they survive further truncation."
            )
        finally:
            _bulk_delete(run_async, stale_ids + stub_ids)


# ── REGRESSION: normal updated_at ordering still works ────────────────
class TestNoRegressionOnNormalSort:
    """A device with only updated_at set (no last_alerted_at, the normal
    case) must still sort by updated_at — i.e. the newest updated_at is
    still returned first when nothing has been broadcast."""

    def test_newest_updated_at_first_when_no_alerts(self, run_async):
        # 3 devices with different updated_at values; NO last_alerted_at.
        newest = _new_id("newest")
        middle = _new_id("middle")
        oldest = _new_id("oldest")

        import deps

        async def _seed():
            for did, ts in [
                (newest, _iso(_now() - timedelta(seconds=1))),
                (middle, _iso(_now() - timedelta(minutes=5))),
                (oldest, _iso(_now() - timedelta(hours=2))),
            ]:
                await deps.db.device_status.update_one(
                    {"device_id": did},
                    {"$set": {"device_id": did, "status": "safe",
                              "updated_at": ts}},
                    upsert=True,
                )
        run_async(_seed)

        try:
            payload = _get_devices(limit=1000)
            devices = payload.get("devices") or []

            # Find positions of our three seeded devices.
            positions = {}
            for i, row in enumerate(devices):
                if row["device_id"] in (newest, middle, oldest):
                    positions[row["device_id"]] = i

            assert set(positions) == {newest, middle, oldest}, (
                f"missing seeded devices in response: {positions}"
            )
            assert positions[newest] < positions[middle] < positions[oldest], (
                f"NORMAL SORT REGRESSION: newest updated_at should come "
                f"first, then middle, then oldest — got positions {positions}"
            )
        finally:
            _bulk_delete(run_async, [newest, middle, oldest])


# ── REGRESSION: mixed rows still ordered correctly by max() ───────────
class TestMixedSortKeyOrdering:
    """When both fields exist, sort key is max(updated_at, last_alerted_at).
    A row whose last_alerted_at is newer than everyone's updated_at must
    still float to the top."""

    def test_row_with_newer_last_alerted_at_beats_row_with_newer_updated_at(
        self, run_async,
    ):
        # A: recent updated_at (30s ago), no alert. Normal-safe row.
        # B: OLD updated_at (1h ago), but VERY recent last_alerted_at (5s ago).
        #    Under old sort (updated_at desc), A > B.
        #    Under new sort (max(...)), B > A (because 5s > 30s).
        a = _new_id("mixA")
        b = _new_id("mixB")

        import deps

        async def _seed():
            await deps.db.device_status.update_one(
                {"device_id": a},
                {"$set": {"device_id": a, "status": "safe",
                          "updated_at": _iso(_now() - timedelta(seconds=30))}},
                upsert=True,
            )
            await deps.db.device_status.update_one(
                {"device_id": b},
                {"$set": {"device_id": b, "status": "safe",
                          "updated_at": _iso(_now() - timedelta(hours=1)),
                          "last_alerted_at": _iso(_now() - timedelta(seconds=5))}},
                upsert=True,
            )
        run_async(_seed)

        try:
            payload = _get_devices(limit=1000)
            devices = payload.get("devices") or []
            positions = {row["device_id"]: i for i, row in enumerate(devices)
                         if row["device_id"] in (a, b)}
            assert set(positions) == {a, b}, f"missing seeded rows: {positions}"
            assert positions[b] < positions[a], (
                f"SORT KEY REGRESSION: row with fresher last_alerted_at "
                f"({b}) should sort ABOVE row with fresher updated_at "
                f"({a}) — got positions {positions}"
            )
        finally:
            _bulk_delete(run_async, [a, b])
