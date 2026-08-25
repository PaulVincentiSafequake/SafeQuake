"""#290 — a worse report puts an acknowledged alarm back in front of you.

Paul, 2026-08-25 (reconfirmed three times):
  > When a new report arrives for a person whose last alarm was already
  > acknowledged, the alarm's yellow note updates correctly but the card
  > never regains an Acknowledge button and is never counted in the alarm
  > total.

The dedupe rule was doing its job too well. An acknowledgement means "I
have seen THIS fact and I am dealing with it" — and a person getting
worse is a new fact, so the acknowledgement no longer covers it.

Paul's ruling on the boundary (2026-08-25): ONLY worse re-alarms. A
same-or-better report updates the yellow note and nothing else, otherwise
every routine check-in from somebody already being helped would sound the
alarm again — which is how a room learns to ignore it.

Run: python -m pytest backend/tests/test_realarm_290.py -q
"""
import os
import time
import uuid

import requests
from dotenv import dotenv_values

BASE = os.environ.get("QG_BASE", "http://localhost:8001")
TOKEN = (dotenv_values("/app/backend/.env").get("ADMIN_TRIGGER_PASSWORD")
         or os.environ.get("ADMIN_TRIGGER_PASSWORD", ""))
H = {"X-Admin-Token": TOKEN, "Content-Type": "application/json"}


def _did():
    return f"qg-{int(time.time()*1000)}-{uuid.uuid4().hex[:8]}"


def _report(device_id, status, severity=None, egress=None, name="Re290"):
    body = {"device_id": device_id, "status": status, "display_name": name}
    if severity:
        body["severity"] = severity
    if egress:
        body["egress"] = egress
    r = requests.post(f"{BASE}/api/status", json=body, timeout=15)
    assert r.status_code == 200, r.text


def _mine(device_id):
    r = requests.get(f"{BASE}/api/admin/alarms", headers=H, timeout=15)
    assert r.status_code == 200, r.text
    d = r.json()
    out = []
    for g in d["groups"]:
        for p in g["people"]:
            if p["device_id"] == device_id:
                out.append({**p, "word": g["word"], "kind": g["kind"]})
    return out, d


def _ack(ids):
    r = requests.post(f"{BASE}/api/admin/alarms/ack", json={"ids": ids},
                      headers=H, timeout=15)
    assert r.status_code == 200, r.text


def test_a_worse_report_reopens_an_acknowledged_alarm():
    """The exact sequence Paul ran: alarm, acknowledge, then a worse
    report. The card must come back needing a decision.

    #303 update: one card per person, so we now assert on the person's
    single card rather than a separate "worse"-kind row alongside a
    "needs_help" one. The card's primary kind is the newest fact, which
    for this sequence is `worse`."""
    did = _did()
    _report(did, "trapped", "red")           # NEEDS HELP alarm
    rows, _ = _mine(did)
    assert len(rows) == 1 and rows[0]["acknowledged"] is False
    _ack([rows[0]["id"]])
    rows, _ = _mine(did)
    assert rows[0]["acknowledged"] is True

    # Now worse: still IMMEDIATE, but they can no longer get out.
    _report(did, "trapped", "red", egress="cannot_exit")
    rows, data = _mine(did)
    # #303: one card per person. Freshest fact leads.
    assert len(rows) == 1, rows
    card = rows[0]
    assert card["kind"] == "worse", card
    assert card["acknowledged"] is False
    assert data["unacknowledged"] >= 1


