"""#268 — the operator decision endpoints, over HTTP.

Doctrine under test: "Records get resolved by a human with a reason
recorded, never removed silently by software. This will be read back in
an inquiry." So every endpoint here must refuse an unattributed or
unexplained removal, and must never delete anything.

One request per test function: this suite's TestClient + Motor
combination cannot reliably do two async-touching calls in the same test
(same constraint documented in the #262 files).
"""
from __future__ import annotations

import os

from dotenv import load_dotenv
load_dotenv("/app/backend/.env")

from fastapi.testclient import TestClient  # noqa: E402

from server import app, RESOLVE_REASONS  # noqa: E402

client = TestClient(app)
HDR = {"X-Admin-Token": os.environ.get("ADMIN_TRIGGER_PASSWORD", "")}
DEV = "qg-1755700000001-neo268a"


def test_resolve_needs_authentication():
    r = client.post(f"/api/admin/records/{DEV}/resolve",
                    json={"reason_code": "test_entry"})
    assert r.status_code == 401, r.text


def test_resolve_refuses_an_unexplained_removal():
    r = client.post(f"/api/admin/records/{DEV}/resolve", json={}, headers=HDR)
    assert r.status_code == 400
    # The message has to name the choices, not just say "invalid".
    for code in RESOLVE_REASONS:
        assert code in r.json()["detail"]


def test_resolve_refuses_other_without_a_note():
    r = client.post(f"/api/admin/records/{DEV}/resolve",
                    json={"reason_code": "other", "reason": ""}, headers=HDR)
    assert r.status_code == 400
    assert "why" in r.json()["detail"]


def test_resolve_refuses_an_unknown_record():
    r = client.post("/api/admin/records/qg-0000000000000-nosuchid/resolve",
                    json={"reason_code": "test_entry"}, headers=HDR)
    assert r.status_code == 404


def test_duplicate_decision_needs_authentication():
    r = client.post(f"/api/admin/records/{DEV}/duplicate-decision",
                    json={"other_device_id": "x", "decision": "confirmed"})
    assert r.status_code == 401


def test_duplicate_decision_refuses_an_invented_verdict():
    r = client.post(f"/api/admin/records/{DEV}/duplicate-decision",
                    json={"other_device_id": "x", "decision": "probably"},
                    headers=HDR)
    assert r.status_code == 400
    assert "confirmed" in r.json()["detail"]


def test_duplicate_decision_needs_the_other_record():
    r = client.post(f"/api/admin/records/{DEV}/duplicate-decision",
                    json={"decision": "confirmed"}, headers=HDR)
    assert r.status_code == 400


def test_unresolve_needs_authentication():
    r = client.post(f"/api/admin/records/{DEV}/unresolve", json={})
    assert r.status_code == 401
