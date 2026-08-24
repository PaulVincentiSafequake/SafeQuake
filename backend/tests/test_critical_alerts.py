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
import uuid
from pathlib import Path

import pytest
import requests
from dotenv import load_dotenv

load_dotenv("/app/backend/.env")

BASE_URL = os.environ.get("BACKEND_URL_OVERRIDE") or "http://localhost:8001"
ADMIN_PWD = os.environ["ADMIN_TRIGGER_PASSWORD"]
# #245: a real alert needs the operator to type the phrase naming the
# consequence. Imported rather than hardcoded so a change to the phrase
# doesn't quietly turn this into a test of nothing.
from server import TRIGGER_ALERT_CONFIRMATION  # noqa: E402

APP_JSON = Path("/app/frontend/app.json")
REMINDERS_TS = Path("/app/frontend/src/utils/reminders.ts")
PUSH_TS = Path("/app/frontend/src/utils/push.ts")
SERVER_PY = Path("/app/backend/server.py")
APNS_PY = Path("/app/backend/apns.py")
PUSH_RELAY_PY = Path("/app/backend/push_relay.py")


# ---------- app.json config ----------
class TestAppJsonConfig:
    @pytest.fixture(scope="class")
    def cfg(self):
        return json.loads(APP_JSON.read_text())

    def test_version_is_at_least_1_0_8(self, cfg):
        # Critical Alerts entitlements shipped in 1.0.8 — the app has moved
        # on since, so assert "not rolled back below that", not equality.
        parts = tuple(int(p) for p in cfg["expo"]["version"].split("."))
        assert parts >= (1, 0, 8), f"version regressed to {cfg['expo']['version']}"

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

    def test_only_the_first_reminder_is_a_critical_alert(self, src):
        # #207/#296 (2026-08-24): the first reminder still breaches the
        # silent switch, in case they slept through the alert. The rest go
        # out `time-sensitive` — still through Focus and Do Not Disturb, but
        # not eight full-volume Critical Alerts in a row, which is how you
        # teach people to ignore the one alarm that matters.
        assert re.search(
            r'interruptionLevel:\s*i === 0 \? "critical" : "timeSensitive"', src,
        ), "reminder ladder should be critical for i===0 and timeSensitive after"
        assert 'interruptionLevel: "critical"' not in src, (
            "an unconditionally critical reminder is back — see #296"
        )

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


# ---------- APNs payload static grep ----------
# The critical-alert payload moved out of server.py into apns.py (direct
# APNs path) and the relay helper into push_relay.py.
class TestServerTriggerPayload:
    @pytest.fixture(scope="class")
    def apns_src(self):
        return APNS_PY.read_text()

    @pytest.fixture(scope="class")
    def relay_src(self):
        return PUSH_RELAY_PY.read_text()

    def test_alert_payload_is_interruption_level_critical(self, apns_src):
        assert '"interruption-level": "critical"' in apns_src

    def test_alert_payload_sends_critical_sound_dict(self, apns_src):
        pattern = (
            r'"sound"\s*:\s*\{\s*"critical"\s*:\s*1\s*,\s*"name"\s*:\s*'
            r'"[^"]+"\s*,\s*"volume"\s*:\s*1\.0\s*\}'
        )
        assert re.search(pattern, apns_src), "critical sound dict not found in apns.py"

    def test_send_push_still_enforces_title_and_message(self, relay_src):
        assert '"title" not in data or "message" not in data' in relay_src
        assert 'ValueError("data must include title and message")' in relay_src


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

    def test_valid_admin_token_returns_200_with_schema(self, sess, stand_down_after):
        r = sess.post(
            f"{BASE_URL}/api/trigger-alert",
            json={
                "triggeredBy": "regression-ok",
                "magnitude": 6.4,
                "distance_km": 12,
                "intensity": "VII",
                "confirmation_phrase": TRIGGER_ALERT_CONFIRMATION,
            },
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

    def test_missing_confirmation_phrase_returns_400(self, sess):
        r = sess.post(
            f"{BASE_URL}/api/trigger-alert",
            json={"triggeredBy": "regression-no-phrase"},
            headers={"X-Admin-Token": ADMIN_PWD},
        )
        assert r.status_code == 400, r.text
        assert TRIGGER_ALERT_CONFIRMATION in r.json()["detail"]


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
        device_id = f"TEST_iter22_{uuid.uuid4().hex[:6]}"
        r = sess.post(
            f"{BASE_URL}/api/status",
            json={"deviceId": device_id, "status": "safe"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body.get("status") == "ok"
        assert body.get("device_id") == device_id

    def test_register_push_refuses_a_malformed_token(self, sess):
        # #266 doctrine: a device row is only written when the push provider
        # accepts the registration, and a token that cannot be real is
        # refused outright rather than filed as a false promise.
        r = sess.post(
            f"{BASE_URL}/api/register-push",
            json={"user_id": "TEST_iter22_reg", "platform": "ios",
                  "device_token": "tok-iter22"},
        )
        assert r.status_code == 400, f"{r.status_code} {r.text}"

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
