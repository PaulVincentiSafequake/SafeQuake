"""
Backend tests for the new event-capture diagnostics:
  - send_push now records `recipients_sample` (first 5 user_ids of the chunk)
  - send_push now records `action_url` from data payload
  - GET /api/debug/last-push-events HTML surfaces those values under
    'first recipients:' and 'action_url:' rows for kind='trigger' events

Also validates the trigger-alert filter behavior:
  - Body {triggeredBy:'dashboard'} excludes only devices whose user_id ==
    'dashboard' (rogue device row), not real devices like diag-A / diag-B.
  - The /api/debug/test-push endpoint has NO filter, so ALL devices —
    including the 'dashboard' rogue — are recipients.

Regressions (ring buffer=20, legacy /api/status, /api/debug/devices,
trigger-alert 200 JSON, send_push HTTPException(500) on upstream 401)
are covered too.
"""
import os
import re
import subprocess
import time
import uuid

import pytest
import requests

BASE_URL = os.environ.get("BACKEND_URL", "http://localhost:8001")
ADMIN_PWD = "Pt3481pt"
EVENTS_URL = f"{BASE_URL}/api/debug/last-push-events"


def _restart_backend_and_wait():
    subprocess.run(
        ["sudo", "supervisorctl", "restart", "backend"],
        check=True, capture_output=True, text=True,
    )
    deadline = time.time() + 20
    while time.time() < deadline:
        try:
            if requests.get(f"{BASE_URL}/api/", timeout=2).status_code == 200:
                return
        except Exception:
            pass
        time.sleep(0.5)
    raise RuntimeError("backend did not come back after restart")


def _mongo_coll():
    from pymongo import MongoClient
    c = MongoClient(os.environ.get("MONGO_URL", "mongodb://localhost:27017"))
    return c, c[os.environ.get("DB_NAME", "test_database")].push_devices


def _delete_device(user_id: str):
    """Best-effort cleanup via direct Mongo (no delete endpoint exists)."""
    try:
        c, coll = _mongo_coll()
        coll.delete_one({"user_id": user_id})
        c.close()
    except Exception:
        pass


def _snapshot_and_wipe_all_devices():
    """Wipe all push_devices so recipients_sample (first 5 in chunk) is
    deterministic. Return the docs so we can restore after the module runs."""
    try:
        c, coll = _mongo_coll()
        docs = list(coll.find({}))
        coll.delete_many({})
        c.close()
        return docs
    except Exception:
        return []


def _restore_devices(docs):
    try:
        c, coll = _mongo_coll()
        for d in docs:
            d.pop("_id", None)
            coll.update_one({"user_id": d["user_id"]}, {"$set": d}, upsert=True)
        c.close()
    except Exception:
        pass


def _register(user_id: str, token: str = None, platform: str = "ios"):
    return requests.post(
        f"{BASE_URL}/api/register-push",
        json={
            "user_id": user_id,
            "platform": platform,
            "device_token": token or ("tok-" + uuid.uuid4().hex[:10]),
        },
        timeout=5,
    )


def _events_html():
    r = requests.get(EVENTS_URL, params={"token": ADMIN_PWD}, timeout=5)
    assert r.status_code == 200, r.text
    return r.text


def _find_first_trigger_card(html: str, title_substr: str) -> str:
    """Return the outer div block for the newest event card whose title
    contains the given substring. Cards are separated by the top-level
    <div style="border:1px solid #ddd; opener."""
    cards = re.findall(
        r'<div style="border:1px solid #ddd;border-radius:8px[^"]*"[^>]*>(.*?)</pre>\s*</div>',
        html, flags=re.S,
    )
    for c in cards:
        if title_substr in c:
            return c
    return ""


