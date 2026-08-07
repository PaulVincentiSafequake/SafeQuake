"""Tests for GET /api/debug/probe-push?variant=A..F

Verifies:
- baseline (A) uses the same payload as /debug/test-push
- variants B-F change EXACTLY one field vs the baseline
- variant F applies all four alert-style overrides at once
- unknown variant → 400
- wrong / missing token → 401
- each call appends a `trigger` event to _last_push_events, so
  hitting /api/debug/last-push-events shows the exact payload
  that was sent (only ONE field changed for B-E)
- rendered HTML card shows red badge + error string when
  EMERGENT_PUSH_KEY is placeholder (local env)
- HTML escape + nav bar with 6 links (A-F) is present
- regressions: test-push, trigger-alert, register-push, debug/devices,
  last-push-events, legacy /status still work
"""
import os
import re
import time
import uuid
from datetime import datetime, timezone

import pytest
import requests
from pymongo import MongoClient
from dotenv import load_dotenv

load_dotenv("/app/backend/.env")

BASE_URL = "http://localhost:8001"
ADMIN_PASSWORD = "REDACTED_SEE_ENV"
TOKEN = ADMIN_PASSWORD

_mongo = MongoClient(os.environ["MONGO_URL"])
_db = _mongo[os.environ["DB_NAME"]]


# ------------- helpers -------------

def _register_device(user_id: str, token: str = "abcdef1234567890", platform: str = "ios") -> None:
    """Bypass /api/register-push (which returns 500 under placeholder
    EMERGENT_PUSH_KEY because the relay 401s) and write straight to Mongo.
    We only need the row present so send_push has a recipient list."""
    _db.push_devices.update_one(
        {"user_id": user_id},
        {"$set": {
            "user_id": user_id,
            "platform": platform,
            "device_token": token,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }},
        upsert=True,
    )


def _probe(variant: str | None, token: str = TOKEN) -> requests.Response:
    params = {"token": token}
    if variant is not None:
        params["variant"] = variant
    return requests.get(f"{BASE_URL}/api/debug/probe-push", params=params, timeout=15)


def _last_events_html(token: str = TOKEN) -> str:
    r = requests.get(f"{BASE_URL}/api/debug/last-push-events", params={"token": token}, timeout=10)
    assert r.status_code == 200, r.text
    return r.text


@pytest.fixture(scope="module", autouse=True)
def _seed_one_device():
    """Make sure at least ONE device is registered so send_push is actually
    invoked (send_push returns early when recipients is []).
    """
    _register_device("TEST_probe_dev_1", token="TESTtokprobe11112222")
    yield


# ------------- helpers to introspect the rendered card -------------

def _extract_row(html: str, label: str) -> str:
    # Rows look like:  <div><b>Title:</b> <code>QuakeGuard test push</code></div>
    m = re.search(rf"<b>{re.escape(label)}:</b>\s*<code>([^<]*)</code>", html)
    assert m, f"Could not find row '{label}' in HTML"
    return m.group(1)


def _extract_badge_bg(html: str) -> str:
    m = re.search(r"background:(#[0-9a-fA-F]{3,6})}", html) or re.search(
        r"badge\{[^}]*background:(#[0-9a-fA-F]{3,6})", html
    )
    assert m, "Could not extract badge background color"
    return m.group(1).lower()


# =====================================================================
# Auth / routing
# =====================================================================

class TestAuth:
    def test_missing_token_returns_401(self):
        r = requests.get(f"{BASE_URL}/api/debug/probe-push", timeout=10)
        assert r.status_code == 401
        assert "Wrong password" in r.text

    def test_wrong_token_returns_401(self):
        r = requests.get(
            f"{BASE_URL}/api/debug/probe-push",
            params={"token": "nope", "variant": "A"},
            timeout=10,
        )
        assert r.status_code == 401
        assert "Wrong password" in r.text

    def test_unknown_variant_returns_400(self):
        r = _probe("X")
        assert r.status_code == 400
        assert "Unknown variant" in r.text
        # HTML escaped
        assert "'X'" in r.text


# =====================================================================
# Baseline & each variant renders correct fields
# =====================================================================

