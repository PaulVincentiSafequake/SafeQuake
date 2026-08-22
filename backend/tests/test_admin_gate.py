"""
Backend tests for the X-Admin-Token password gate on POST /api/trigger-alert.
Also verifies:
  - CORS preflight from safequake.onrender.com origin.
  - Legacy /api/status GET+POST unchanged.
  - /api/register-push still not gated (writes db.push_devices).
Runs against the LOCAL backend (curl-parity), i.e. http://localhost:8001.
"""
import os
import uuid
import pytest
import requests
from dotenv import load_dotenv

load_dotenv("/app/backend/.env")

# Per problem statement: curl against local backend.
BASE_URL = "http://localhost:8001"
ADMIN_PWD = os.environ["ADMIN_TRIGGER_PASSWORD"]

from server import TRIGGER_ALERT_CONFIRMATION  # noqa: E402


@pytest.fixture(scope="module")
def s():
    session = requests.Session()
    session.headers.update({"Content-Type": "application/json"})
    return session


# ---------- Health ----------
class TestHealth:
    def test_api_root_ok(self, s):
        r = s.get(f"{BASE_URL}/api/")
        assert r.status_code == 200
        assert r.json().get("message") == "Hello World"


# ---------- /api/trigger-alert password gate ----------
class TestTriggerAlertGate:
    def test_missing_token_returns_401(self, s):
        r = requests.post(
            f"{BASE_URL}/api/trigger-alert",
            json={"triggeredBy": "dashboard"},
            headers={"Content-Type": "application/json"},
        )
        assert r.status_code == 401, r.text
        # Message wording changed with the JWT cutover (task #9): an
        # unauthenticated call is now "Not authenticated". Either wording is
        # acceptable; the 401 is the contract.
        assert r.json().get("detail") in (
            "Invalid or missing X-Admin-Token", "Not authenticated",
        )

    def test_wrong_token_returns_401(self, s):
        r = requests.post(
            f"{BASE_URL}/api/trigger-alert",
            json={"triggeredBy": "dashboard"},
            headers={"Content-Type": "application/json", "X-Admin-Token": "wrong"},
        )
        assert r.status_code == 401, r.text
        # Message wording changed with the JWT cutover (task #9): an
        # unauthenticated call is now "Not authenticated". Either wording is
        # acceptable; the 401 is the contract.
        assert r.json().get("detail") in (
            "Invalid or missing X-Admin-Token", "Not authenticated",
        )

    def test_correct_token_returns_200_broadcast(self, s, stand_down_after):
        r = requests.post(
            f"{BASE_URL}/api/trigger-alert",
            json={"triggeredBy": "dashboard",
                  "confirmation_phrase": TRIGGER_ALERT_CONFIRMATION},
            headers={"Content-Type": "application/json", "X-Admin-Token": ADMIN_PWD},
        )
        assert r.status_code == 200, r.text
        data = r.json()
        assert data.get("status") == "broadcast"
        assert isinstance(data.get("recipients"), int)
        # Push should NOT be delivered locally (placeholder key).
        assert data.get("push_delivered") is False


# ---------- CORS preflight from safequake origin ----------
class TestCorsPreflight:
    def test_preflight_trigger_alert(self, s):
        r = requests.options(
            f"{BASE_URL}/api/trigger-alert",
            headers={
                "Origin": "https://safequake.onrender.com",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type,x-admin-token",
            },
        )
        assert r.status_code in (200, 204), f"got {r.status_code}: {r.text}"
        allow_origin = r.headers.get("access-control-allow-origin", "")
        # Either wildcard or echo of the requesting origin
        assert allow_origin in ("*", "https://safequake.onrender.com"), r.headers
        allow_headers = r.headers.get("access-control-allow-headers", "").lower()
        # Must permit x-admin-token (echoed or wildcard)
        assert ("x-admin-token" in allow_headers) or ("*" in allow_headers), r.headers


# ---------- /api/register-push unchanged (no admin gate) ----------
class TestRegisterPushNotGated:
    def test_register_push_is_not_behind_the_admin_token(self, s, clear_register_rate_limit):
        user_id = f"TEST_{uuid.uuid4()}"
        body = {"user_id": user_id, "platform": "android",
                "device_token": "TEST_" + "t" * 40}
        r = requests.post(
            f"{BASE_URL}/api/register-push",
            json=body,
            headers={"Content-Type": "application/json"},
        )
        # A phone must be able to register before anyone has signed in
        # anywhere, so this endpoint is never gated by X-Admin-Token.
        # #266: with a placeholder EMERGENT_PUSH_KEY the relay refuses the
        # registration and the endpoint answers 502 WITHOUT filing a row —
        # no false promise that the phone is reachable.
        assert r.status_code != 401, "register-push must NOT require X-Admin-Token"
        assert r.status_code in (201, 502), r.text


# ---------- /api/status endpoints ungated ----------
class TestStatusNotGated:
    device_id = "TEST_admin_gate_status"

    def test_status_post_no_gate(self, s):
        r = s.post(f"{BASE_URL}/api/status",
                   json={"deviceId": self.device_id, "status": "safe"})
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["status"] == "ok"
        assert d["device_id"] == self.device_id
        assert "updated_at" in d

    def test_status_get_no_gate(self, s):
        r = s.get(f"{BASE_URL}/api/status")
        assert r.status_code == 200
        assert isinstance(r.json(), list)


# ---------- HTML snippet sanity ----------
class TestDashboardSnippet:
    SNIPPET = "/app/memory/dashboard-trigger-button.html"

    def _read(self):
        with open(self.SNIPPET, encoding="utf-8") as f:
            return f.read()

    def test_snippet_has_required_elements(self):
        html = self._read()
        assert 'id="qg-trigger-btn"' in html
        assert "window.prompt(" in html
        assert 'X-Admin-Token' in html
        assert "QUAKEGUARD_BACKEND" in html
        # 401 → wrong password user-visible message
        assert "res.status === 401" in html
        assert "Wrong password" in html
        # No <input> field for the password (uses window.prompt)
        assert "<input" not in html.lower()

    def test_snippet_js_syntax_balanced(self):
        html = self._read()
        # Extract JS between the <script> tag and its closing tag
        start = html.find("<script>")
        end = html.find("</script>", start)
        assert start != -1 and end != -1, "script block missing"
        js = html[start + len("<script>"):end]
        # Balanced braces/parens/brackets
        for open_c, close_c in [("{", "}"), ("(", ")"), ("[", "]")]:
            assert js.count(open_c) == js.count(close_c), (
                f"unbalanced {open_c}{close_c}: {js.count(open_c)} vs {js.count(close_c)}"
            )
        # No unterminated single-line strings (quick heuristic: odd count of
        # unescaped double quotes on any single line)
        for i, line in enumerate(js.splitlines(), 1):
            # strip trivial escapes so we don't over-count
            stripped = line.replace('\\"', "")
            # ignore lines that are pure comments
            if stripped.strip().startswith("//"):
                continue
            assert stripped.count('"') % 2 == 0, f"unterminated \" on JS line {i}: {line!r}"
