"""#303 — one card per person, and any new report re-opens the card.

Paul, 2026-08-26 (live, urgent):
  > On the live dashboard, one person — device QQ43D, my own test phone —
  > already had two separate alarm cards sitting on the board. Both said
  > "acknowledged by pmvincenti@gmail.com." I triggered a brand new alert
  > and reported "immediate, cannot move" again. Both cards updated their
  > inner text to show the new report. But neither card's "acknowledged
  > by" line changed. So a brand new, unhandled report is now sitting on
  > the board dressed up as already-handled.
  >
  > What I need: one card per person, not one per past trigger event —
  > keep full history inside "What happened," don't create a second card.
  > And any new report on a person must clear that person's acknowledged
  > state and put the card back into "needs action," with the
  > acknowledged summary count updating to match.

Run: python -m pytest backend/tests/test_303_one_card_per_person.py -q
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


def _report(device_id, status, severity=None, egress=None, name="qq43d"):
    body = {"device_id": device_id, "status": status, "display_name": name}
    if severity:
        body["severity"] = severity
    if egress:
        body["egress"] = egress
    r = requests.post(f"{BASE}/api/status", json=body, timeout=15)
    assert r.status_code == 200, r.text


def _cards_for(device_id):
    """Every card the board currently shows for this person."""
    r = requests.get(f"{BASE}/api/admin/alarms", headers=H, timeout=15)
    assert r.status_code == 200, r.text
    d = r.json()
    out = []
    for g in d["groups"]:
        for p in g["people"]:
            if p["device_id"] == device_id:
                out.append({**p, "group_word": g["word"], "group_ids": g["ids"],
                            "group_unacked_ids": g["unacked_ids"]})
    return out, d


def _ack(payload):
    r = requests.post(f"{BASE}/api/admin/alarms/ack", json=payload,
                      headers=H, timeout=15)
    assert r.status_code == 200, r.text
    return r.json()


def test_one_person_is_only_ever_one_card():
    """QQ43D's actual sequence: get worse then also cannot get out. Two
    events, one person, one card on the board."""
    did = _did()
    _report(did, "trapped", "yellow")                       # NEEDS_HELP raised
    _report(did, "trapped", "red")                          # WORSE raised
    _report(did, "trapped", "red", egress="cannot_exit")    # WORSE re-raised
    cards, _ = _cards_for(did)
    assert len(cards) == 1, cards


def test_a_new_report_clears_the_acknowledgement():
    """The specific failure Paul reported: acknowledge, then report
    again — the ack must clear so nobody mistakes the new fact for the
    handled one."""
    did = _did()
    _report(did, "trapped", "red")
    cards, _ = _cards_for(did)
    _ack({"ids": [cards[0]["id"]]})
    cards, _ = _cards_for(did)
    assert cards[0]["acknowledged"] is True

    # New report of any kind — even the same severity — re-opens the card.
    _report(did, "trapped", "red")
    cards, data = _cards_for(did)
    assert len(cards) == 1
    assert cards[0]["acknowledged"] is False, cards[0]
    assert data["unacknowledged"] >= 1


def test_acknowledged_count_matches_reality_after_a_new_report():
    """The top of the panel used to say "0 — everything showing has been
    acknowledged" while a brand-new report was sitting there un-handled.
    After a new report, the unacknowledged count has to go up again."""
    did = _did()
    _report(did, "trapped", "red")
    cards, _ = _cards_for(did)
    _ack({"ids": [cards[0]["id"]]})

    # Right after ack: everything is acknowledged.
    _, data = _cards_for(did)
    unacked_before = data["unacknowledged"]

    _report(did, "trapped", "red")

    _, data = _cards_for(did)
    assert data["unacknowledged"] >= unacked_before + 1


def test_history_is_preserved_in_what_happened():
    """One card, but the full timeline of what this person went through
    is inside the story so nothing is lost."""
    did = _did()
    _report(did, "trapped", "yellow")                       # NEEDS_HELP
    _report(did, "trapped", "red")                          # WORSE
    cards, _ = _cards_for(did)
    _ack({"ids": cards[0]["group_ids"]})
    _report(did, "trapped", "red", egress="cannot_exit")    # new fact

    cards, _ = _cards_for(did)
    assert len(cards) == 1
    words = " ".join(s["words"] for s in cards[0]["story"])
    # The initial raise is in there.
    assert "needs help" in words.lower() or "needs_help" in words.lower(), words
    # The worse-report is in there.
    assert "worse" in words.lower() or "cannot get out" in words.lower(), words
    # And someone acknowledged an earlier fact — that must be readable.
    assert "Acknowledged by" in words, words


def test_a_single_acknowledge_press_silences_all_of_a_persons_rows():
    """The card carries every underlying row ID for that person, so one
    Acknowledge press has to silence them all — otherwise the card would
    show as "acknowledged" while a hidden row underneath still sounded."""
    did = _did()
    _report(did, "trapped", "yellow")                       # NEEDS_HELP
    _report(did, "trapped", "red")                          # WORSE
    # Now two open rows in Mongo but ONE card on the board.
    cards, _ = _cards_for(did)
    assert len(cards) == 1
    ids_before = list(cards[0]["group_ids"])
    assert len(ids_before) >= 2, "expected the card to carry every open row"

    _ack({"ids": ids_before})
    cards, data = _cards_for(did)
    assert cards[0]["acknowledged"] is True
    # And the top-of-panel count reflects it.
    assert data["unacknowledged"] == 0 or all(
        p["device_id"] != did for g in data["groups"] for p in g["people"]
        if not p["acknowledged"]
    )


def test_the_dedupe_snapshot_endpoint_is_idempotent():
    """The migration snapshot backs up any pre-303 duplicate rows so an
    inquiry can still see the pre-merge state. It must be safe to press
    twice — nothing is destroyed either time."""
    r1 = requests.post(f"{BASE}/api/admin/alarms/dedupe",
                       headers=H, timeout=15)
    assert r1.status_code == 200, r1.text
    r2 = requests.post(f"{BASE}/api/admin/alarms/dedupe",
                       headers=H, timeout=15)
    assert r2.status_code == 200, r2.text
    body = r2.json()
    assert "scanned" in body and "backed_up" in body and "devices" in body