class TestVariantRendering:
    def test_variant_A_baseline(self):
        r = _probe("A")
        assert r.status_code == 200
        assert _extract_row(r.text, "Title") == "QuakeGuard test push"
        assert _extract_row(r.text, "Message") == "If you see this, the push channel is working."
        assert _extract_row(r.text, "action_url") == "/"
        idem = _extract_row(r.text, "idempotency_key")
        assert idem.startswith("quake-testpush-"), idem

    def test_variant_B_title_only(self):
        r = _probe("B")
        assert r.status_code == 200
        assert _extract_row(r.text, "Title") == "EARTHQUAKE ALERT"
        # Everything else = baseline
        assert _extract_row(r.text, "Message") == "If you see this, the push channel is working."
        assert _extract_row(r.text, "action_url") == "/"
        assert _extract_row(r.text, "idempotency_key").startswith("quake-testpush-")

    def test_variant_C_message_only(self):
        r = _probe("C")
        assert r.status_code == 200
        assert _extract_row(r.text, "Title") == "QuakeGuard test push"
        assert _extract_row(r.text, "Message") == "Magnitude 6.4. Are you safe? Tap to check in."
        assert _extract_row(r.text, "action_url") == "/"
        assert _extract_row(r.text, "idempotency_key").startswith("quake-testpush-")

    def test_variant_D_action_url_only(self):
        r = _probe("D")
        assert r.status_code == 200
        assert _extract_row(r.text, "Title") == "QuakeGuard test push"
        assert _extract_row(r.text, "Message") == "If you see this, the push channel is working."
        assert _extract_row(r.text, "action_url") == "/alert"
        assert _extract_row(r.text, "idempotency_key").startswith("quake-testpush-")

    def test_variant_E_idempotency_prefix_only(self):
        r = _probe("E")
        assert r.status_code == 200
        assert _extract_row(r.text, "Title") == "QuakeGuard test push"
        assert _extract_row(r.text, "Message") == "If you see this, the push channel is working."
        assert _extract_row(r.text, "action_url") == "/"
        idem = _extract_row(r.text, "idempotency_key")
        assert idem.startswith("quake-"), idem
        assert not idem.startswith("quake-testpush-"), idem

    def test_variant_F_full_alert(self):
        r = _probe("F")
        assert r.status_code == 200
        assert _extract_row(r.text, "Title") == "EARTHQUAKE ALERT"
        assert _extract_row(r.text, "Message") == "Magnitude 6.4. Are you safe? Tap to check in."
        assert _extract_row(r.text, "action_url") == "/alert"
        idem = _extract_row(r.text, "idempotency_key")
        assert idem.startswith("quake-") and not idem.startswith("quake-testpush-")

    def test_variant_case_insensitive(self):
        r = _probe("b")
        assert r.status_code == 200
        assert _extract_row(r.text, "Title") == "EARTHQUAKE ALERT"

    def test_missing_variant_defaults_to_A(self):
        r = _probe(None)
        assert r.status_code == 200
        assert _extract_row(r.text, "Title") == "QuakeGuard test push"


# =====================================================================
# HTML structure: badge color, nav bar with 6 links, error line
# =====================================================================

class TestHtmlStructure:
    def test_nav_bar_has_six_links_A_to_F(self):
        r = _probe("A")
        assert r.status_code == 200
        for v in ("A", "B", "C", "D", "E", "F"):
            assert f"variant={v}" in r.text, f"nav link for variant={v} missing"

    def test_placeholder_key_shows_red_badge_and_error(self):
        r = _probe("A")
        assert r.status_code == 200
        # With placeholder EMERGENT_PUSH_KEY the relay returns 401 → push_delivered=false
        assert "not delivered" in r.text
        assert "#c21818" in r.text.lower()
        assert "EMERGENT_PUSH_KEY missing or invalid" in r.text


# =====================================================================
# Diagnostic append: each probe adds a `trigger` event
# =====================================================================

