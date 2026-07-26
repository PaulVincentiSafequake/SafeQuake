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

import pytest
import requests

BASE_URL = os.environ["EXPO_PUBLIC_BACKEND_URL"].rstrip("/")
ADMIN_TOKEN = "Pt3481pt"


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

    def test_purge_seeded_rows_and_leaves_real_row(self, api):
        # Clean baseline: purge whatever leftover test rows are there
        api.post(
            f"{BASE_URL}/api/admin/purge-test-devices",
            headers={"X-Admin-Token": ADMIN_TOKEN},
        )

        # Use a unique real user_id to avoid clobbering real rows if any exist
        real_uid = f"qg-real-{uuid.uuid4().hex[:8]}"

        seed = [
            ("TEST_1", "android", "tok-TEST_1"),
            ("test-x", "android", "tok-test-x"),
            ("diag-y", "ios", "tok-diag-y"),
            ("dashboard", "android", "tok-dashboard"),
            (real_uid, "android", "tok-real"),
        ]
        for uid, platform, tok in seed:
            r = api.post(
                f"{BASE_URL}/api/register-push",
                json={"user_id": uid, "platform": platform, "device_token": tok},
            )
            # In preview env EMERGENT_PUSH_KEY is placeholder → relay returns 401
            # → endpoint raises 500. DB upsert still happens BEFORE relay call.
            assert r.status_code in (201, 500), (
                f"register-push unexpected status for {uid}: {r.status_code} {r.text}"
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

        # Trigger-alert broadcast to remaining recipients — should include our real uid.
        # We use triggeredBy = a random uid so nothing is excluded.
        r2 = api.post(
            f"{BASE_URL}/api/trigger-alert",
            headers={"X-Admin-Token": ADMIN_TOKEN},
            json={"triggeredBy": "no-such-user"},
        )
        assert r2.status_code == 200, r2.text
        # recipients count should equal 'remaining' from purge
        assert r2.json().get("recipients") == body["remaining"]

        # Cleanup: remove our real seed row so tests are idempotent
        # Register again as TEST_ prefix then purge — since no DELETE endpoint exists.
        # Simpler: rely on it being a unique uid; the next test run uses a fresh uid.


# ---------- (e) trigger-alert still works ----------
class TestTriggerAlert:
    def test_trigger_alert_ok(self, api):
        r = api.post(
            f"{BASE_URL}/api/trigger-alert",
            headers={"X-Admin-Token": ADMIN_TOKEN},
            json={"magnitude": 6.4},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "broadcast"
        assert "recipients" in body
        assert "push_delivered" in body

    def test_trigger_alert_unauthorized(self, api):
        r = api.post(f"{BASE_URL}/api/trigger-alert", json={})
        assert r.status_code == 401


# ---------- (f) register-push upserts ----------
class TestRegisterPush:
    def test_register_push_upserts(self, api):
        uid = f"TEST_reg_{uuid.uuid4().hex[:6]}"
        r1 = api.post(
            f"{BASE_URL}/api/register-push",
            json={"user_id": uid, "platform": "ios", "device_token": "tok-a"},
        )
        # In preview env: EMERGENT_PUSH_KEY placeholder → relay 401 → 500.
        # DB upsert still succeeds because it runs before the relay call.
        assert r1.status_code in (201, 500), r1.text
        if r1.status_code == 201:
            assert r1.json() == {"status": "registered"}

        r2 = api.post(
            f"{BASE_URL}/api/register-push",
            json={"user_id": uid, "platform": "ios", "device_token": "tok-b"},
        )
        assert r2.status_code in (201, 500), r2.text

        # Verify DB upsert really happened by checking trigger-alert recipient count.
        r3 = api.post(
            f"{BASE_URL}/api/trigger-alert",
            headers={"X-Admin-Token": ADMIN_TOKEN},
            json={"triggeredBy": "no-such-user"},
        )
        assert r3.status_code == 200
        assert r3.json().get("recipients", 0) >= 1

        # Cleanup
        api.post(
            f"{BASE_URL}/api/admin/purge-test-devices",
            headers={"X-Admin-Token": ADMIN_TOKEN},
        )


# ---------- (g) Legacy status endpoints ----------
class TestLegacyStatus:
    def test_status_post_and_get(self, api):
        name = f"TEST_client_{uuid.uuid4().hex[:6]}"
        r = api.post(f"{BASE_URL}/api/status", json={"client_name": name})
        assert r.status_code == 200, r.text
        created = r.json()
        assert created["client_name"] == name
        assert "id" in created

        r2 = api.get(f"{BASE_URL}/api/status")
        assert r2.status_code == 200
        assert any(s.get("client_name") == name for s in r2.json())


# ---------- (h) server.py should not reference _last_push_events ----------
class TestSourceHygiene:
    def test_no_last_push_events_in_source(self):
        src = Path("/app/backend/server.py").read_text()
        hits = re.findall(r"_last_push_events", src)
        assert hits == [], f"Found lingering _last_push_events refs: {len(hits)}"
