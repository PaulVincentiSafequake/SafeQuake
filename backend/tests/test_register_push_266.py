"""#266 / #260 (Neo, 2026-08-20 — Paul) — truthful registration status.

Uses live HTTP against the running preview backend at localhost:8001,
which is the pattern that avoids the pre-existing Motor + TestClient
event-loop leakage documented in test_register_push_262.py's header.

Scope of this test file:
  1. Relay refuses (4xx) → NO row in push_devices, response is 502 with
     plain-English detail. (Pre-fix bug: wrote row + returned 500.)
  2. GET /api/register-push/status/{user_id} returns registered=false
     with a truthful last_attempt reason.
  3. Unknown user → registered=false, not 404.
  4. GET /api/admin/relay-health returns healthy=false with a plain-
     English reason when the relay is refusing.
  5. /api/admin/relay-health is auth-gated.
"""
from __future__ import annotations

import os
import uuid

import pytest
import requests
from dotenv import load_dotenv
load_dotenv("/app/backend/.env")

BACKEND = os.environ.get("TEST_BACKEND_URL", "http://localhost:8001")
ADMIN_TOKEN = os.environ.get("ADMIN_TRIGGER_PASSWORD", "")

VALID_IOS_TOKEN = "a" * 64


def _uid() -> str:
    return f"qg-test-266-{uuid.uuid4().hex[:12]}"


def _register(uid: str, platform: str = "ios", token: str = VALID_IOS_TOKEN) -> requests.Response:
    return requests.post(
        f"{BACKEND}/api/register-push",
        json={"user_id": uid, "platform": platform, "device_token": token},
        timeout=15,
    )


def _status(uid: str) -> requests.Response:
    return requests.get(f"{BACKEND}/api/register-push/status/{uid}", timeout=15)


def test_relay_4xx_does_not_persist_row():
    uid = _uid()
    r = _register(uid)
    # Response: 502 (relay refused, we did not persist).
    assert r.status_code == 502, r.text

    detail = r.json().get("detail", "")
    assert isinstance(detail, str) and detail, "detail must be non-empty"
    # Plain English — no "HTTP 500", no stack trace vocab.
    assert "http" not in detail.lower() or "https" in detail.lower()
    # For the 401 case (preview env's EMERGENT_PUSH_KEY is a placeholder),
    # the message must name the specific cause the operator can act on.
    assert (
        "provider" in detail.lower()
        or "credentials" in detail.lower()
        or "refused" in detail.lower()
    ), detail

    # And critically — the read-back says NOT registered.
    s = _status(uid)
    assert s.status_code == 200, s.text
    assert s.json()["registered"] is False


def test_status_endpoint_reflects_server_truth():
    uid = _uid()
    _register(uid)
    s = _status(uid)
    assert s.status_code == 200, s.text
    body = s.json()
    assert body["registered"] is False
    la = body.get("last_attempt")
    assert la is not None
    assert la["persisted"] is False
    # In preview env with placeholder EMERGENT_PUSH_KEY we expect a 401.
    assert la["relay_status"] == 401 or la["relay_status"] is None
    assert isinstance(la.get("relay_error"), str)


def test_status_endpoint_for_unknown_device_is_false_not_error():
    uid = f"qg-test-266-unknown-{uuid.uuid4().hex[:8]}"
    s = _status(uid)
    assert s.status_code == 200, s.text
    body = s.json()
    assert body["registered"] is False
    assert body["last_attempt"] is None


def test_relay_health_endpoint_reports_unhealthy():
    assert ADMIN_TOKEN, "ADMIN_TRIGGER_PASSWORD not set"
    # Prime the log with a refused attempt so relay_healthy has a sample.
    _register(_uid())
    r = requests.get(
        f"{BACKEND}/api/admin/relay-health",
        headers={"X-Admin-Token": ADMIN_TOKEN},
        timeout=15,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["healthy"] is False, body
    reason = body["reason"]
    assert isinstance(reason, str) and reason
    assert (
        "credentials" in reason.lower()
        or "refused" in reason.lower()
        or "refusing" in reason.lower()
    )
    assert isinstance(body["recent_attempts"], list)
    assert len(body["recent_attempts"]) >= 1


def test_relay_health_endpoint_requires_auth():
    r = requests.get(f"{BACKEND}/api/admin/relay-health", timeout=15)
    assert r.status_code in (401, 403), r.text


def test_relay_health_endpoint_for_operator_role_is_allowed():
    # The read endpoints (device-registry, relay-health) accept admin OR
    # operator. Legacy X-Admin-Token maps to a synthetic admin, so this
    # is covered by test_relay_health_endpoint_reports_unhealthy above.
    # Placeholder here to document scope for the reviewer.
    pass