# ---------- module-scoped fixture: two clean diag devices ----------
@pytest.fixture(scope="module", autouse=True)
def clean_and_seed():
    _restart_backend_and_wait()
    # Wipe the push_devices collection so recipients_sample (first 5 of
    # chunk) is deterministic across suite runs. Restore after.
    saved = _snapshot_and_wipe_all_devices()
    _register("diag-A", "tokA")
    _register("diag-B", "tokB")
    yield
    for uid in ("diag-A", "diag-B", "dashboard"):
        _delete_device(uid)
    _restore_devices(saved)


# =====================================================================
# Spec (a)–(f): recipients_sample + action_url captured & rendered
# =====================================================================
class TestRecipientsSampleAndActionUrl:
    def test_devices_registered(self):
        r = requests.get(f"{BASE_URL}/api/debug/devices")
        assert r.status_code == 200
        ids = [d["user_id"] for d in r.json()["devices"]]
        assert "diag-A" in ids and "diag-B" in ids

    def test_get_test_push_captures_action_url_root(self):
        # (b) trigger a test push
        r = requests.get(
            f"{BASE_URL}/api/debug/test-push", params={"token": ADMIN_PWD}, timeout=15
        )
        assert r.status_code == 200

        html = _events_html()
        # Event card exists for test-push (title = 'QuakeGuard test push')
        card = _find_first_trigger_card(html, "QuakeGuard test push")
        assert card, "no event card found for test-push"
        # (d) all four labels present
        assert "first recipients:" in card
        assert "action_url:" in card
        assert "recipients in chunk:" in card
        # action_url for test-push is '/'
        assert re.search(r"action_url:</b>\s*/", card), \
            f"action_url row must contain '/' for test-push. Card:\n{card[:800]}"
        # (e) recipients_sample contains diag-A and diag-B, in <code>
        code_blocks = re.findall(r"<code[^>]*>(.*?)</code>", card)
        joined = " | ".join(code_blocks)
        assert "diag-A" in joined and "diag-B" in joined, \
            f"expected diag-A & diag-B inside <code>, got: {joined!r}"

    def test_trigger_alert_captures_action_url_alert_and_filters(self):
        # (c) trigger-alert with triggeredBy='dashboard'
        r = requests.post(
            f"{BASE_URL}/api/trigger-alert",
            headers={"X-Admin-Token": ADMIN_PWD, "Content-Type": "application/json"},
            json={"triggeredBy": "dashboard"},
            timeout=15,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        # No device with user_id == 'dashboard' yet, so filter changes nothing;
        # both diag-A and diag-B are recipients.
        assert body["recipients"] >= 2

        html = _events_html()
        card = _find_first_trigger_card(html, "EARTHQUAKE ALERT")
        assert card, "no event card found for trigger-alert"
        assert "first recipients:" in card
        assert "action_url:" in card
        assert "recipients in chunk:" in card
        # action_url for trigger-alert is '/alert'
        assert re.search(r"action_url:</b>\s*/alert", card), \
            f"action_url row must contain '/alert'. Card:\n{card[:800]}"
        # (f) recipients_sample contains diag-A and diag-B
        code_blocks = re.findall(r"<code[^>]*>(.*?)</code>", card)
        joined = " | ".join(code_blocks)
        assert "diag-A" in joined and "diag-B" in joined, \
            f"expected diag-A & diag-B for triggeredBy=dashboard when no " \
            f"'dashboard' device exists; got: {joined!r}"

    def test_rogue_dashboard_device_is_filtered_out(self):
        # (g) register rogue device with user_id='dashboard'
        _register("dashboard", "ROGUE")

        # trigger-alert again → recipients must NOT include 'dashboard'
        r = requests.post(
            f"{BASE_URL}/api/trigger-alert",
            headers={"X-Admin-Token": ADMIN_PWD, "Content-Type": "application/json"},
            json={"triggeredBy": "dashboard"},
            timeout=15,
        )
        assert r.status_code == 200, r.text

        html = _events_html()
        card = _find_first_trigger_card(html, "EARTHQUAKE ALERT")
        assert card, "no fresh trigger-alert card"
        code_blocks = re.findall(r"<code[^>]*>(.*?)</code>", card)
        joined = " | ".join(code_blocks)
        assert "dashboard" not in joined, \
            f"filter failed: 'dashboard' should be excluded from trigger-alert " \
            f"recipients_sample; got: {joined!r}"
        assert "diag-A" in joined and "diag-B" in joined, \
            f"real devices missing after filter; got: {joined!r}"

        # And test-push (no filter) MUST still include 'dashboard'
        r2 = requests.get(
            f"{BASE_URL}/api/debug/test-push", params={"token": ADMIN_PWD}, timeout=15
        )
        assert r2.status_code == 200
        html2 = _events_html()
        card2 = _find_first_trigger_card(html2, "QuakeGuard test push")
        assert card2
        cb2 = re.findall(r"<code[^>]*>(.*?)</code>", card2)
        joined2 = " | ".join(cb2)
        assert "dashboard" in joined2, \
            f"test-push has no filter and should include 'dashboard'; got: {joined2!r}"
        assert "diag-A" in joined2 and "diag-B" in joined2


# =====================================================================
#  Regression coverage
# =====================================================================
class TestRegression:
    def test_legacy_status_get_post(self):
        name = f"TEST_diag_{uuid.uuid4().hex[:6]}"
        r = requests.post(f"{BASE_URL}/api/status", json={"client_name": name})
        assert r.status_code == 200
        assert r.json()["client_name"] == name
        r2 = requests.get(f"{BASE_URL}/api/status")
        assert r2.status_code == 200
        assert any(s["client_name"] == name for s in r2.json())

    def test_debug_devices_lists_diag_and_dashboard(self):
        r = requests.get(f"{BASE_URL}/api/debug/devices")
        assert r.status_code == 200
        ids = [d["user_id"] for d in r.json()["devices"]]
        # Depending on ordering with the rogue-device test above, all three
        # should exist by end-of-suite. At minimum diag-A and diag-B.
        assert "diag-A" in ids and "diag-B" in ids

    def test_trigger_alert_200_contract(self):
        r = requests.post(
            f"{BASE_URL}/api/trigger-alert",
            headers={"X-Admin-Token": ADMIN_PWD, "Content-Type": "application/json"},
            json={"magnitude": 6.1},
        )
        assert r.status_code == 200, r.text
        b = r.json()
        assert b["status"] == "broadcast"
        assert isinstance(b["recipients"], int)
        # placeholder key ⇒ push_delivered False, but 200
        assert b["push_delivered"] is False
        assert b["push_error"] == "EMERGENT_PUSH_KEY missing or invalid"

    def test_send_push_raises_500_on_upstream_401(self):
        """POST /api/debug/test-push exercises send_push directly. With the
        placeholder key, the upstream returns 401, which send_push maps to
        HTTPException(500). The endpoint catches that and returns 200 JSON
        with push_delivered=false and push_error containing the mapped msg.
        This preserves the pre-existing contract."""
        r = requests.post(
            f"{BASE_URL}/api/debug/test-push",
            headers={"X-Admin-Token": ADMIN_PWD},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["push_delivered"] is False
        assert body["push_error"] == "EMERGENT_PUSH_KEY missing or invalid"

    def test_ring_buffer_bounded_at_20(self):
        # trigger a bunch of pushes and confirm the page never shows >20 cards
        for _ in range(25):
            requests.get(
                f"{BASE_URL}/api/debug/test-push",
                params={"token": ADMIN_PWD},
                timeout=10,
            )
        html = _events_html()
        cards = len(re.findall(r'<pre style="background:#f4f4f6', html))
        assert cards <= 20, f"ring buffer overflowed: {cards} cards"
        # subtitle reflects the count
        m = re.search(r"Last\s+(\d+)\s+interaction", html)
        assert m and int(m.group(1)) <= 20
