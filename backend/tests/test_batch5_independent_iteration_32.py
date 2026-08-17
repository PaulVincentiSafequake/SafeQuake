"""Iteration 32 — INDEPENDENT verification of the Batch 5 review checklist.

Written separately from the developer's own tests/test_batch5.py so a
different set of eyes hits the same contract:
  * B1 reminder kill-switch — auth, response contract, audit-log write,
    idempotency-key uniqueness, silent-push shape (via source inspection).
  * B8 places CRUD — defaults, add/list/delete, cap enforcement message,
    whole-feature switch, 422 on invalid lat/lon, 422/400 on missing name.
  * Regressions the checklist called out: GET /api/devices auth gate,
    GET /api/public/summary anon-allowed, casualty PDFs (B1 operational +
    B2 public) return real PDFs with X-Admin-Token, audit-log CSV/PDF
    export.
  * B9 payload contract structurally re-verified via apns.py source.
"""
import os
import re
import uuid

import pytest
import requests
from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv("/app/backend/.env")
load_dotenv("/app/frontend/.env")

BASE_URL = (
    os.environ.get("EXPO_PUBLIC_BACKEND_URL")
    or os.environ.get("EXPO_BACKEND_URL")
    or "http://localhost:8001"
).rstrip("/")
ADMIN_TOKEN = os.environ.get("ADMIN_TRIGGER_PASSWORD")
MONGO_URL = os.environ.get("MONGO_URL") or "mongodb://localhost:27017"
DB_NAME = os.environ.get("DB_NAME") or "test_database"
ADMIN_HDR = {"X-Admin-Token": ADMIN_TOKEN}
CANCEL_URL = f"{BASE_URL}/api/admin/reminders/cancel"


@pytest.fixture(scope="module")
def db():
    c = MongoClient(MONGO_URL)
    yield c[DB_NAME]
    c.close()


@pytest.fixture
def device_id(db):
    did = f"qg-iter32-{uuid.uuid4().hex[:8]}"
    yield did
    db.user_places.delete_many({"device_id": did})
    db.push_devices.delete_many({"user_id": did})


# ── B1: reminder kill-switch ─────────────────────────────────────────────
class TestB1KillSwitch:
    def test_no_auth_returns_401_or_403(self):
        r = requests.post(CANCEL_URL, timeout=30)
        assert r.status_code in (401, 403), r.status_code

    def test_bad_token_returns_401_or_403(self):
        r = requests.post(
            CANCEL_URL, headers={"X-Admin-Token": "totally-wrong"}, timeout=30,
        )
        assert r.status_code in (401, 403), r.status_code

    def test_admin_token_returns_full_response_contract(self, db):
        r = requests.post(CANCEL_URL, headers=ADMIN_HDR, timeout=60)
        assert r.status_code == 200, r.text
        data = r.json()
        for key in ("ok", "targeted", "delivered", "silent", "idempotency_key"):
            assert key in data, f"missing key {key} in {data}"
        assert data["ok"] is True
        assert data["silent"] is True
        assert isinstance(data["targeted"], int) and data["targeted"] >= 0
        assert isinstance(data["delivered"], int) and data["delivered"] >= 0
        assert data["delivered"] <= data["targeted"]
        assert data["idempotency_key"].startswith("cancel-reminders-")

    def test_audit_row_written_with_matching_key(self, db):
        r = requests.post(CANCEL_URL, headers=ADMIN_HDR, timeout=60)
        assert r.status_code == 200
        idem = r.json()["idempotency_key"]
        row = db.emsc_audit_log.find_one(
            {"event_type": "reminders_cancelled",
             "context.idempotency_key": idem},
        )
        assert row is not None, "audit row not written"
        assert row["context"]["silent"] is True
        assert isinstance(row["context"]["targeted"], int)

    def test_two_calls_produce_distinct_idempotency_keys(self):
        r1 = requests.post(CANCEL_URL, headers=ADMIN_HDR, timeout=60).json()
        r2 = requests.post(CANCEL_URL, headers=ADMIN_HDR, timeout=60).json()
        assert r1["idempotency_key"] != r2["idempotency_key"]

    def test_silent_push_source_shape(self):
        """The point of the switch: cancelling noise mustn't make noise."""
        src = open("/app/backend/apns.py").read()
        fn = src.split("async def send_silent_cancel_reminders")[1]
        # only inspect the body of this function, not the whole file
        fn = fn.split("\n\n\n")[0]
        assert '"aps": {"content-available": 1}' in fn
        assert 'push_type="background"' in fn
        assert 'apns_priority="5"' in fn
        assert '"kind": "cancel_reminders"' in fn
        # NEVER carry an alert or sound
        assert '"alert"' not in fn, "silent cancel must not have an alert"
        assert '"sound"' not in fn, "silent cancel must not have a sound"


