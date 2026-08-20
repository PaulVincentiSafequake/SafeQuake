"""#135 / #266 / #267 (Neo, 2026-08-20 — Paul) — safety-control invariants.

Live-HTTP tests against the running preview backend (same pattern as
test_register_push_266.py) so we sidestep the Motor + TestClient
event-loop leakage documented in test_register_push_262.py's header.

Scope of this file:
  1. #267 phrase length + case: TRIGGER_ALERT_CONFIRMATION, STAND_DOWN_
     CONFIRMATION and DEVICE_PURGE_CONFIRMATION are all short one-word
     phrases (letter-distinct so muscle memory from one cannot carry
     into another). Case/whitespace-tolerant. Mismatch detail is plain
     English that names both the phrase and the action.
  2. #135 /admin/incident-status truthful: no trigger → active=false.
     A fresh trigger with no subsequent stand-down → active=true. A
     stand-down after the trigger → active=false. Auth-gated.
  3. Truthful "recall" language: the /alert/stand-down mismatch names
     STANDDOWN, not "the phrase shown in the confirm dialog".
"""
from __future__ import annotations

import os
import time
import uuid

import pytest
import requests
from dotenv import load_dotenv
load_dotenv("/app/backend/.env")

BACKEND = os.environ.get("TEST_BACKEND_URL", "http://localhost:8001")
ADMIN_TOKEN = os.environ.get("ADMIN_TRIGGER_PASSWORD", "")

# ---------- #267 phrases ----------

def test_phrases_are_one_word_and_letter_distinct():
    # The constants live in server.py; probe the endpoints' error
    # messages to confirm the deployed values.
    r = requests.post(
        f"{BACKEND}/api/trigger-alert",
        headers={"X-Admin-Token": ADMIN_TOKEN, "Content-Type": "application/json"},
        json={"confirmation_phrase": "wrong-phrase"},
        timeout=10,
    )
    assert r.status_code == 400
    trigger_detail = r.json().get("detail", "")
    assert "SIREN" in trigger_detail
    assert "send the alert" in trigger_detail.lower()

    r = requests.post(
        f"{BACKEND}/api/admin/alert/stand-down",
        headers={"X-Admin-Token": ADMIN_TOKEN, "Content-Type": "application/json"},
        json={"confirmation_phrase": "wrong-phrase"},
        timeout=10,
    )
    assert r.status_code == 400
    standdown_detail = r.json().get("detail", "")
    assert "STANDDOWN" in standdown_detail
    assert "recall" in standdown_detail.lower()

    r = requests.post(
        f"{BACKEND}/api/admin/device-registry/purge-all",
        headers={"X-Admin-Token": ADMIN_TOKEN, "Content-Type": "application/json"},
        json={"confirmation_phrase": "wrong-phrase"},
        timeout=10,
    )
    assert r.status_code == 400
    purge_detail = r.json().get("detail", "")
    assert "WIPE" in purge_detail
    assert "erase" in purge_detail.lower()

    # Letter-distinct check — Paul's rule: "make the two words
    # different enough that muscle memory from one cannot carry into
    # the other." No shared substring of length >= 3.
    words = ["SIREN", "STANDDOWN", "WIPE"]
    for i, a in enumerate(words):
        for b in words[i + 1:]:
            for k in range(len(a) - 2):
                sub = a[k:k + 3]
                assert sub not in b, (
                    f"muscle-memory collision: '{sub}' appears in both {a} and {b}"
                )


def test_trigger_phrase_case_insensitive_and_trim():
    # Empty registry so no phones get sirened by the test.
    # (Purge is idempotent when the registry is already empty.)
    requests.post(
        f"{BACKEND}/api/admin/device-registry/purge-all",
        headers={"X-Admin-Token": ADMIN_TOKEN, "Content-Type": "application/json"},
        json={"confirmation_phrase": "WIPE"},
        timeout=10,
    )

    for variant in ("siren", "  SIREN ", "SirEn"):
        r = requests.post(
            f"{BACKEND}/api/trigger-alert",
            headers={"X-Admin-Token": ADMIN_TOKEN, "Content-Type": "application/json"},
            json={"confirmation_phrase": variant, "triggeredBy": f"neo-test-{uuid.uuid4().hex[:6]}"},
            timeout=15,
        )
        assert r.status_code == 200, (variant, r.text)


# ---------- #135 incident-status ----------

def test_incident_status_requires_auth():
    r = requests.get(f"{BACKEND}/api/admin/incident-status", timeout=10)
    assert r.status_code in (401, 403), r.text


def test_incident_status_reflects_trigger_and_stand_down():
    assert ADMIN_TOKEN, "ADMIN_TRIGGER_PASSWORD not set"

    # Close whatever might already be open.
    requests.post(
        f"{BACKEND}/api/admin/alert/stand-down",
        headers={"X-Admin-Token": ADMIN_TOKEN, "Content-Type": "application/json"},
        json={"confirmation_phrase": "STANDDOWN", "reason": "test setup"},
        timeout=10,
    )
    time.sleep(0.2)

    # After a stand-down, active should be false.
    r = requests.get(
        f"{BACKEND}/api/admin/incident-status",
        headers={"X-Admin-Token": ADMIN_TOKEN}, timeout=10,
    )
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["active"] is False, d
    assert d["last_stand_down_at"] is not None

    # Trigger a new alert.
    tr = requests.post(
        f"{BACKEND}/api/trigger-alert",
        headers={"X-Admin-Token": ADMIN_TOKEN, "Content-Type": "application/json"},
        json={"confirmation_phrase": "SIREN", "triggeredBy": f"neo-135-{uuid.uuid4().hex[:6]}"},
        timeout=15,
    )
    assert tr.status_code == 200, tr.text
    time.sleep(0.5)

    # Now active should be true, with a plain-English reason.
    r = requests.get(
        f"{BACKEND}/api/admin/incident-status",
        headers={"X-Admin-Token": ADMIN_TOKEN}, timeout=10,
    )
    d = r.json()
    assert d["active"] is True, d
    assert isinstance(d["reason"], str) and "stand-down" in d["reason"].lower()
    assert d["hours_since_trigger"] is not None
    assert d["hours_since_trigger"] < 72

    # Stand it down and re-check.
    requests.post(
        f"{BACKEND}/api/admin/alert/stand-down",
        headers={"X-Admin-Token": ADMIN_TOKEN, "Content-Type": "application/json"},
        json={"confirmation_phrase": "STANDDOWN", "reason": "test cleanup"},
        timeout=10,
    )
    time.sleep(0.3)
    r = requests.get(
        f"{BACKEND}/api/admin/incident-status",
        headers={"X-Admin-Token": ADMIN_TOKEN}, timeout=10,
    )
    assert r.status_code == 200
    d = r.json()
    assert d["active"] is False, d
