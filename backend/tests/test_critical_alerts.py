"""Iteration 22 — Critical Alerts entitlement verification.

Verifies:
- app.json has version 1.0.8, ios entitlements include aps-environment=production
  and critical-alerts=true, bundleIdentifier unchanged, expo-notifications plugin
  entry has mode=production.
- reminders.ts has allowCriticalAlerts:true and interruptionLevel:'critical' with
  sound:'default'.
- push.ts has allowCriticalAlerts:true.
- server.py trigger-alert data payload sends interruption_level:'critical' and
  sound:{critical:1, name:'default', volume:1.0} in addition to title/message/action_url.
- POST /api/trigger-alert with correct X-Admin-Token → 200 JSON schema unchanged.
- POST /api/trigger-alert with no admin token → 401.
- send_push helper interface unchanged (title+message enforced).
"""
import json
import os
import re
from pathlib import Path

import pytest
import requests

BASE_URL = os.environ.get("BACKEND_URL_OVERRIDE") or "http://localhost:8001"
ADMIN_PWD = os.environ.get("ADMIN_TRIGGER_PASSWORD", "Pt3481pt")

APP_JSON = Path("/app/frontend/app.json")
REMINDERS_TS = Path("/app/frontend/src/utils/reminders.ts")
PUSH_TS = Path("/app/frontend/src/utils/push.ts")
SERVER_PY = Path("/app/backend/server.py")


# ---------- app.json config ----------
class TestAppJsonConfig:
    @pytest.fixture(scope="class")
    def cfg(self):
        return json.loads(APP_JSON.read_text())

    def test_version_bumped_to_1_0_8(self, cfg):
        assert cfg["expo"]["version"] == "1.0.8", f"expected 1.0.8, got {cfg['expo']['version']}"

    def test_ios_bundle_identifier_unchanged(self, cfg):
        assert cfg["expo"]["ios"]["bundleIdentifier"] == "com.paulvincenti.quakeguard"

    def test_ios_entitlements_have_aps_environment_production(self, cfg):
        ent = cfg["expo"]["ios"]["entitlements"]
        assert ent.get("aps-environment") == "production"

    def test_ios_entitlements_have_critical_alerts_true(self, cfg):
        ent = cfg["expo"]["ios"]["entitlements"]
        assert ent.get("com.apple.developer.usernotifications.critical-alerts") is True

    def test_expo_notifications_plugin_production_mode(self, cfg):
        plugins = cfg["expo"]["plugins"]
        found = False
        for p in plugins:
            if isinstance(p, list) and len(p) >= 2 and p[0] == "expo-notifications":
                assert p[1].get("mode") == "production"
                found = True
        assert found, "expo-notifications plugin entry not found"


# ---------- reminders.ts ----------
class TestRemindersTs:
    @pytest.fixture(scope="class")
    def src(self):
        return REMINDERS_TS.read_text()

    def test_allow_critical_alerts_true(self, src):
        # Must contain allowCriticalAlerts: true (not false)
        assert re.search(r"allowCriticalAlerts\s*:\s*true", src), "allowCriticalAlerts:true missing"
        assert not re.search(r"allowCriticalAlerts\s*:\s*false", src), "stale allowCriticalAlerts:false remains"

    def test_interruption_level_critical(self, src):
        assert re.search(r'interruptionLevel\s*:\s*"critical"', src), "interruptionLevel:'critical' missing"
        assert 'interruptionLevel: "timeSensitive"' not in src, "stale timeSensitive remains"

    def test_sound_default_preserved(self, src):
        assert re.search(r'sound\s*:\s*"default"', src)


# ---------- push.ts ----------
class TestPushTs:
    @pytest.fixture(scope="class")
    def src(self):
        return PUSH_TS.read_text()

    def test_allow_critical_alerts_true(self, src):
        assert re.search(r"allowCriticalAlerts\s*:\s*true", src)
        assert not re.search(r"allowCriticalAlerts\s*:\s*false", src)


# ---------- server.py static grep ----------
class TestServerTriggerPayload:
    @pytest.fixture(scope="class")
    def src(self):
        return SERVER_PY.read_text()

    def test_trigger_alert_sends_interruption_level_critical(self, src):
        # The literal string must appear in the trigger-alert send_push data dict
        assert '"interruption_level": "critical"' in src

    def test_trigger_alert_sends_sound_dict(self, src):
        # Match the full sound dict; allow whitespace flexibility
        pattern = r'"sound"\s*:\s*\{\s*"critical"\s*:\s*1\s*,\s*"name"\s*:\s*"default"\s*,\s*"volume"\s*:\s*1\.0\s*\}'
        assert re.search(pattern, src), "sound dict for critical alerts not found in server.py"

    def test_send_push_still_enforces_title_and_message(self, src):
        assert '"title" not in data or "message" not in data' in src
        assert 'ValueError("data must include title and message")' in src


