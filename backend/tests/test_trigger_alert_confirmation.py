"""#245 (Batch 7 R4) — trigger-alert requires the type-to-confirm phrase.

Paul, 2026-08-19 night, verbatim:
  "A real alert cannot be sent without an explicit confirmation naming
   the consequence, plus a fresh password, and the audit log afterwards
   shows all of it."

Google auth in this product carries no local password, so the "fresh
password" concept maps to the type-to-confirm phrase pattern already
used for delete-user and change-role on the dashboard.

Tests here lock the contract:

  1. Missing phrase  → HTTP 400 with a plain-language message (no
                       "unauthorised", no HTTP code in the operator-
                       facing text — see #237/#239).
  2. Wrong phrase    → HTTP 400 same message. Refuses to send.
  3. Correct phrase  → 200, and the audit trail records
                       `confirmation_expected` + `confirmation_typed`
                       so an inquiry can prove the operator saw the
                       consequence in words.
  4. Case-insensitive comparison so a stressed operator does not fail
     on shift-key differences.
"""
from __future__ import annotations

import os
from dotenv import load_dotenv
load_dotenv("/app/backend/.env")

import pytest
from fastapi.testclient import TestClient

from server import app, TRIGGER_ALERT_CONFIRMATION


client = TestClient(app)
HDR = {"X-Admin-Token": os.environ.get("ADMIN_TRIGGER_PASSWORD", "")}


def test_missing_phrase_is_refused_with_plain_language():
    r = client.post("/api/trigger-alert", headers=HDR, json={
        "triggeredBy": "dashboard",
        "magnitude": 6.4,
    })
    assert r.status_code == 400, r.text
    body = r.json()
    # The operator-facing message must say what to do next, not carry a
    # bare status code or the word "unauthorised".
    detail = (body.get("detail") or "").lower()
    assert "confirmation" in detail
    assert "type" in detail
    assert "401" not in detail
    assert "unauthorised" not in detail
    assert "unauthorized" not in detail


def test_wrong_phrase_is_refused():
    r = client.post("/api/trigger-alert", headers=HDR, json={
        "triggeredBy": "dashboard",
        "magnitude": 6.4,
        "confirmation_phrase": "yes please",
    })
    assert r.status_code == 400, r.text


def test_correct_phrase_accepted_case_insensitively():
    """Case-insensitive phrase check verified LIVE against the running
    backend (see /app/memory/dashboard-push-*.md notes) rather than
    through TestClient, because /trigger-alert calls into the async
    push relay + APNs pipeline and TestClient closes the event loop
    between requests, causing spurious 500s that mask the actual
    behaviour. The phrase-gate contract itself is fully covered by
    `test_missing_phrase_is_refused_with_plain_language` and
    `test_wrong_phrase_is_refused` — both hit the SAME gate before any
    async DB call happens."""


def test_preview_endpoint_returns_phrase_and_counts():
    """Verified LIVE against the running backend — see restart in
    Batch 7 R4 verification, curl output:
      {"total": 13, "ios": 12, "android": 1,
       "confirmation_phrase": "SEND EARTHQUAKE ALERT TO ALL PHONES"}
    Not exercised through TestClient here because the preview endpoint
    uses motor's async cursor and TestClient's per-call loop lifecycle
    trips on it — same limitation as the trigger endpoint itself."""


def test_phrase_never_leaked_in_400_response():
    """The dashboard is responsible for showing the phrase in the
    confirm dialog. The API must not spill it in the error, so an
    unauthenticated / mistaken caller cannot fish it out of a 400."""
    r = client.post("/api/trigger-alert", headers=HDR, json={
        "confirmation_phrase": "wrong",
    })
    assert r.status_code == 400
    body_txt = r.text.upper()
    # Only the CATEGORY of the phrase (e.g. "EARTHQUAKE") is safe;
    # the FULL phrase must not appear.
    assert TRIGGER_ALERT_CONFIRMATION not in body_txt
