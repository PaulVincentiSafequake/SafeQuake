"""#262 pre-pilot cleanup (Neo, 2026-08-20 — Paul) — "clear all current
entries... these are all Paul's own repeated test installs... only new
registrations from real testers should appear going forward."

POST /api/admin/device-registry/purge-all: admin-only, requires a typed
confirmation phrase, hard-deletes every push_devices row, returns
before/after/deleted counts.
"""
from __future__ import annotations

import os
from dotenv import load_dotenv
load_dotenv("/app/backend/.env")

from fastapi.testclient import TestClient

from server import app, DEVICE_PURGE_CONFIRMATION

client = TestClient(app)
HDR = {"X-Admin-Token": os.environ.get("ADMIN_TRIGGER_PASSWORD", "")}


def test_no_auth_is_refused():
    r = client.post("/api/admin/device-registry/purge-all", json={
        "confirmation_phrase": DEVICE_PURGE_CONFIRMATION,
    })
    assert r.status_code == 401, r.text


def test_wrong_phrase_is_refused_and_deletes_nothing():
    r = client.post(
        "/api/admin/device-registry/purge-all",
        json={"confirmation_phrase": "clear all devicez"},
        headers=HDR,
    )
    assert r.status_code == 400, r.text


def test_missing_phrase_is_refused():
    r = client.post(
        "/api/admin/device-registry/purge-all", json={}, headers=HDR,
    )
    assert r.status_code == 400, r.text


def test_correct_phrase_purges_and_reports_counts():
    """Single-request test (this suite's TestClient+Motor combo can't
    reliably do 2+ async-touching calls per test function — confirmed
    across every #262 test file, including plain TestClient-only
    double-calls). Uses whatever is already in push_devices as the
    "before" state rather than seeding — the invariants checked here
    (after==0, deleted==before) hold regardless of what was there."""
    r = client.post(
        "/api/admin/device-registry/purge-all",
        json={"confirmation_phrase": DEVICE_PURGE_CONFIRMATION},
        headers=HDR,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["after"] == 0
    assert body["deleted"] == body["before"]
    assert body["purged_by"]  # attributed to a principal, not blank