# ── B8: places I care about ──────────────────────────────────────────────
class TestB8Places:
    def _url(self, did): return f"{BASE_URL}/api/devices/{did}/places"

    def test_unknown_device_defaults(self, device_id):
        r = requests.get(self._url(device_id), timeout=30)
        assert r.status_code == 200
        data = r.json()
        assert data["places"] == []
        assert data["enabled"] is True
        assert data["max_places"] == 5

    def test_create_returns_place_id_and_get_lists_it(self, device_id):
        payload = {"name": "Family in Sicily", "latitude": 37.5, "longitude": 15.09}
        r = requests.post(self._url(device_id), json=payload, timeout=30)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is True
        pid = body["place"]["place_id"]
        assert isinstance(pid, str) and len(pid) >= 8

        listed = requests.get(self._url(device_id), timeout=30).json()["places"]
        assert len(listed) == 1
        assert listed[0]["name"] == "Family in Sicily"
        assert listed[0]["place_id"] == pid
        assert listed[0]["latitude"] == pytest.approx(37.5)

    def test_delete_removes_place_and_404s_on_unknown(self, device_id):
        r = requests.post(
            self._url(device_id),
            json={"name": "Ragusa flat", "latitude": 36.9, "longitude": 14.7},
            timeout=30,
        ).json()
        pid = r["place"]["place_id"]

        d = requests.delete(f"{self._url(device_id)}/{pid}", timeout=30)
        assert d.status_code == 200
        after = requests.get(self._url(device_id), timeout=30).json()["places"]
        assert after == []

        bad = requests.delete(f"{self._url(device_id)}/nope-nope-nope", timeout=30)
        assert bad.status_code == 404

    def test_sixth_place_rejected_with_limit_message(self, device_id):
        for i in range(5):
            r = requests.post(
                self._url(device_id),
                json={"name": f"Place{i}", "latitude": 36.0 + i / 10, "longitude": 14.4},
                timeout=30,
            )
            assert r.status_code == 200, r.text
        over = requests.post(
            self._url(device_id),
            json={"name": "one too many", "latitude": 36.9, "longitude": 14.4},
            timeout=30,
        )
        assert over.status_code == 400
        assert "5" in over.json()["detail"], over.text

    def test_whole_feature_switch_persists(self, device_id):
        r = requests.post(
            f"{self._url(device_id)}/enabled", json={"enabled": False}, timeout=30,
        )
        assert r.status_code == 200
        assert requests.get(self._url(device_id), timeout=30).json()["enabled"] is False
        # Toggle back on
        requests.post(
            f"{self._url(device_id)}/enabled", json={"enabled": True}, timeout=30,
        )
        assert requests.get(self._url(device_id), timeout=30).json()["enabled"] is True

    def test_latitude_out_of_range_is_422_not_500(self, device_id):
        r = requests.post(
            self._url(device_id),
            json={"name": "Impossible", "latitude": 999.0, "longitude": 14.4},
            timeout=30,
        )
        assert r.status_code in (400, 422), r.status_code
        assert r.status_code != 500

    def test_missing_name_is_422_or_400_not_500(self, device_id):
        r = requests.post(
            self._url(device_id),
            json={"latitude": 36.0, "longitude": 14.4},
            timeout=30,
        )
        assert r.status_code in (400, 422)

    def test_empty_name_string_rejected(self, device_id):
        r = requests.post(
            self._url(device_id),
            json={"name": "", "latitude": 36.0, "longitude": 14.4},
            timeout=30,
        )
        assert r.status_code in (400, 422)

    def test_longitude_out_of_range_is_422(self, device_id):
        r = requests.post(
            self._url(device_id),
            json={"name": "Bad", "latitude": 36.0, "longitude": 999.0},
            timeout=30,
        )
        assert r.status_code in (400, 422)


# ── B9: category structural check (payload builders) ─────────────────────
class TestB9Category:
    def test_preview_has_category_critical_has_none(self):
        src = open("/app/backend/apns.py").read()
        assert "TREMOR_CATEGORY_ID" in src
        # Structural: category must live in preview builder, not critical.
        preview_fn = src.split("def _build_preview_payload")[1].split("\n\n\n")[0]
        critical_fn = src.split("def _build_critical_payload")[1].split("\n\n\n")[0]
        assert '"category"' in preview_fn
        assert '"category"' not in critical_fn


