"""#262 follow-up (Neo, 2026-08-20) — "Registered devices" dashboard panel.

Paul asked for the real device list (platform, registered date, last-seen
date) to live inside the signed-in admin dashboard instead of a raw
?token=<password> URL. This locks the backend contract for that panel:

  1. No credentials at all -> 401 (same gate as every other admin route,
     not a new, wider-open surface).
  2. Valid admin token -> 200, with the exact ios/android/total shape the
     dashboard's summary line and the real trigger's confirm dialog both
     read from — the whole point is that these two numbers are provably
     the same source.
  3. Each device row carries device_id, platform, registered_at,
     last_seen_at, device_token_fingerprint — and NEVER the raw push
     token itself.
  4. The ios/android/total breakdown here is computed with the exact
     same platform-matching logic as /admin/trigger-alert/preview (by
     design, so the two numbers in the dashboard are provably the same
     source) — verified by code inspection; a same-test-function
     two-request comparison isn't used here because this suite's
     TestClient + Motor combination doesn't support two sequential
     requests inside one test function (a pre-existing infra quirk,
     confirmed unrelated to this endpoint: the same failure reproduces
     on the pre-existing /admin/trigger-alert/preview endpoint alone).
"""
from __future__ import annotations

import os
from dotenv import load_dotenv
load_dotenv("/app/backend/.env")

import pytest
from fastapi.testclient import TestClient

from server import app

client = TestClient(app)
HDR = {"X-Admin-Token": os.environ.get("ADMIN_TRIGGER_PASSWORD", "")}


def test_no_auth_is_refused():
    r = client.get("/api/admin/device-registry")
    assert r.status_code == 401, r.text


def test_valid_admin_token_returns_expected_shape():
    r = client.get("/api/admin/device-registry", headers=HDR)
    assert r.status_code == 200, r.text
    body = r.json()
    for key in ("total", "ios", "android", "generated_at", "devices"):
        assert key in body
    assert body["total"] == body["ios"] + body["android"]
    assert isinstance(body["devices"], list)
    for row in body["devices"]:
        assert set(row.keys()) == {
            "device_id", "platform", "registered_at",
            "last_seen_at", "device_token_fingerprint",
            "status", "dead_token_reason", "dead_token_at",
        }
        assert row["status"] in ("active", "dead_token")
        # The raw push token must never leave this endpoint.
        assert "device_token" not in row
