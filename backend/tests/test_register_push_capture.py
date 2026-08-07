"""
Backend tests for the two new behaviors in /app/backend/server.py:

  (1) POST /api/register-push now captures the Emergent relay's upstream
      response (status_code + response body) into the module-level
      _last_push_events deque with kind='register'. The DB upsert must
      still happen even when the relay returns 401 (and the endpoint
      re-raises HTTPException(500) in that case).

  (2) GET /api/debug/last-push-events HTML page now shows a "kind" badge
      (register|trigger) on each event card, and switches the detail line
      accordingly:
         - kind=register → "user_id: … · platform: …"
         - kind=trigger  → "title: … · recipients in chunk: …"

Runs against http://localhost:8001. EMERGENT_PUSH_KEY is 'placeholder'
locally so upstream returns HTTP 401 with body containing 'invalid X-Push-Key'.

Also covers the full regression matrix in the review request.
"""
import os
import re
import subprocess
import time
import uuid

import pytest
import requests

BASE_URL = os.environ.get("BACKEND_URL", "http://localhost:8001")
ADMIN_PWD = "REDACTED_SEE_ENV"
EVENTS_URL = f"{BASE_URL}/api/debug/last-push-events"
DEVICES_URL = f"{BASE_URL}/api/debug/devices"
REG_URL = f"{BASE_URL}/api/register-push"


# --- utilities ---------------------------------------------------------

def _restart_backend_and_wait():
    """Restart backend via supervisor so the module-level deque starts empty."""
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


@pytest.fixture(scope="module")
def api():
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    return s


# =====================================================================
#  (a) POST /api/register-push → 500 upstream-401, DB upsert still happens
#  (b) events page shows one register event, status=401, invalid X-Push-Key
#  (c) second register-push adds a second register event
#  (d) test-push adds a trigger event; newest first
# =====================================================================
class TestRegisterPushCapturesUpstream:

    @classmethod
    def setup_class(cls):
        _restart_backend_and_wait()
        cls.uid_a = "diag-A"
        cls.uid_b = "diag-B"

    def test_a_register_push_500_and_db_upsert(self):
        r = requests.post(
            REG_URL,
            json={
                "user_id": self.uid_a,
                "platform": "ios",
                "device_token": "AABBCC112233",
            },
        )
        assert r.status_code == 500, r.text
        assert "EMERGENT_PUSH_KEY missing or invalid" in r.text

        # DB upsert must have happened BEFORE the 500
        r2 = requests.get(DEVICES_URL)
        assert r2.status_code == 200
        ids = [d["user_id"] for d in r2.json()["devices"]]
        assert self.uid_a in ids, (
            f"{self.uid_a} not in db.push_devices — upsert did not happen"
        )

    def test_b_register_event_visible_in_ring_buffer(self):
        html = requests.get(EVENTS_URL, params={"token": ADMIN_PWD}).text
        # status 401 badge
        assert re.search(r">401<", html), "no 401 status badge in HTML"
        # kind=register badge (rendered as inner text of a dark badge span)
        assert ">register<" in html, "no 'register' kind badge in HTML"
        # detail line for register kind
        assert f">{self.uid_a}<" in html or f"{self.uid_a} " in html, (
            "user_id=diag-A not surfaced in register detail line"
        )
        assert re.search(
            r"user_id:</b>\s*diag-A", html
        ), "register detail line missing user_id:diag-A"
        assert re.search(
            r"platform:</b>\s*ios", html
        ), "register detail line missing platform:ios"
        # <pre> body must contain upstream error text
        pre_blocks = re.findall(r"<pre[^>]*>(.*?)</pre>", html, flags=re.S)
        assert any("invalid X-Push-Key" in p for p in pre_blocks), (
            "'invalid X-Push-Key' not surfaced inside a <pre> block"
        )

    def test_c_second_register_push_adds_second_event(self):
        r = requests.post(
            REG_URL,
            json={
                "user_id": self.uid_b,
                "platform": "android",
                "device_token": "XYZ",
            },
        )
        assert r.status_code == 500, r.text

        # DB has both
        r2 = requests.get(DEVICES_URL)
        ids = [d["user_id"] for d in r2.json()["devices"]]
        assert self.uid_b in ids
        assert self.uid_a in ids

        html = requests.get(EVENTS_URL, params={"token": ADMIN_PWD}).text
        # Two register kind badges
        assert html.count(">register<") >= 2, (
            f"expected >=2 register kind badges, got {html.count('>register<')}"
        )
        # Both user_ids surface in a register detail line
        assert re.search(r"user_id:</b>\s*diag-A", html)
        assert re.search(r"user_id:</b>\s*diag-B", html)
        assert re.search(r"platform:</b>\s*android", html)

    def test_d_test_push_adds_trigger_event_and_newest_first(self):
        r = requests.get(
            f"{BASE_URL}/api/debug/test-push", params={"token": ADMIN_PWD}
        )
        assert r.status_code == 200
        html = requests.get(EVENTS_URL, params={"token": ADMIN_PWD}).text

        # Must have BOTH kinds
        assert ">trigger<" in html, "no trigger kind badge after test-push"
        assert ">register<" in html, "register events disappeared"
        # Trigger event detail line
        assert re.search(
            r"title:</b>\s*QuakeGuard test push", html
        ), "trigger detail line missing 'QuakeGuard test push'"
        assert re.search(
            r"recipients in chunk:</b>\s*\d+", html
        ), "trigger detail line missing chunk_size"

        # Newest-first: the FIRST <pre> block (topmost card) should
        # correspond to the just-fired trigger, i.e. the first kind badge
        # occurrence in the HTML should be 'trigger', not 'register'.
        # Find the position of first ">trigger<" vs first ">register<".
        pos_trigger = html.find(">trigger<")
        pos_register = html.find(">register<")
        assert pos_trigger < pos_register, (
            "newest-first invariant broken: register appears before trigger "
            f"(pos_trigger={pos_trigger}, pos_register={pos_register})"
        )

    def test_e_register_pre_block_exact_upstream_body(self):
        """The <pre> for register events must literally contain the exact
        upstream body 'invalid X-Push-Key'."""
        html = requests.get(EVENTS_URL, params={"token": ADMIN_PWD}).text
        # Look for a <pre> that contains both 'invalid X-Push-Key' AND
        # sits inside a card that also carries the 'register' kind badge.
        # Split HTML by the per-card wrapper 'border:1px solid #ddd;border-radius:8px'
        cards = re.split(r'<div style="border:1px solid #ddd', html)
        register_cards = [c for c in cards if ">register<" in c]
        assert register_cards, "no register-kind card in HTML"
        assert any("invalid X-Push-Key" in c for c in register_cards), (
            "register cards missing 'invalid X-Push-Key' in <pre>"
        )


