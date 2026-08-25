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
    report. The card must come back needing a decision."""
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
    worse = [r for r in rows if r["kind"] == "worse"]
    assert worse, rows
    assert worse[0]["acknowledged"] is False
    assert worse[0]["id"] in [i for g in data["groups"] for i in g["unacked_ids"]]
    assert data["unacknowledged"] >= 1


def test_a_re_raised_alarm_is_the_same_alarm_not_a_second_one():
    """Re-alarming must not print the same person twice for the same
    thing — the strip would become a scroll of duplicates."""
    did = _did()
    _report(did, "trapped", "yellow")
    _report(did, "trapped", "red")                       # WORSE alarm
    rows, _ = _mine(did)
    worse = [r for r in rows if r["kind"] == "worse"]
    assert len(worse) == 1
    _ack([r["id"] for r in rows])

    _report(did, "trapped", "red", egress="cannot_exit")  # worse again
    rows, _ = _mine(did)
    worse2 = [r for r in rows if r["kind"] == "worse"]
    assert len(worse2) == 1, worse2
    assert worse2[0]["id"] == worse[0]["id"]
    assert worse2[0]["acknowledged"] is False
    assert worse2[0]["re_raise_count"] == 1
    assert worse2[0]["re_raised_at"]


def test_a_same_or_better_report_does_not_re_alarm():
    """Paul's boundary. An improvement is information, not an alarm — it
    updates the note and leaves the acknowledgement standing."""
    did = _did()
    _report(did, "trapped", "red")
    rows, _ = _mine(did)
    _ack([rows[0]["id"]])

    _report(did, "trapped", "green")     # better
    rows, _ = _mine(did)
    assert all(r["acknowledged"] for r in rows), rows
    assert any((r["since_report"] or {}).get("words") for r in rows)

    _report(did, "trapped", "green")     # same again
    rows, _ = _mine(did)
    assert all(r["acknowledged"] for r in rows), rows


def test_an_unacknowledged_alarm_is_still_not_duplicated():
    """The original dedupe rule stands where it was right: nobody is
    shouted about twice for a fact nobody has looked at yet."""
    did = _did()
    _report(did, "trapped", "yellow")
    _report(did, "trapped", "red")
    _report(did, "trapped", "red", egress="cannot_exit")
    rows, _ = _mine(did)
    assert len([r for r in rows if r["kind"] == "worse"]) == 1
    assert len([r for r in rows if r["kind"] == "needs_help"]) == 1


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
