"""
Backend tests for the new GET /api/debug/test-push?token=<pwd> browser-friendly
variant. Adds coverage for:
  a) GET with no token           → 401 HTML "Wrong password"
  b) GET with wrong token        → 401 HTML "Wrong password"
  c) GET with correct token      → 200 HTML with headline, badge, recipients,
                                    push_delivered, and (placeholder key)
                                    error line.
  d) POST /api/debug/test-push with X-Admin-Token still returns JSON (bc compat)
  e) POST /api/debug/test-push with no header still 401 JSON (bc compat)
  f) server.py factored the shared logic into async def _run_test_push and
     both endpoints await it.

Runs against http://localhost:8001. EMERGENT_PUSH_KEY is 'placeholder' locally
so push_delivered MUST be False and push_error must be
'EMERGENT_PUSH_KEY missing or invalid'.
"""
import re
from pathlib import Path

import pytest
import requests

BASE_URL = "http://localhost:8001"
ADMIN_PWD = "Pt3481pt"
SERVER_PY = Path("/app/backend/server.py")


# ---------- (a) GET, no token ----------
class TestGetNoToken:
    def test_missing_token_returns_401_html_wrong_password(self):
        r = requests.get(f"{BASE_URL}/api/debug/test-push")
        assert r.status_code == 401, r.text
        ct = r.headers.get("content-type", "")
        assert "text/html" in ct.lower(), f"unexpected content-type: {ct!r}"
        assert "Wrong password" in r.text, r.text


# ---------- (b) GET, wrong token ----------
class TestGetWrongToken:
    def test_wrong_token_returns_401_html_wrong_password(self):
        r = requests.get(f"{BASE_URL}/api/debug/test-push", params={"token": "wrong"})
        assert r.status_code == 401, r.text
        ct = r.headers.get("content-type", "")
        assert "text/html" in ct.lower(), f"unexpected content-type: {ct!r}"
        assert "Wrong password" in r.text, r.text


# ---------- (c) GET, correct token ----------
class TestGetCorrectToken:
    def test_correct_token_returns_200_html_with_result(self):
        r = requests.get(f"{BASE_URL}/api/debug/test-push", params={"token": ADMIN_PWD})
        assert r.status_code == 200, r.text
        ct = r.headers.get("content-type", "")
        assert "text/html" in ct.lower(), f"unexpected content-type: {ct!r}"
        body = r.text

        # Headline
        assert "<h1>QuakeGuard test push</h1>" in body, body[:500]

        # Badge — one of the two acceptable values, but placeholder key
        # MUST be 'not delivered'.
        assert "not delivered" in body or "delivered" in body
        assert "not delivered" in body, (
            "placeholder EMERGENT_PUSH_KEY must render 'not delivered'"
        )

        # Recipients: <int>
        m = re.search(r"Recipients:</b>\s*(\d+)", body)
        assert m is not None, f"missing 'Recipients:' integer line in HTML: {body[:800]}"
        recipients = int(m.group(1))
        assert recipients >= 0

        # Push delivered: line
        assert "Push delivered:" in body, body[:800]
        # With placeholder key, ok is False
        assert re.search(r"Push delivered:</b>\s*False", body), body[:800]

        # Error line for placeholder key
        assert "Error:" in body, body[:800]
        assert "EMERGENT_PUSH_KEY missing or invalid" in body, body[:800]


# ---------- (d) POST with header still returns JSON (backward compat) ----------
class TestPostBackCompat:
    def test_post_with_header_returns_json(self):
        r = requests.post(
            f"{BASE_URL}/api/debug/test-push",
            headers={"X-Admin-Token": ADMIN_PWD},
        )
        assert r.status_code == 200, r.text
        ct = r.headers.get("content-type", "")
        assert "application/json" in ct.lower(), f"unexpected content-type: {ct!r}"
        data = r.json()
        # required JSON keys
        for k in ("recipients", "push_delivered", "push_error"):
            assert k in data, f"missing key {k!r}: {data}"
        assert isinstance(data["recipients"], int)
        # placeholder key ⇒ False
        assert data["push_delivered"] is False
        assert data["push_error"] == "EMERGENT_PUSH_KEY missing or invalid"


