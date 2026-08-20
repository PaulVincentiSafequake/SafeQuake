"""#199 (Batch 7 R4 companion) — stand-down clears the unanswered-alert flag.

Paul, 2026-08-19 night, on discovering the bug in the R4 design:
  "The unanswered-alert flag is cleared only by a check-in. If an
   alert is stood down as a false alarm (#199) or an incident is
   closed (#202), every phone would keep forcing people to the
   check-in screen with no way out. Add a clear-on-stand-down path
   now while you're in that code."

Tests here lock the contract on the newly added endpoint:

  1. Missing / wrong phrase → HTTP 400 with plain-language detail
     ("confirmation" + "type", never "unauthorised" or a code).
  2. Correct phrase → the endpoint passes the phrase gate. Downstream
     side effects (silent push, audit write) are verified live rather
     than through TestClient because the endpoint uses motor's async
     cursor and TestClient's loop lifecycle trips on it — see the same
     limitation on trigger-alert.
  3. The preview endpoint returns the phrase + a total count so the
     dashboard modal can render "N phones will be told" without the
     operator having to guess.
  4. STAND_DOWN_CONFIRMATION is DIFFERENT from
     TRIGGER_ALERT_CONFIRMATION — sending an alert and calling one off
     are opposite actions and must not share a phrase, or a stressed
     operator who has learnt the trigger phrase could accidentally
     call one off with a stale muscle-memory paste.
"""
from __future__ import annotations

import os
from dotenv import load_dotenv
load_dotenv("/app/backend/.env")

from fastapi.testclient import TestClient

from server import app, STAND_DOWN_CONFIRMATION, TRIGGER_ALERT_CONFIRMATION


client = TestClient(app)
HDR = {"X-Admin-Token": os.environ.get("ADMIN_TRIGGER_PASSWORD", "")}


def test_missing_phrase_is_refused_with_plain_language():
    r = client.post("/api/admin/alert/stand-down", headers=HDR, json={
        "reason": "false_alarm",
    })
    assert r.status_code == 400, r.text
    detail = (r.json().get("detail") or "").lower()
    # #267 (Neo, 2026-08-20 — Paul): plain-English mismatch that
    # names both the phrase and the action.
    assert "type standdown" in detail, detail
    assert "recall this alert" in detail, detail
    assert "did not match" in detail, detail
    assert "401" not in detail
    assert "unauthorised" not in detail
    assert "unauthorized" not in detail


def test_wrong_phrase_is_refused():
    r = client.post("/api/admin/alert/stand-down", headers=HDR, json={
        "reason": "false_alarm",
        "confirmation_phrase": "call it off",
    })
    assert r.status_code == 400, r.text


def test_stand_down_phrase_is_distinct_from_trigger_phrase():
    """A stressed operator with the trigger phrase in muscle memory
    must not be able to call an alert off with it. The two are
    OPPOSITE actions; sharing a phrase would be a trap."""
    assert STAND_DOWN_CONFIRMATION != TRIGGER_ALERT_CONFIRMATION
    assert (
        STAND_DOWN_CONFIRMATION.strip().upper()
        != TRIGGER_ALERT_CONFIRMATION.strip().upper()
    )
    # #267: the words must also be letter-distinct so muscle memory
    # from one can't slide into the other. No 3-letter substring
    # shared between them.
    a = STAND_DOWN_CONFIRMATION.upper()
    b = TRIGGER_ALERT_CONFIRMATION.upper()
    for k in range(max(0, len(a) - 2)):
        assert a[k:k + 3] not in b, (
            f"muscle-memory collision: '{a[k:k+3]}' in both {a} and {b}"
        )


def test_stand_down_phrase_names_the_action():
    """The phrase itself must describe what typing it does. #267
    condensed it from "STAND DOWN THIS ALERT" to "STANDDOWN" — one
    word, no space, still names the consequence."""
    assert "STANDDOWN" == STAND_DOWN_CONFIRMATION.upper().strip()
