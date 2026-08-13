"""
CORS verification tests for QuakeGuard/Quake Angel FastAPI backend.

Verifies fix in /app/backend/server.py CORSMiddleware block:
- explicit allow_origins list expanded with malta.quakeangel.app, quakeangel.app, www.quakeangel.app
- allow_origin_regex broadened to match https://<sub>.quakeangel.app + http://localhost:<port>
"""

import pytest
import requests

BASE_URL = "http://localhost:8001"

import os
from dotenv import load_dotenv
load_dotenv("/app/backend/.env")
# GET /api/devices and /api/audit are operator/admin gated as of
# 2026-08-13 — the actual-request CORS checks authenticate so they can
# still assert on 200 responses. Preflights stay unauthenticated
# (browsers never attach credentials to OPTIONS).
ADMIN_TOKEN = os.environ.get("ADMIN_TRIGGER_PASSWORD")

# Passing origins — must be echoed back in Access-Control-Allow-Origin
PASSING_ORIGINS = [
    "https://malta.quakeangel.app",
    "https://safequake.onrender.com",
    "https://quakeangel.app",
    "https://www.quakeangel.app",
    "https://london.quakeangel.app",
    "http://localhost:5173",
]

# Rejected origins — must NOT get their origin echoed back
REJECTED_ORIGINS = [
    "https://evil.example.com",
    "http://malta.quakeangel.app",       # http not https
    "https://quakeangel.app.evil.com",   # spoofed suffix
]

# Endpoints hit by dashboard
GET_ENDPOINTS = ["/api/devices", "/api/audit?limit=50"]
POST_ENDPOINTS = ["/api/mark-rescued", "/api/trigger-alert"]


def _preflight(endpoint: str, origin: str, method: str = "GET"):
    return requests.options(
        f"{BASE_URL}{endpoint}",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": method,
            "Access-Control-Request-Headers": "content-type,x-admin-token",
        },
        timeout=10,
    )


def _get_with_origin(endpoint: str, origin: str):
    return requests.get(
        f"{BASE_URL}{endpoint}",
        headers={"Origin": origin, "X-Admin-Token": ADMIN_TOKEN},
        timeout=10,
    )


# ---------------- Passing origins ----------------

@pytest.mark.parametrize("origin", PASSING_ORIGINS)
@pytest.mark.parametrize("endpoint", GET_ENDPOINTS)
def test_options_preflight_allowed_origin(origin, endpoint):
    r = _preflight(endpoint, origin, method="GET")
    assert r.status_code in (200, 204), (
        f"OPTIONS {endpoint} from {origin} got {r.status_code}"
    )
    aco = r.headers.get("access-control-allow-origin")
    assert aco == origin or aco == "*", (
        f"OPTIONS {endpoint} from {origin}: expected ACAO={origin} or *, got {aco!r}"
    )


@pytest.mark.parametrize("origin", PASSING_ORIGINS)
@pytest.mark.parametrize("endpoint", GET_ENDPOINTS)
def test_get_actual_request_allowed_origin(origin, endpoint):
    r = _get_with_origin(endpoint, origin)
    assert r.status_code == 200, (
        f"GET {endpoint} from {origin} status {r.status_code}"
    )
    aco = r.headers.get("access-control-allow-origin")
    assert aco == origin or aco == "*", (
        f"GET {endpoint} from {origin}: expected ACAO={origin} or *, got {aco!r}"
    )


def test_get_devices_returns_devices_array():
    """Sanity: /api/devices returns a body with a 'devices' array."""
    r = _get_with_origin("/api/devices", "https://malta.quakeangel.app")
    assert r.status_code == 200
    body = r.json()
    assert "devices" in body, f"expected 'devices' key, got keys={list(body.keys())}"
    assert isinstance(body["devices"], list), "'devices' must be a list"


@pytest.mark.parametrize("origin", PASSING_ORIGINS)
@pytest.mark.parametrize("endpoint", POST_ENDPOINTS)
def test_options_preflight_post_endpoints(origin, endpoint):
    """POST endpoints (mark-rescued, trigger-alert): preflight must succeed for allowed origins."""
    r = _preflight(endpoint, origin, method="POST")
    assert r.status_code in (200, 204), (
        f"OPTIONS {endpoint} from {origin} got {r.status_code}"
    )
    aco = r.headers.get("access-control-allow-origin")
    assert aco == origin or aco == "*", (
        f"OPTIONS {endpoint} from {origin}: expected ACAO={origin} or *, got {aco!r}"
    )


# ---------------- Rejected origins ----------------

@pytest.mark.parametrize("origin", REJECTED_ORIGINS)
@pytest.mark.parametrize("endpoint", GET_ENDPOINTS)
def test_options_preflight_rejected_origin(origin, endpoint):
    r = _preflight(endpoint, origin, method="GET")
    aco = r.headers.get("access-control-allow-origin")
    assert aco != origin and aco != "*", (
        f"REJECTED origin {origin} was echoed back on OPTIONS {endpoint}: ACAO={aco!r}"
    )


@pytest.mark.parametrize("origin", REJECTED_ORIGINS)
@pytest.mark.parametrize("endpoint", GET_ENDPOINTS)
def test_get_rejected_origin_no_acao(origin, endpoint):
    r = _get_with_origin(endpoint, origin)
    aco = r.headers.get("access-control-allow-origin")
    assert aco != origin and aco != "*", (
        f"REJECTED origin {origin} was echoed back on GET {endpoint}: ACAO={aco!r}"
    )
