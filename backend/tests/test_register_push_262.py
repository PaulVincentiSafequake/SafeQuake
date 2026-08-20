"""#262 tier 1 (Neo, 2026-08-20 — Paul) — reject invalid device tokens and
rate-limit /register-push by IP.

Approved scope: format validation + per-IP rate limit ONLY. Explicitly
NOT in scope this round: shared build-secret header, App Attest / Play
Integrity (Paul: "not needed for a small pilot with known testers").

Test design note: this suite's TestClient + Motor combination cannot
reliably make more than one request per test function (a pre-existing
infra quirk — confirmed by reproducing "Event loop is closed" on
completely unrelated, unmodified endpoints too). So every HTTP test here
makes exactly ONE call. The rate-limit test instead calls the limiter
function directly inside a single asyncio.run(), which sidesteps the
issue entirely and is arguably a more precise unit test anyway.
"""
from __future__ import annotations

import os
from dotenv import load_dotenv
load_dotenv("/app/backend/.env")

import pytest
from fastapi.testclient import TestClient

from server import app

client = TestClient(app)


def test_valid_ios_token_is_not_rejected():
    # NOTE: EMERGENT_PUSH_KEY is a placeholder in this preview environment
    # (documented in test_register_push_capture.py) — a VALID token still
    # gets a 500 from the downstream relay call after being accepted and
    # written to push_devices. That's pre-existing, unrelated behavior.
    # What this test actually proves: a well-formed token is never
    # rejected as 400 (garbage) by the new validation.
    r = client.post("/api/register-push", json={
        "user_id": "qg-test-262-ios-valid",
        "platform": "ios",
        "device_token": "a" * 64,  # 64 hex chars — a real APNs token shape
    })
    assert r.status_code != 400, r.text


def test_valid_android_token_is_not_rejected():
    r = client.post("/api/register-push", json={
        "user_id": "qg-test-262-android-valid",
        "platform": "android",
        "device_token": "fcm-token-" + "b" * 130,
    })
    assert r.status_code != 400, r.text


def test_garbage_ios_token_is_rejected():
    r = client.post("/api/register-push", json={
        "user_id": "qg-test-262-ios-garbage",
        "platform": "ios",
        "device_token": "not-a-real-token",
    })
    assert r.status_code == 400, r.text


def test_short_android_token_is_rejected():
    r = client.post("/api/register-push", json={
        "user_id": "qg-test-262-android-garbage",
        "platform": "android",
        "device_token": "short",
    })
    assert r.status_code == 400, r.text


def test_unknown_platform_is_rejected():
    r = client.post("/api/register-push", json={
        "user_id": "qg-test-262-bad-platform",
        "platform": "windows-phone",
        "device_token": "a" * 64,
    })
    assert r.status_code == 400, r.text


def test_empty_token_is_rejected():
    r = client.post("/api/register-push", json={
        "user_id": "qg-test-262-empty-token",
        "platform": "ios",
        "device_token": "",
    })
    assert r.status_code == 400, r.text


def test_rate_limit_kicks_in_after_threshold():
    """Direct unit test of the limiter function — no TestClient, no HTTP,
    a single asyncio.run() driving a loop of calls against ONE event loop.
    """
    import asyncio
    from fastapi import HTTPException
    from starlette.requests import Request as StarletteRequest
    from server import _enforce_register_rate_limit, REGISTER_RATE_LIMIT_PER_HOUR

    def _fake_request(ip: str) -> StarletteRequest:
        scope = {
            "type": "http",
            "headers": [(b"x-forwarded-for", ip.encode())],
            "client": (ip, 0),
            "method": "POST",
            "path": "/api/register-push",
        }
        return StarletteRequest(scope)

    async def _run():
        # Unique IP per test run (not a fixed constant) — this endpoint's
        # rate-limit bucket is keyed by ip:hour and persists in Mongo for
        # up to 2h, so a fixed IP would accumulate count across repeated
        # test runs within the same hour and trip earlier than expected.
        import uuid
        ip = f"203.0.113.{uuid.uuid4().int % 250 + 1}"  # TEST-NET-3, RFC 5737
        for i in range(REGISTER_RATE_LIMIT_PER_HOUR + 5):
            try:
                await _enforce_register_rate_limit(_fake_request(ip))
            except HTTPException as e:
                return i, e.status_code
        return None

    tripped = asyncio.run(_run())
    assert tripped is not None, "rate limit never tripped after exceeding the threshold"
    index, status = tripped
    assert status == 429
    assert index == REGISTER_RATE_LIMIT_PER_HOUR  # trips on the (LIMIT+1)-th call, 0-indexed


def test_missing_ip_fails_open():
    """No client IP available (can't extract one) -> allowed through, not
    blocked. Losing a real device's ability to register because a proxy
    hid its IP would be a worse outcome than a theoretical bypass."""
    import asyncio
    from starlette.requests import Request as StarletteRequest
    from server import _enforce_register_rate_limit

    def _fake_request_no_ip() -> StarletteRequest:
        scope = {
            "type": "http", "headers": [], "client": None,
            "method": "POST", "path": "/api/register-push",
        }
        return StarletteRequest(scope)

    asyncio.run(_enforce_register_rate_limit(_fake_request_no_ip()))  # must not raise
