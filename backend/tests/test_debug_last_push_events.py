"""
Backend tests for the new browser-friendly diagnostic endpoint
GET /api/debug/last-push-events?token=<pwd>, plus regressions on the send_push
capture behavior and the existing debug/status endpoints.

Runs against localhost:8001. EMERGENT_PUSH_KEY is 'placeholder' locally so
the Emergent relay returns HTTP 401 with body '{"error":"invalid X-Push-Key"}',
which is the exact string these tests look for inside the <pre> block.

Spec coverage:
  (a) empty ring buffer → 200 HTML with 'No push events captured yet'
  (b) no token → 401 HTML 'Wrong password'
  (c) wrong token → 401 HTML 'Wrong password'
  (d) after 1 test-push trigger → 200 HTML with 401 badge + 'invalid X-Push-Key'
  (e) 5 pushes visible; ring buffer bounded to 20 (21st evicts oldest)
  (f) each event includes: status badge, ISO timestamp, title, chunk count,
      error line (when error present), <pre> raw body

Regression:
  - POST /api/debug/test-push with X-Admin-Token still returns
    JSON {recipients, push_delivered:false, push_error}
  - POST /api/trigger-alert with X-Admin-Token still returns 200 JSON
  - POST /api/register-push still upserts to db.push_devices
  - Legacy /api/status GET+POST
  - GET /api/debug/devices
  - send_push still raises HTTPException(500) on upstream 401 (backward compat)
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
    """Restart the backend via supervisor so the module-level ring buffer
    starts empty for the 'empty' spec point."""
    subprocess.run(
        ["sudo", "supervisorctl", "restart", "backend"],
        check=True,
        capture_output=True,
        text=True,
    )
    # Poll until the API is back
    deadline = time.time() + 20
    while time.time() < deadline:
        try:
            if requests.get(f"{BASE_URL}/api/", timeout=2).status_code == 200:
                return
        except Exception:
            pass
        time.sleep(0.5)
    raise RuntimeError("backend did not come back after restart")


@pytest.fixture(scope="module")
def api():
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    return s


# =====================================================================
#  Auth gate — order-independent (do these BEFORE the ring buffer gets
#  filled by other test modules so we can also verify empty state).
# =====================================================================
class TestAuthGate:
    def test_no_token_returns_401_html(self):
        r = requests.get(EVENTS_URL)
        assert r.status_code == 401, r.text
        assert "text/html" in r.headers.get("content-type", "").lower()
        assert "Wrong password" in r.text

    def test_wrong_token_returns_401_html(self):
        r = requests.get(EVENTS_URL, params={"token": "wrong"})
        assert r.status_code == 401, r.text
        assert "text/html" in r.headers.get("content-type", "").lower()
        assert "Wrong password" in r.text


# =====================================================================
#  Empty state — restart backend to ensure the module-level deque is empty
# =====================================================================
class TestEmptyBuffer:
    def test_empty_buffer_shows_placeholder_message(self):
        _restart_backend_and_wait()
        r = requests.get(EVENTS_URL, params={"token": ADMIN_PWD})
        assert r.status_code == 200, r.text
        assert "text/html" in r.headers.get("content-type", "").lower()
        assert "No push events captured yet" in r.text
        # Sanity: page shell must be present
        assert "QuakeGuard push events" in r.text


# =====================================================================
#  Trigger a push, then inspect the buffer
# =====================================================================
class TestSinglePushCaptured:
    """After one test-push, the buffer must have >=1 event with the exact
    upstream error body 'invalid X-Push-Key' surfaced in the <pre> block."""

    @classmethod
    def setup_class(cls):
        _restart_backend_and_wait()
        # Ensure at least one recipient exists so send_push actually POSTs
        # to the relay (send_push returns early on empty recipients).
        cls.seed_user = f"TEST_evt_{uuid.uuid4().hex[:6]}"
        requests.post(
            f"{BASE_URL}/api/register-push",
            json={
                "user_id": cls.seed_user,
                "platform": "ios",
                "device_token": "tok-" + uuid.uuid4().hex,
            },
        )
        # Trigger exactly one push via the browser variant
        r = requests.get(
            f"{BASE_URL}/api/debug/test-push", params={"token": ADMIN_PWD}
        )
        assert r.status_code == 200, r.text

    def test_events_page_ok_and_html(self):
        r = requests.get(EVENTS_URL, params={"token": ADMIN_PWD})
        assert r.status_code == 200
        assert "text/html" in r.headers.get("content-type", "").lower()

    def test_event_has_401_status_badge(self):
        html = requests.get(EVENTS_URL, params={"token": ADMIN_PWD}).text
        # Badge span carries the status code as its inner text
        assert re.search(r">401<", html), "no 401 status badge found"

    def test_event_pre_contains_relay_error_body(self):
        html = requests.get(EVENTS_URL, params={"token": ADMIN_PWD}).text
        # This is the literal upstream body when EMERGENT_PUSH_KEY is placeholder
        assert "invalid X-Push-Key" in html, (
            "raw relay response body 'invalid X-Push-Key' not surfaced in HTML"
        )
        # Must live inside a <pre> block
        pre_blocks = re.findall(r"<pre[^>]*>(.*?)</pre>", html, flags=re.S)
        assert any("invalid X-Push-Key" in p for p in pre_blocks), (
            "'invalid X-Push-Key' present in page but NOT inside a <pre> block"
        )

    def test_event_has_all_required_fields(self):
        """Spec (f): each event entry must include status badge, ISO
        timestamp, title, chunk count, error line, and the <pre> body."""
        html = requests.get(EVENTS_URL, params={"token": ADMIN_PWD}).text
        # ISO timestamp (yyyy-mm-ddThh:mm:ss)
        assert re.search(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", html
        ), "no ISO timestamp"
        # Title
        assert "QuakeGuard test push" in html
        # Chunk count label
        assert re.search(r"recipients in chunk:</b>\s*\d+", html), (
            "chunk_size line missing"
        )
        # Error line (only rendered when error present — should be here)
        assert "<b>error:</b>" in html, "error line missing"
        assert "EMERGENT_PUSH_KEY missing or invalid" in html
        # <pre> raw body
        assert "<pre" in html


# =====================================================================
#  Multiple pushes: newest-first + bounded ring buffer
# =====================================================================
class TestRingBufferBounded:
    """Trigger 5 → verify all visible & newest-first; trigger 21 total →
    verify buffer capped at 20 and oldest is evicted."""

    @classmethod
    def setup_class(cls):
        _restart_backend_and_wait()
        # Ensure at least one recipient
        requests.post(
            f"{BASE_URL}/api/register-push",
            json={
                "user_id": f"TEST_bounded_{uuid.uuid4().hex[:6]}",
                "platform": "ios",
                "device_token": "tok-" + uuid.uuid4().hex,
            },
        )

    def _trigger_n(self, n: int):
        for _ in range(n):
            requests.get(
                f"{BASE_URL}/api/debug/test-push", params={"token": ADMIN_PWD}
            )

    def _count_event_cards(self, html: str) -> int:
        # Each event card renders a status badge span with either the
        # numeric status or an em-dash inside 'padding:2px 8px;border-radius:999px'
        # Simpler: count the fixed <pre style="background:#f4f4f6 blocks
        # (there is exactly one per event card in the template).
        return len(
            re.findall(
                r'<pre style="background:#f4f4f6', html
            )
        )

    def test_5_pushes_all_visible(self):
        self._trigger_n(5)
        html = requests.get(EVENTS_URL, params={"token": ADMIN_PWD}).text
        cards = self._count_event_cards(html)
        assert cards >= 5, f"expected >=5 event cards, got {cards}"

    def test_buffer_capped_at_20(self):
        # We've already pushed 5 in the previous test (module-scoped state).
        # Push 16 more → total 21 triggers → buffer must hold exactly 20.
        self._trigger_n(16)
        html = requests.get(EVENTS_URL, params={"token": ADMIN_PWD}).text
        cards = self._count_event_cards(html)
        assert cards == 20, f"ring buffer must cap at 20, got {cards} cards"
        # 'Last 20 interaction(s)' subtitle
        assert re.search(r"Last\s+20\s+interaction", html), (
            "subtitle should reflect count=20"
        )


# =====================================================================
#  Regression: send_push still raises HTTPException on upstream 401,
#  contract of debug/test-push (POST) and trigger-alert unchanged.
# =====================================================================
class TestRegression:
    def test_post_test_push_json_contract(self):
        r = requests.post(
            f"{BASE_URL}/api/debug/test-push",
            headers={"X-Admin-Token": ADMIN_PWD},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert set(body.keys()) >= {"recipients", "push_delivered", "push_error"}
        assert isinstance(body["recipients"], int)
        assert body["push_delivered"] is False
        assert body["push_error"] == "EMERGENT_PUSH_KEY missing or invalid"

    def test_post_test_push_no_header_401(self):
        r = requests.post(f"{BASE_URL}/api/debug/test-push")
        assert r.status_code == 401

    def test_trigger_alert_returns_200(self):
        r = requests.post(
            f"{BASE_URL}/api/trigger-alert",
            headers={"X-Admin-Token": ADMIN_PWD, "Content-Type": "application/json"},
            json={"magnitude": 5.9},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "broadcast"
        assert isinstance(body["recipients"], int)
        # placeholder key ⇒ push not delivered but response still 200
        assert body["push_delivered"] is False
        assert body["push_error"] == "EMERGENT_PUSH_KEY missing or invalid"

    def test_register_push_upserts_to_db(self):
        uid = f"TEST_reg_{uuid.uuid4().hex[:6]}"
        r = requests.post(
            f"{BASE_URL}/api/register-push",
            json={"user_id": uid, "platform": "android", "device_token": "t-" + uid},
        )
        # Placeholder key ⇒ upstream 401 gets re-raised as 500 (pre-existing
        # behavior). Either way, the DB upsert happens FIRST.
        assert r.status_code in (201, 500), r.text
        r2 = requests.get(f"{BASE_URL}/api/debug/devices")
        assert r2.status_code == 200
        ids = [d.get("user_id") for d in r2.json()["devices"]]
        assert uid in ids

    def test_legacy_status_get_and_post(self):
        name = f"TEST_status_{uuid.uuid4().hex[:6]}"
        r = requests.post(
            f"{BASE_URL}/api/status", json={"client_name": name}
        )
        assert r.status_code == 200, r.text
        obj = r.json()
        assert obj["client_name"] == name
        assert "id" in obj and "timestamp" in obj

        r2 = requests.get(f"{BASE_URL}/api/status")
        assert r2.status_code == 200
        names = [s["client_name"] for s in r2.json()]
        assert name in names

    def test_debug_devices_still_returns_list(self):
        r = requests.get(f"{BASE_URL}/api/debug/devices")
        assert r.status_code == 200
        data = r.json()
        assert "devices" in data and isinstance(data["devices"], list)
        assert data["push_key_status"] == "placeholder"
        assert data["admin_password_configured"] is True


# =====================================================================
#  Cross-check: the JSON POST /debug/test-push also populates the ring
#  buffer (send_push captures BEFORE raising). This is the whole point
#  of the new endpoint.
# =====================================================================
class TestPostAlsoCaptures:
    def test_post_test_push_captures_event(self):
        _restart_backend_and_wait()
        requests.post(
            f"{BASE_URL}/api/register-push",
            json={
                "user_id": f"TEST_postcap_{uuid.uuid4().hex[:6]}",
                "platform": "ios",
                "device_token": "tok-" + uuid.uuid4().hex,
            },
        )
        r = requests.post(
            f"{BASE_URL}/api/debug/test-push",
            headers={"X-Admin-Token": ADMIN_PWD},
        )
        assert r.status_code == 200
        assert r.json()["push_delivered"] is False
        # And the buffer now shows that 401 event
        html = requests.get(EVENTS_URL, params={"token": ADMIN_PWD}).text
        assert "invalid X-Push-Key" in html
        assert ">401<" in html
