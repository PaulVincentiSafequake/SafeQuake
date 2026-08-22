"""
Backend cleanup verification tests (iteration 19).

Covers:
- All /api/debug/* endpoints removed (404)
- POST /api/admin/purge-test-devices auth + filtering behaviour
- /api/trigger-alert still works with admin token
- /api/register-push upserts device rows
- Legacy /api/status GET+POST work
- server.py no longer references _last_push_events
"""
import os
import re
import uuid
from pathlib import Path

import pymongo
import pytest
import requests
from dotenv import load_dotenv

load_dotenv("/app/backend/.env")

BASE_URL = os.environ.get("BACKEND_URL_OVERRIDE") or "http://localhost:8001"
ADMIN_TOKEN = os.environ["ADMIN_TRIGGER_PASSWORD"]

from server import TRIGGER_ALERT_CONFIRMATION  # noqa: E402


@pytest.fixture(scope="module")
def devices():
    m = pymongo.MongoClient(os.environ["MONGO_URL"])
    yield m[os.environ.get("DB_NAME", "test_database")].push_devices
    m.close()


@pytest.fixture(scope="module")
def api():
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    return s


# ---------- (a) All debug endpoints must be 404 ----------
class TestDebugEndpointsRemoved:
    def test_debug_devices_get(self, api):
        r = api.get(f"{BASE_URL}/api/debug/devices")
        assert r.status_code == 404, r.text

    def test_debug_test_push_get(self, api):
        r = api.get(f"{BASE_URL}/api/debug/test-push", params={"token": ADMIN_TOKEN})
        assert r.status_code == 404, r.text

    def test_debug_test_push_post(self, api):
        r = api.post(
            f"{BASE_URL}/api/debug/test-push",
            headers={"X-Admin-Token": ADMIN_TOKEN},
            json={},
        )
        assert r.status_code == 404, r.text

    def test_debug_last_push_events(self, api):
        r = api.get(
            f"{BASE_URL}/api/debug/last-push-events",
            params={"token": ADMIN_TOKEN},
        )
        assert r.status_code == 404, r.text

    def test_debug_probe_push(self, api):
        r = api.get(
            f"{BASE_URL}/api/debug/probe-push",
            params={"token": ADMIN_TOKEN, "variant": "A"},
        )
        assert r.status_code == 404, r.text


# ---------- (b-d) purge-test-devices ----------
class TestPurgeTestDevices:
    def test_no_header_returns_401(self, api):
        r = api.post(f"{BASE_URL}/api/admin/purge-test-devices")
        assert r.status_code == 401
        assert r.json() == {"detail": "Invalid or missing X-Admin-Token"}

    def test_wrong_header_returns_401(self, api):
        r = api.post(
            f"{BASE_URL}/api/admin/purge-test-devices",
            headers={"X-Admin-Token": "wrong"},
        )
        assert r.status_code == 401
        assert r.json() == {"detail": "Invalid or missing X-Admin-Token"}

    def test_purge_seeded_rows_and_leaves_real_row(self, api, devices, stand_down_after):
        # Clean baseline: purge whatever leftover test rows are there
        api.post(
            f"{BASE_URL}/api/admin/purge-test-devices",
            headers={"X-Admin-Token": ADMIN_TOKEN},
        )

        # Use a unique real user_id to avoid clobbering real rows if any exist
        real_uid = f"qg-real-{uuid.uuid4().hex[:8]}"

        seed = [
            ("TEST_1", "android"),
            ("test-x", "android"),
            ("diag-y", "ios"),
            ("dashboard", "android"),
            (real_uid, "android"),
        ]
        # Seeded straight into Mongo: since #266 /register-push only files a
        # row when the push provider accepts it, and this environment's
        # EMERGENT_PUSH_KEY is a placeholder, so every registration is
        # refused by design.
        for uid, platform in seed:
            devices.update_one(
                {"user_id": uid},
                {"$set": {"user_id": uid, "platform": platform,
                          "device_token": f"tok-{uid}"}},
                upsert=True,
            )

        # Purge
        r = api.post(
            f"{BASE_URL}/api/admin/purge-test-devices",
            headers={"X-Admin-Token": ADMIN_TOKEN},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert set(body.keys()) == {"deleted", "remaining"}
        assert isinstance(body["deleted"], int)
        assert isinstance(body["remaining"], int)
        # At least the 4 test-shaped rows we just seeded should have been deleted
        assert body["deleted"] >= 4
        # The real row is untouched.
        assert devices.count_documents({"user_id": real_uid}) == 1

        # Trigger-alert broadcast to remaining recipients — should include our real uid.
        # We use triggeredBy = a random uid so nothing is excluded.
        r2 = api.post(
            f"{BASE_URL}/api/trigger-alert",
            headers={"X-Admin-Token": ADMIN_TOKEN},
            json={"triggeredBy": "no-such-user",
                  "confirmation_phrase": TRIGGER_ALERT_CONFIRMATION},
        )
        assert r2.status_code == 200, r2.text
        # Recipients are the remaining rows minus any the push provider has
        # already told us are dead (#262 soft-marks those instead of
        # deleting them, so `remaining` can be the larger number).
        live = devices.count_documents({"dead_token": {"$ne": True}})
        assert r2.json().get("recipients") == live
        assert live <= body["remaining"]

        devices.delete_one({"user_id": real_uid})


# ---------- (e) trigger-alert still works ----------
class TestTriggerAlert:
    def test_trigger_alert_ok(self, api, stand_down_after):
        r = api.post(
            f"{BASE_URL}/api/trigger-alert",
            headers={"X-Admin-Token": ADMIN_TOKEN},
            json={"magnitude": 6.4,
                  "confirmation_phrase": TRIGGER_ALERT_CONFIRMATION},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "broadcast"
        assert "recipients" in body
        assert "push_delivered" in body

    def test_trigger_alert_unauthorized(self, api):
        r = api.post(f"{BASE_URL}/api/trigger-alert", json={})
        assert r.status_code == 401


# ---------- (f) register-push refuses what it cannot deliver to ----------
class TestRegisterPush:
    def test_register_push_does_not_file_a_row_the_relay_refused(
        self, api, devices, clear_register_rate_limit,
    ):
        uid = f"TEST_reg_{uuid.uuid4().hex[:6]}"
        r = api.post(
            f"{BASE_URL}/api/register-push",
            json={"user_id": uid, "platform": "ios", "device_token": "a" * 64},
        )
        # #266: placeholder EMERGENT_PUSH_KEY → relay 401 → 502 and NO row,
        # so the phone is never told it is on the alert list when it isn't.
        assert r.status_code == 502, r.text
        assert devices.count_documents({"user_id": uid}) == 0


# ---------- (g) status endpoints ----------
class TestStatusEndpoints:
    def test_status_post_and_get(self, api):
        device_id = f"TEST_cleanup_{uuid.uuid4().hex[:6]}"
        r = api.post(f"{BASE_URL}/api/status",
                     json={"deviceId": device_id, "status": "safe"})
        assert r.status_code == 200, r.text
        created = r.json()
        assert created["status"] == "ok"
        assert created["device_id"] == device_id

        r2 = api.get(f"{BASE_URL}/api/status")
        assert r2.status_code == 200
        assert isinstance(r2.json(), list)


# ---------- (h) server.py should not reference _last_push_events ----------
class TestSourceHygiene:
    def test_no_last_push_events_in_source(self):
        src = Path("/app/backend/server.py").read_text()
        hits = re.findall(r"_last_push_events", src)
        assert hits == [], f"Found lingering _last_push_events refs: {len(hits)}"
