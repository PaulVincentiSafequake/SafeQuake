"""Live integration test for the Paul-reported "Trapped for 2h 20m for a
5-minute report" bug fix.

Seeds a scenario directly in MongoDB (unique test device_id) matching the
verbatim bug: prior alert with a stale trapped event AND a fresh trapped
event after the current alert start. Then hits GET /api/devices on the
PUBLIC preview URL through the admin path and asserts trapped_since is
the FRESH event's recorded_at — never the stale one.

Cleans up all seeded rows after the assertion so no impact on other
consumers of the dashboard.
"""
import os
import time
from datetime import datetime, timedelta, timezone

import pytest
import requests
from pymongo import MongoClient


BASE_URL = os.environ.get("EXPO_PUBLIC_BACKEND_URL", "https://group-size-update.preview.emergentagent.com").rstrip("/")
ADMIN_TOKEN = os.environ.get("ADMIN_TRIGGER_PASSWORD", "m11vRwfDoxnHvIMLkKzjUwQy")
MONGO_URL = os.environ.get("MONGO_URL", "mongodb://localhost:27017")
DB_NAME = os.environ.get("DB_NAME", "test_database")


def _iso(dt: datetime) -> str:
    return dt.isoformat()


@pytest.fixture(scope="module")
def mongo_db():
    client = MongoClient(MONGO_URL)
    yield client[DB_NAME]
    client.close()


@pytest.fixture
def seed_marker():
    """Unique test marker used to identify + clean up seeded rows."""
    return f"TEST_trappedsince_{int(time.time()*1000)}"


class TestTrappedSinceLiveIntegration:
    def test_prior_alert_trapped_does_not_bleed_into_current_alert(
        self, mongo_db, seed_marker
    ):
        """Verbatim Paul scenario against the live /api/devices endpoint."""
        now = datetime.now(timezone.utc)
        test_device_id = f"TEST-{seed_marker}-abcdefgh1234"

        prior_alert   = now - timedelta(hours=3)
        prior_trapped = now - timedelta(hours=2, minutes=20)
        current_alert = now - timedelta(minutes=10)
        fresh_trapped = now - timedelta(minutes=4)

        # ── Seed ──────────────────────────────────────────────────────
        mongo_db.push_events.insert_many([
            {"kind": "trigger", "created_at": _iso(prior_alert),
             "_test_marker": seed_marker},
            {"kind": "trigger", "created_at": _iso(current_alert),
             "_test_marker": seed_marker},
        ])
        mongo_db.status_events.insert_many([
            {"device_id": test_device_id, "status": "trapped",
             "recorded_at": _iso(prior_trapped), "_test_marker": seed_marker},
            {"device_id": test_device_id, "status": "trapped",
             "recorded_at": _iso(fresh_trapped), "_test_marker": seed_marker},
        ])
        mongo_db.device_status.insert_one({
            "device_id": test_device_id,
            "display_name": "TEST Paul-pin",
            "status": "trapped",
            "severity": "unspecified",
            "mobility": "unspecified",
            "latitude": 35.9,
            "longitude": 14.5,
            "updated_at": _iso(fresh_trapped),
            "created_at": _iso(fresh_trapped),
            "is_test": True,
            "_test_marker": seed_marker,
        })

        try:
            # ── Call live endpoint ────────────────────────────────────
            resp = requests.get(
                f"{BASE_URL}/api/devices",
                headers={"X-Admin-Token": ADMIN_TOKEN},
                timeout=30,
            )
            assert resp.status_code == 200, (
                f"GET /api/devices returned {resp.status_code}: {resp.text[:400]}"
            )
            body = resp.json()
            devices = body if isinstance(body, list) else body.get("devices", [])
            assert isinstance(devices, list), f"unexpected shape: {type(body)}"

            match = next(
                (d for d in devices if d.get("device_id") == test_device_id),
                None,
            )
            assert match is not None, (
                f"Seeded test device {test_device_id!r} not present in "
                f"/api/devices response (got {len(devices)} devices)."
            )

            got = match.get("trapped_since")
            expected = _iso(fresh_trapped)
            stale = _iso(prior_trapped)

            assert got != stale, (
                f"BUG REPRODUCED: /api/devices returned the STALE prior-alert "
                f"trapped_since {stale!r} for the fresh report. "
                f"Paul's 2h20m bug is not fixed on the live endpoint."
            )
            assert got == expected, (
                f"trapped_since={got!r} did not match the fresh event "
                f"{expected!r} (stale would have been {stale!r})."
            )
        finally:
            # ── Cleanup — never leave TEST_ rows behind ───────────────
            mongo_db.push_events.delete_many({"_test_marker": seed_marker})
            mongo_db.status_events.delete_many({"_test_marker": seed_marker})
            mongo_db.device_status.delete_many({"_test_marker": seed_marker})

    def test_only_prior_trapped_returns_no_trapped_since(
        self, mongo_db, seed_marker
    ):
        """Device whose ONLY trapped event predates the current alert must
        NOT get any trapped_since — that's the exact wording of the bug
        (stale timer shown when there is no current-alert report)."""
        now = datetime.now(timezone.utc)
        test_device_id = f"TEST-{seed_marker}-onlystale567"

        current_alert = now - timedelta(minutes=10)
        prior_trapped = now - timedelta(hours=6)

        mongo_db.push_events.insert_one(
            {"kind": "trigger", "created_at": _iso(current_alert),
             "_test_marker": seed_marker}
        )
        mongo_db.status_events.insert_one({
            "device_id": test_device_id, "status": "trapped",
            "recorded_at": _iso(prior_trapped),
            "_test_marker": seed_marker,
        })
        mongo_db.device_status.insert_one({
            "device_id": test_device_id,
            "display_name": "TEST only-stale",
            "status": "trapped",
            "latitude": 35.9,
            "longitude": 14.5,
            "updated_at": _iso(prior_trapped),
            "created_at": _iso(prior_trapped),
            "is_test": True,
            "_test_marker": seed_marker,
        })

        try:
            resp = requests.get(
                f"{BASE_URL}/api/devices",
                headers={"X-Admin-Token": ADMIN_TOKEN},
                timeout=30,
            )
            assert resp.status_code == 200
            body = resp.json()
            devices = body if isinstance(body, list) else body.get("devices", [])
            match = next(
                (d for d in devices if d.get("device_id") == test_device_id),
                None,
            )
            assert match is not None, "seeded device missing from /api/devices"
            assert not match.get("trapped_since"), (
                f"Device with only a pre-alert trapped event returned "
                f"trapped_since={match.get('trapped_since')!r}; expected null."
            )
        finally:
            mongo_db.push_events.delete_many({"_test_marker": seed_marker})
            mongo_db.status_events.delete_many({"_test_marker": seed_marker})
            mongo_db.device_status.delete_many({"_test_marker": seed_marker})
