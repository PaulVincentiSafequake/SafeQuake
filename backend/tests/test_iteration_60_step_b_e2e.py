"""#331 (Paul, 2026-08-29) — Step B end-to-end HTTP verification.

Verifies the map_color fix against the deployed API through the public
EXPO_PUBLIC_BACKEND_URL (what the operator's browser actually sees).

Uses pymongo (synchronous) for the small direct-DB mutations so we do
not need to share Motor's event loop.
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import pymongo
import pytest
import requests
from dotenv import load_dotenv

load_dotenv("/app/backend/.env")

BASE = (os.environ.get("EXPO_PUBLIC_BACKEND_URL")
        or "https://rescue-alert-hub-3.preview.emergentagent.com").rstrip("/")
ADMIN_TOKEN = os.environ["ADMIN_TRIGGER_PASSWORD"]
HDR_ADMIN = {"X-Admin-Token": ADMIN_TOKEN, "Content-Type": "application/json"}


@pytest.fixture(scope="module")
def mongo():
    m = pymongo.MongoClient(os.environ["MONGO_URL"])
    db = m[os.environ.get("DB_NAME", "test_database")]
    yield db
    m.close()


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _new_id(tag: str) -> str:
    return f"qg-e2e-331-{tag}-{uuid.uuid4().hex[:10]}"


def _cleanup(mongo, device_id: str) -> None:
    mongo.device_status.delete_many({"device_id": device_id})
    mongo.push_devices.delete_many({"user_id": device_id})
    mongo.status_events.delete_many({"device_id": device_id})


def _find(payload, device_id):
    for row in payload.get("devices", []):
        if row.get("device_id") == device_id:
            return row
    return None


class TestRescuedThenAlertedShowsRedThroughPublicApi:
    """Bug #331 reproduction and fix verification through the public URL."""

    def test_rescued_plus_alert_after_returns_red_and_preserves_history(self, mongo):
        did = _new_id("A")
        try:
            # 1. Seed device as trapped
            r = requests.post(
                f"{BASE}/api/status",
                json={
                    "deviceId": did,
                    "status": "trapped",
                    "severity": "red",
                    "latitude": 35.9,
                    "longitude": 14.5,
                },
                timeout=15,
            )
            assert r.status_code == 200, r.text

            # 2. Mark rescued (admin auth)
            r2 = requests.post(
                f"{BASE}/api/mark-rescued",
                headers=HDR_ADMIN,
                json={"device_id": did, "rescued_by": "e2e_test"},
                timeout=15,
            )
            assert r2.status_code == 200, r2.text

            # Sanity check pre-alert
            r_pre = requests.get(f"{BASE}/api/devices", headers=HDR_ADMIN,
                                 params={"limit": 5000}, timeout=30)
            assert r_pre.status_code == 200, r_pre.text
            row_pre = _find(r_pre.json(), did)
            assert row_pre is not None, "seeded device missing from /api/devices"
            assert row_pre["status"] == "rescued"
            assert row_pre.get("rescued_at")
            rescued_at = row_pre["rescued_at"]
            rescued_by = row_pre.get("rescued_by")
            assert row_pre["map_color"] is None, (
                f"pre-alert rescued row should be off the live map, "
                f"got {row_pre['map_color']!r}"
            )

            # 3. Simulate an alert fire AFTER rescued_at by writing
            # last_alerted_at directly to Mongo (as suggested in the
            # review request).
            alerted_time = _now() + timedelta(seconds=2)
            res = mongo.device_status.update_one(
                {"device_id": did},
                {"$set": {"last_alerted_at": _iso(alerted_time)}},
            )
            assert res.matched_count == 1

            # 4. GET /api/devices, assert red + silent_since_alert
            r3 = requests.get(f"{BASE}/api/devices", headers=HDR_ADMIN,
                              params={"limit": 5000}, timeout=30)
            assert r3.status_code == 200, r3.text
            row = _find(r3.json(), did)
            assert row is not None

            assert row["map_color"] == "red", (
                f"BUG #331 REGRESSION: rescued+alerted row should be RED, "
                f"got {row['map_color']!r}"
            )
            assert row["silent_since_alert"] is True, (
                f"silent_since_alert should be True, got {row.get('silent_since_alert')!r}"
            )

            # 5. History preserved — this is the doctrine
            # "Nothing is deleted — their earlier rescue stays in the history."
            assert row.get("rescued_at") == rescued_at, (
                f"rescued_at was NOT preserved after alert: "
                f"{row.get('rescued_at')!r} vs {rescued_at!r}"
            )
            if rescued_by:
                assert row.get("rescued_by") == rescued_by, (
                    "rescued_by should be preserved"
                )
            assert row.get("status") == "rescued", (
                f"raw status must still be 'rescued', got {row.get('status')!r}"
            )
        finally:
            _cleanup(mongo, did)

    def test_safe_plus_alert_after_still_red(self, mongo):
        """Regression guard: the pre-existing safe+silent path must
        continue to return red. This is the state that already worked
        before #331; confirm the fix did not break it."""
        did = _new_id("B")
        try:
            r = requests.post(
                f"{BASE}/api/status",
                json={"deviceId": did, "status": "safe"},
                timeout=15,
            )
            assert r.status_code == 200, r.text

            # Push updated_at into the past, then stamp a newer last_alerted_at
            mongo.device_status.update_one(
                {"device_id": did},
                {"$set": {
                    "updated_at": _iso(_now() - timedelta(hours=6)),
                    "last_alerted_at": _iso(_now()),
                }},
            )

            r2 = requests.get(f"{BASE}/api/devices", headers=HDR_ADMIN,
                              params={"limit": 5000}, timeout=30)
            assert r2.status_code == 200
            row = _find(r2.json(), did)
            assert row is not None
            assert row["map_color"] == "red", (
                f"safe+silent-since-alert should be RED, got {row['map_color']!r}"
            )
            assert row["silent_since_alert"] is True
            # Status untouched — only the map treatment changes.
            assert row.get("status") == "safe"
        finally:
            _cleanup(mongo, did)