# ---------- (e) POST with no header still 401 JSON (backward compat) ----------
class TestPostNoHeader:
    def test_post_no_header_401_json(self):
        r = requests.post(f"{BASE_URL}/api/debug/test-push")
        assert r.status_code == 401, r.text
        ct = r.headers.get("content-type", "")
        assert "application/json" in ct.lower(), f"unexpected content-type: {ct!r}"
        assert r.json().get("detail") == "Invalid or missing X-Admin-Token"


# ---------- (f) shared helper wiring ----------
class TestSharedHelper:
    def test_run_test_push_helper_shared(self):
        src = SERVER_PY.read_text()
        # exactly one definition
        defs = re.findall(r"^\s*async def _run_test_push\b", src, flags=re.MULTILINE)
        assert len(defs) == 1, f"expected 1 def of _run_test_push, found {len(defs)}"
        # awaited at least twice (once by POST, once by GET)
        awaits = re.findall(r"await\s+_run_test_push\(", src)
        assert len(awaits) >= 2, f"expected >=2 awaits of _run_test_push, found {len(awaits)}"


# ---------- REGRESSION: existing endpoints untouched ----------
class TestRegression:
    def test_debug_devices_still_ok(self):
        r = requests.get(f"{BASE_URL}/api/debug/devices")
        assert r.status_code == 200, r.text
        data = r.json()
        for k in ("device_count", "push_key_status", "admin_password_configured", "devices"):
            assert k in data
        assert isinstance(data["device_count"], int)
        assert isinstance(data["devices"], list)
        assert data["admin_password_configured"] is True

    def test_trigger_alert_with_admin_token(self):
        r = requests.post(
            f"{BASE_URL}/api/trigger-alert",
            headers={"X-Admin-Token": ADMIN_PWD},
            json={"triggeredBy": "TEST_regression_trigger", "magnitude": 6.4},
        )
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["status"] == "broadcast"
        assert isinstance(data["recipients"], int)
        assert "push_delivered" in data

    def test_register_push_upserts(self):
        import uuid
        user_id = f"TEST_regr_{uuid.uuid4().hex[:6]}"
        r = requests.post(
            f"{BASE_URL}/api/register-push",
            json={"user_id": user_id, "platform": "ios", "device_token": "tok-regr-abc"},
        )
        # With placeholder EMERGENT_PUSH_KEY upstream returns 401 which the
        # endpoint re-raises as 500. Real key would return 201. In BOTH cases
        # the local upsert must have happened BEFORE the upstream call.
        assert r.status_code in (201, 500), r.text
        # confirm visible in /debug/devices — this is the real proof of upsert
        r2 = requests.get(f"{BASE_URL}/api/debug/devices")
        assert r2.status_code == 200
        ids = [d.get("user_id") for d in r2.json()["devices"]]
        assert user_id in ids, f"upsert failed — {user_id} missing"

    def test_legacy_status_get_and_post(self):
        import uuid
        client_name = f"TEST_status_{uuid.uuid4().hex[:6]}"
        rp = requests.post(
            f"{BASE_URL}/api/status",
            json={"client_name": client_name},
        )
        assert rp.status_code == 200, rp.text
        assert rp.json().get("client_name") == client_name

        rg = requests.get(f"{BASE_URL}/api/status")
        assert rg.status_code == 200, rg.text
        names = [s.get("client_name") for s in rg.json()]
        assert client_name in names


# ---------- (e-lint) module imports cleanly ----------
class TestImports:
    def test_server_imports_query_and_htmlresponse(self):
        src = SERVER_PY.read_text()
        assert re.search(r"from fastapi import [^\n]*\bQuery\b", src), \
            "server.py must import Query from fastapi"
        assert re.search(r"from fastapi\.responses import [^\n]*\bHTMLResponse\b", src), \
            "server.py must import HTMLResponse from fastapi.responses"

    def test_server_module_parses(self):
        import ast
        src = SERVER_PY.read_text()
        ast.parse(src)  # will raise SyntaxError on failure