# ── REGRESSION: legacy auth gates & anonymous endpoints ──────────────────
class TestRegressionAuthGates:
    def test_get_devices_needs_admin_token(self):
        r = requests.get(f"{BASE_URL}/api/devices", timeout=30)
        assert r.status_code in (401, 403)

    def test_get_devices_ok_with_admin_token(self):
        r = requests.get(f"{BASE_URL}/api/devices", headers=ADMIN_HDR, timeout=30)
        assert r.status_code == 200, r.text
        # Response should include an array of devices somewhere.
        data = r.json()
        assert isinstance(data, (list, dict))

    def test_public_summary_is_anon(self):
        r = requests.get(f"{BASE_URL}/api/public/summary", timeout=30)
        assert r.status_code == 200, r.text

    def test_operational_pdf_returns_pdf(self):
        r = requests.get(
            f"{BASE_URL}/api/admin/casualty-report/operational.pdf",
            headers=ADMIN_HDR, params={"detail": "summary"}, timeout=60,
        )
        assert r.status_code == 200, r.text[:200]
        assert r.content[:4] == b"%PDF", "not a PDF file"

    def test_public_pdf_returns_pdf(self):
        r = requests.get(
            f"{BASE_URL}/api/admin/casualty-report/public.pdf",
            headers=ADMIN_HDR, timeout=60,
        )
        assert r.status_code == 200, r.text[:200]
        assert r.content[:4] == b"%PDF"

    def test_audit_log_csv_export(self):
        r = requests.get(
            f"{BASE_URL}/api/admin/audit-log/export.csv",
            headers=ADMIN_HDR, timeout=60,
        )
        assert r.status_code == 200
        # CSV should be text, not JSON error
        assert "application/pdf" not in r.headers.get("content-type", "")

    def test_audit_log_pdf_export(self):
        r = requests.get(
            f"{BASE_URL}/api/admin/audit-log/export.pdf",
            headers=ADMIN_HDR, timeout=60,
        )
        assert r.status_code == 200
        assert r.content[:4] == b"%PDF"


# ── REGRESSION: notification-preset still accepts 4 values, rejects junk ─
class TestRegressionNotificationPresets:
    @pytest.mark.parametrize("preset", ["off", "significant", "noticeable", "everything"])
    def test_valid_presets_accepted(self, preset, device_id):
        r = requests.post(
            f"{BASE_URL}/api/devices/notification-preset",
            json={"device_id": device_id, "preset": preset}, timeout=30,
        )
        assert r.status_code == 200, r.text
        assert r.json()["preset"] == preset

    def test_rubbish_preset_rejected_400(self, device_id):
        r = requests.post(
            f"{BASE_URL}/api/devices/notification-preset",
            json={"device_id": device_id, "preset": "rubbish"}, timeout=30,
        )
        assert r.status_code == 400


# ── App-side copy sanity (cheap re-check) ────────────────────────────────
class TestAppCopy:
    def test_no_wake_promises_in_frontend(self):
        offenders = []
        for root, _dirs, files in os.walk("/app/frontend/app"):
            for f in files:
                if not f.endswith((".tsx", ".ts")):
                    continue
                p = os.path.join(root, f)
                for m in re.finditer(
                    r"\b(woken|wake you|wake the user|wake your phone)\b",
                    open(p).read(),
                ):
                    offenders.append(f"{p}: {m.group(0)}")
        assert offenders == [], offenders

    def test_settings_notifications_has_three_options(self):
        s = open("/app/frontend/app/settings/notifications.tsx").read()
        opts = re.findall(r'value: "(off|significant|noticeable|everything)"', s)
        # Exactly three visible options: off / noticeable / everything
        assert opts == ["off", "noticeable", "everything"], opts

    def test_alert_screen_dismiss_button_removed(self):
        alert = open("/app/frontend/app/alert.tsx").read()
        assert "alert-dismiss-btn" not in alert
        assert "alert-back-home-btn" in alert

    def test_places_screen_shows_safety_line(self):
        s = open("/app/frontend/app/settings/places.tsx").read()
        # phrasing may vary but the reassurance must exist
        assert "This never changes the emergency alert" in s or \
               "never changes the emergency alert" in s

    def test_settings_notifications_footer_says_alerted_not_woken(self):
        s = open("/app/frontend/app/settings/notifications.tsx").read()
        assert "alerted by the siren" in s
        assert "woken" not in s