class TestLastPushEventsAppend:
    def _first_card(self, html: str) -> str:
        """Return the raw HTML of the newest (first) event card."""
        # Cards are separated by top-level card divs. Split on the card start
        # marker and take the second element (first card body).
        marker = '<div style="border:1px solid #ddd;border-radius:8px;padding:12px;margin-bottom:10px">'
        parts = html.split(marker)
        assert len(parts) >= 2, "no event cards found in HTML"
        # Take up to the start of the next card (or the tip footer).
        first = parts[1]
        # Truncate at start of next card or the tip paragraph if present.
        for stop in (marker, '<p style="margin-top:24px'):
            idx = first.find(stop)
            if idx != -1:
                first = first[:idx]
                break
        return first

    def test_variant_B_appends_event_with_only_title_changed(self):
        _probe("B")
        time.sleep(0.3)
        html = _last_events_html()
        first_card = self._first_card(html)
        assert "EARTHQUAKE ALERT" in first_card
        m = re.search(r"<b>action_url:</b>\s*([^<]*)<", first_card)
        assert m, "action_url row missing on first card"
        assert m.group(1).strip() == "/", (
            f"action_url was {m.group(1)!r}, expected '/' — proves only ONE field changed"
        )

    def test_variant_D_appends_event_with_only_action_url_changed(self):
        _probe("D")
        time.sleep(0.3)
        html = _last_events_html()
        first_card = self._first_card(html)
        assert "QuakeGuard test push" in first_card
        m = re.search(r"<b>action_url:</b>\s*([^<]*)<", first_card)
        assert m and m.group(1).strip() == "/alert"

    def test_recipients_unfiltered(self):
        """Probe uses {} query (like test-push) — chunk_size on the resulting
        event must equal the total registered-device count. If probe were
        applying the trigger-alert `{$ne: triggeredBy}` filter the count
        would be lower. We assert equality against a snapshot count."""
        # snapshot device count
        r = requests.get(f"{BASE_URL}/api/debug/devices", timeout=10)
        assert r.status_code == 200
        expected = r.json()["device_count"]

        _probe("A")
        time.sleep(0.3)
        html = _last_events_html()
        first_card = self._first_card(html)
        m = re.search(r"<b>recipients in chunk:</b>\s*(\d+)", first_card)
        assert m, "recipients in chunk row missing"
        chunk_size = int(m.group(1))
        # send_push chunks by 100. Total across all cards should equal expected.
        # For a single chunk (<=100 devices) chunk_size == expected.
        assert chunk_size == min(expected, 100), (
            f"chunk_size={chunk_size} but expected {min(expected, 100)} unfiltered recipients"
        )


# =====================================================================
# Regressions
# =====================================================================

class TestRegressions:
    def test_test_push_get_still_works(self):
        r = requests.get(
            f"{BASE_URL}/api/debug/test-push", params={"token": TOKEN}, timeout=15
        )
        assert r.status_code == 200
        assert "QuakeGuard test push" in r.text
        # placeholder key ⇒ badge shows 'not delivered'
        assert "not delivered" in r.text

    def test_test_push_post_still_works(self):
        r = requests.post(
            f"{BASE_URL}/api/debug/test-push",
            headers={"X-Admin-Token": ADMIN_PASSWORD},
            timeout=15,
        )
        assert r.status_code == 200
        body = r.json()
        assert set(body.keys()) >= {"recipients", "push_delivered", "push_error"}
        assert isinstance(body["recipients"], int)

    def test_trigger_alert_returns_200_json(self):
        r = requests.post(
            f"{BASE_URL}/api/trigger-alert",
            headers={"X-Admin-Token": ADMIN_PASSWORD},
            json={"triggeredBy": "TEST_probe_dev_1"},
            timeout=15,
        )
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "broadcast"
        assert isinstance(body["recipients"], int)

    def test_register_push_upserts(self):
        """register-push under placeholder EMERGENT_PUSH_KEY still upserts
        into Mongo BEFORE the relay call, then surfaces 500 because the
        relay 401s. We assert the DB write happened regardless of the
        response code (contract of register-push is upsert-first)."""
        uid = f"TEST_probe_upsert_{uuid.uuid4()}"
        for _ in range(2):
            r = requests.post(
                f"{BASE_URL}/api/register-push",
                json={"user_id": uid, "platform": "ios", "device_token": "t123456789012"},
                timeout=10,
            )
            # Placeholder key -> 500; real key -> 201. Accept either.
            assert r.status_code in (201, 500), r.text
        # verify device list contains this uid exactly once (upsert worked)
        r = requests.get(f"{BASE_URL}/api/debug/devices", timeout=10)
        assert r.status_code == 200
        devs = r.json()["devices"]
        matches = [d for d in devs if d["user_id"] == uid]
        assert len(matches) == 1

    def test_debug_devices(self):
        r = requests.get(f"{BASE_URL}/api/debug/devices", timeout=10)
        assert r.status_code == 200
        body = r.json()
        assert "device_count" in body and "devices" in body
        assert body["push_key_status"] in ("placeholder", "real")

    def test_debug_last_push_events_renders(self):
        # Fire a probe first so we know there's at least one event
        _probe("A")
        time.sleep(0.3)
        r = requests.get(
            f"{BASE_URL}/api/debug/last-push-events", params={"token": TOKEN}, timeout=10
        )
        assert r.status_code == 200
        assert "first recipients:" in r.text
        assert "action_url:" in r.text

    def test_legacy_status_get_post(self):
        r = requests.post(
            f"{BASE_URL}/api/status",
            json={"client_name": "TEST_probe_client"},
            timeout=10,
        )
        assert r.status_code == 200
        assert r.json()["client_name"] == "TEST_probe_client"
        r2 = requests.get(f"{BASE_URL}/api/status", timeout=10)
        assert r2.status_code == 200
        assert isinstance(r2.json(), list)