def test_a_re_raised_alarm_is_the_same_alarm_not_a_second_one():
    """Re-alarming must not print the same person twice for the same
    thing — the strip would become a scroll of duplicates.

    #303: one card per person, whether the re-raise is on the same kind
    or bumps them to a new kind, so the assertion is the number of
    cards, not the number of rows of a given kind."""
    did = _did()
    _report(did, "trapped", "yellow")
    _report(did, "trapped", "red")                       # WORSE alarm
    rows, _ = _mine(did)
    assert len(rows) == 1, rows                          # one card
    _ack([rows[0]["id"]])                                # ack the card

    _report(did, "trapped", "red", egress="cannot_exit")  # worse again
    rows, _ = _mine(did)
    assert len(rows) == 1, rows                          # still one card
    assert rows[0]["acknowledged"] is False
    # A person who has been re-raised has that history in their story.
    words = " ".join(s["words"] for s in rows[0]["story"])
    assert "got worse" in words or "cannot get out" in words, words


def test_any_new_report_reopens_an_acknowledged_alarm():
    """#303 (Paul, 2026-08-26 — supersedes #290's boundary): an
    acknowledgement means "I have seen THIS fact." Any new fact — worse,
    same, or even better — is something the operator has not seen since
    they acknowledged, so the card has to go back into needs-action.

    This replaces the old "same-or-better is only information" rule,
    which allowed a brand-new report to sit on the board camouflaged as
    already-handled."""
    did = _did()
    _report(did, "trapped", "red")
    rows, _ = _mine(did)
    _ack([rows[0]["id"]])
    rows, _ = _mine(did)
    assert rows[0]["acknowledged"] is True

    # A "better" report is still a new fact.
    _report(did, "trapped", "green")
    rows, _ = _mine(did)
    assert rows[0]["acknowledged"] is False, rows
    assert any((r["since_report"] or {}).get("words") for r in rows)

    # And the newest report is on the card so the operator knows what to
    # re-triage.
    _ack([rows[0]["id"]])
    _report(did, "trapped", "green")     # same again
    rows, _ = _mine(did)
    assert rows[0]["acknowledged"] is False, rows


def test_an_unacknowledged_alarm_is_still_not_duplicated():
    """The original dedupe rule stands where it was right: nobody is
    shouted about twice for a fact nobody has looked at yet.

    #303: verified at the card level (one per person) rather than at the
    kind level — the strip never shows the same person twice."""
    did = _did()
    _report(did, "trapped", "yellow")
    _report(did, "trapped", "red")
    _report(did, "trapped", "red", egress="cannot_exit")
    rows, _ = _mine(did)
    assert len(rows) == 1, rows                          # one card
    # And the underlying rows are still deduped per kind in Mongo, so
    # opening the story does not show duplicates either.
    story_words = [s["words"] for s in rows[0]["story"]]
    # At most one "needs help" step (from the initial raise) and at most
    # one "got worse" step — nobody is announced twice for the same
    # fact.
    assert sum("needs help" in w for w in story_words) <= 1, story_words


def test_needing_help_again_after_being_safe_re_alarms():
    """Someone who reported safe and then asks for help again is worse
    than they were, and an old acknowledgement cannot cover that."""
    did = _did()
    _report(did, "trapped", "yellow")
    rows, _ = _mine(did)
    _ack([rows[0]["id"]])
    _report(did, "safe")
    _report(did, "trapped", "yellow")
    rows, _ = _mine(did)
    help_rows = [r for r in rows if r["kind"] == "needs_help"]
    assert help_rows and help_rows[0]["acknowledged"] is False, rows


def test_the_re_raise_keeps_the_original_acknowledgement_in_the_story():
    """#298: the card has to explain itself. A re-opened alarm must show
    that somebody DID acknowledge it, and why it came back."""
    did = _did()
    _report(did, "trapped", "yellow")
    _report(did, "trapped", "red")
    rows, _ = _mine(did)
    _ack([r["id"] for r in rows if r["kind"] == "worse"])
    _report(did, "trapped", "red", egress="cannot_exit")
    rows, _ = _mine(did)
    worse = [r for r in rows if r["kind"] == "worse"][0]
    words = " ".join(s["words"] for s in worse["story"])
    assert "Acknowledged by" in words
    assert "got worse" in words
    assert "Not acknowledged by anybody yet." in words
