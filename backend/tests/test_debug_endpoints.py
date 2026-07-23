"""
Backend tests for the new /api/debug/* diagnostic endpoints introduced to
troubleshoot the "no alarm sound, no notification" TestFlight report.

Tests run against localhost:8001. EMERGENT_PUSH_KEY is 'placeholder' → the
upstream Emergent relay will 401 and push_delivered must be False locally.

Spec:
  a) GET /api/debug/devices → 200 with {device_count, push_key_status:'placeholder',
     admin_password_configured:true, devices:[...]}. Each device has
     device_token_preview (NOT full token), user_id, platform, updated_at.
  b) POST /api/debug/test-push with NO header → 401.
  c) POST /api/debug/test-push with X-Admin-Token: Pt3481pt → 200 with
     {recipients, push_delivered:false, push_error:'EMERGENT_PUSH_KEY missing or invalid'}.
  d) POST /api/register-push still upserts to db.push_devices (visible in
     follow-up GET /api/debug/devices).
"""
import uuid
import pytest
import requests

BASE_URL = "http://localhost:8001"
ADMIN_PWD = "Pt3481pt"


@pytest.fixture(scope="module")
def api():
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    return s


# ---------- (a) GET /api/debug/devices ----------
class TestDebugDevices:
    def test_returns_expected_shape(self, api):
        r = api.get(f"{BASE_URL}/api/debug/devices")
        assert r.status_code == 200, r.text
        data = r.json()
        # Required top-level keys
        for k in ("device_count", "push_key_status", "admin_password_configured", "devices"):
            assert k in data, f"missing top-level key {k!r}: {data}"
        assert data["push_key_status"] == "placeholder"
        assert data["admin_password_configured"] is True
        assert isinstance(data["device_count"], int)
        assert isinstance(data["devices"], list)
        assert data["device_count"] == len(data["devices"])

    def test_no_full_token_leaked(self, api):
        """Register a device with a distinctive full token, then verify the
        debug endpoint returns only a preview (never the raw token)."""
        raw = "SUPER_SECRET_TOKEN_" + uuid.uuid4().hex
        user_id = f"TEST_debug_{uuid.uuid4().hex[:6]}"
        api.post(
            f"{BASE_URL}/api/register-push",
            json={"user_id": user_id, "platform": "ios", "device_token": raw},
        )
        r = api.get(f"{BASE_URL}/api/debug/devices")
        assert r.status_code == 200
        devices = r.json()["devices"]
        me = next((d for d in devices if d.get("user_id") == user_id), None)
        assert me is not None, "just-registered device missing from /debug/devices"
        # Must not leak full token key
        assert "device_token" not in me, f"raw device_token leaked: {me}"
        # Preview must exist and not equal the full raw token
        assert "device_token_preview" in me
        assert me["device_token_preview"] != raw
        # Preview must contain the ellipsis marker for long tokens
        assert "…" in me["device_token_preview"]
        # Required per-device fields
        assert me.get("platform") == "ios"
        assert "updated_at" in me


# ---------- (b) auth gate on POST /api/debug/test-push ----------
class TestDebugTestPushAuth:
    def test_missing_header_401(self, api):
        r = requests.post(f"{BASE_URL}/api/debug/test-push")
        assert r.status_code == 401, r.text
        assert r.json().get("detail") == "Invalid or missing X-Admin-Token"

    def test_wrong_header_401(self, api):
        r = requests.post(
            f"{BASE_URL}/api/debug/test-push",
            headers={"X-Admin-Token": "nope"},
        )
        assert r.status_code == 401, r.text


# ---------- (c) POST /api/debug/test-push with correct header ----------
class TestDebugTestPushBroadcast:
    def test_correct_header_200_with_placeholder_key(self, api):
        r = requests.post(
            f"{BASE_URL}/api/debug/test-push",
            headers={"X-Admin-Token": ADMIN_PWD},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        # recipients present as int
        assert isinstance(body.get("recipients"), int)
        # local placeholder key ⇒ push_delivered must be False
        assert body.get("push_delivered") is False
        assert body.get("push_error") == "EMERGENT_PUSH_KEY missing or invalid"


# ---------- (d) register-push visible via debug/devices ----------
class TestRegisterPushVisibleInDebug:
    def test_new_registration_appears_in_debug(self, api):
        user_id = f"TEST_visible_{uuid.uuid4().hex[:6]}"
        api.post(
            f"{BASE_URL}/api/register-push",
            json={"user_id": user_id, "platform": "android", "device_token": "tok-xyz-123456"},
        )
        r = api.get(f"{BASE_URL}/api/debug/devices")
        assert r.status_code == 200
        ids = [d.get("user_id") for d in r.json()["devices"]]
        assert user_id in ids, f"{user_id} missing from debug devices"