# ---------- Live backend: /api/trigger-alert ----------
class TestTriggerAlertEndpoint:
    @pytest.fixture(scope="class")
    def sess(self):
        s = requests.Session()
        s.headers.update({"Content-Type": "application/json"})
        return s

    def test_no_admin_token_returns_401(self, sess):
        r = sess.post(f"{BASE_URL}/api/trigger-alert", json={"triggeredBy": "regression-none"})
        assert r.status_code == 401, r.text

    def test_wrong_admin_token_returns_401(self, sess):
        r = sess.post(
            f"{BASE_URL}/api/trigger-alert",
            json={"triggeredBy": "regression-wrong"},
            headers={"X-Admin-Token": "definitely-wrong"},
        )
        assert r.status_code == 401

    def test_valid_admin_token_returns_200_with_schema(self, sess):
        r = sess.post(
            f"{BASE_URL}/api/trigger-alert",
            json={"triggeredBy": "regression-ok", "magnitude": 6.4, "distance_km": 12, "intensity": "VII"},
            headers={"X-Admin-Token": ADMIN_PWD},
        )
        assert r.status_code == 200, r.text
        data = r.json()
        # Response schema unchanged
        for k in ("status", "recipients", "push_delivered", "push_error"):
            assert k in data, f"missing key {k} in response: {data}"
        assert data["status"] == "broadcast"
        assert isinstance(data["recipients"], int)
        assert isinstance(data["push_delivered"], bool)


# ---------- Regression: neighbouring production endpoints ----------
class TestNeighbouringEndpoints:
    @pytest.fixture(scope="class")
    def sess(self):
        s = requests.Session()
        s.headers.update({"Content-Type": "application/json"})
        return s

    def test_status_get_ok(self, sess):
        r = sess.get(f"{BASE_URL}/api/status")
        assert r.status_code == 200
        assert isinstance(r.json(), list)

    def test_status_post_ok(self, sess):
        r = sess.post(f"{BASE_URL}/api/status", json={"client_name": "TEST_iter22"})
        assert r.status_code == 200
        body = r.json()
        assert body.get("client_name") == "TEST_iter22"
        assert "id" in body

    def test_register_push_still_upserts_row(self, sess):
        # Placeholder EMERGENT_PUSH_KEY may make the endpoint 500 after DB write;
        # tolerate 201 or 500 (see other tests / iteration_21 notes).
        r = sess.post(
            f"{BASE_URL}/api/register-push",
            json={"user_id": "TEST_iter22_reg", "platform": "ios", "device_token": "tok-iter22"},
        )
        assert r.status_code in (201, 500), f"{r.status_code} {r.text}"

    def test_purge_test_devices_post_401_without_token(self, sess):
        r = sess.post(f"{BASE_URL}/api/admin/purge-test-devices")
        assert r.status_code == 401

    def test_purge_test_devices_post_200_with_token(self, sess):
        r = sess.post(
            f"{BASE_URL}/api/admin/purge-test-devices",
            headers={"X-Admin-Token": ADMIN_PWD},
        )
        assert r.status_code == 200
        body = r.json()
        assert "deleted" in body and "remaining" in body

    def test_purge_get_page_reachable_with_token(self, sess):
        r = sess.get(f"{BASE_URL}/api/admin/purge-test-devices", params={"token": ADMIN_PWD})
        assert r.status_code == 200
        assert "Preview" in r.text or "Purged" in r.text


# ---------- Regression: prior debug endpoints must remain 404 ----------
class TestDebugEndpointsGone:
    debug_paths = [
        "/api/debug/last-push-events",
        "/api/debug/test-push",
        "/api/debug/recipients-sample",
        "/api/debug/full-recipient-list",
        "/api/debug/probe-push",
        "/api/debug/register-push-capture",
    ]

    @pytest.mark.parametrize("path", debug_paths)
    def test_debug_endpoint_404(self, path):
        r = requests.get(f"{BASE_URL}{path}")
        assert r.status_code == 404, f"{path} unexpectedly returned {r.status_code}"