# =====================================================================
#  Regression matrix from the review request
# =====================================================================
class TestRegression:
    def test_trigger_alert_200_json(self):
        r = requests.post(
            f"{BASE_URL}/api/trigger-alert",
            headers={"X-Admin-Token": ADMIN_PWD, "Content-Type": "application/json"},
            json={"magnitude": 5.9},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "broadcast"
        assert isinstance(body["recipients"], int)
        assert body["push_delivered"] is False
        assert body["push_error"] == "EMERGENT_PUSH_KEY missing or invalid"

    def test_debug_devices_still_returns_list(self):
        r = requests.get(DEVICES_URL)
        assert r.status_code == 200
        data = r.json()
        assert "devices" in data and isinstance(data["devices"], list)
        assert data["push_key_status"] == "placeholder"
        assert data["admin_password_configured"] is True

    def test_debug_test_push_browser_html_200(self):
        r = requests.get(
            f"{BASE_URL}/api/debug/test-push", params={"token": ADMIN_PWD}
        )
        assert r.status_code == 200
        assert "text/html" in r.headers.get("content-type", "").lower()
        # badge + Recipients row
        assert "QuakeGuard test push" in r.text
        assert "Recipients:" in r.text
        assert ("not delivered" in r.text) or ("delivered" in r.text)

    def test_post_debug_test_push_json_contract(self):
        r = requests.post(
            f"{BASE_URL}/api/debug/test-push",
            headers={"X-Admin-Token": ADMIN_PWD},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert set(body.keys()) >= {"recipients", "push_delivered", "push_error"}
        assert body["push_delivered"] is False
        assert body["push_error"] == "EMERGENT_PUSH_KEY missing or invalid"

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

    def test_send_push_contract_500_on_401(self):
        """send_push must raise HTTPException(500) on upstream 401. Verify
        transitively via /api/trigger-alert push_error field.
        (Direct import would be more invasive.)"""
        r = requests.post(
            f"{BASE_URL}/api/trigger-alert",
            headers={"X-Admin-Token": ADMIN_PWD, "Content-Type": "application/json"},
            json={},
        )
        assert r.status_code == 200
        # push_error is the HTTPException.detail from send_push
        assert r.json()["push_error"] == "EMERGENT_PUSH_KEY missing or invalid"


# =====================================================================
#  RingBuffer bounded at maxlen=20 even with mix of register + trigger
# =====================================================================
class TestRingBufferBoundedMixed:
    @classmethod
    def setup_class(cls):
        _restart_backend_and_wait()

    def _count_event_cards(self, html: str) -> int:
        return len(
            re.findall(r'<pre style="background:#f4f4f6', html)
        )

    def test_mix_over_20_bounded(self):
        # 11 register events (each returns 500 upstream-401)
        for i in range(11):
            uid = f"TEST_ringmix_reg_{i}_{uuid.uuid4().hex[:4]}"
            requests.post(
                REG_URL,
                json={
                    "user_id": uid,
                    "platform": "ios",
                    "device_token": "tok-" + uid,
                },
            )
        # 11 trigger events
        for _ in range(11):
            requests.get(
                f"{BASE_URL}/api/debug/test-push", params={"token": ADMIN_PWD}
            )

        html = requests.get(EVENTS_URL, params={"token": ADMIN_PWD}).text
        cards = self._count_event_cards(html)
        assert cards == 20, f"ring buffer must cap at 20, got {cards}"
        # subtitle reflects count=20
        assert re.search(r"Last\s+20\s+interaction", html)
        # Both kinds still present in the last 20
        assert ">register<" in html
        assert ">trigger<" in html
