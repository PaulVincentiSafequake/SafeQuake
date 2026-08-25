"""#304 — story is time-ordered and deduped, and the card face never
lets a "gone quiet" event push a live IMMEDIATE report off the top.

Paul, 2026-08-26 (live re-test of #303):
  > The merged "What happened" history is not in time order. It reads
  > 09:53, then 12:29, then 09:55, then 11:35, then 12:29 again, then
  > 12:23, then 12:45. The 12:29 report appears twice.
  >
  > The card's current headline says "GONE QUIET" from 12:45, but the
  > most recent real fact is more recent than that in relevance terms —
  > an "immediate, cannot move" report at 12:29 — and the "gone quiet"
  > event was acknowledged in the same second it was raised.

Device IDs here are all `qgtest-<random>` — no resemblance to any real
short code, per the standing rule Paul asked for.

Run: python -m pytest backend/tests/test_304_story_and_primary.py -q
"""
import os
import time
import uuid
from datetime import datetime, timezone, timedelta

import pytest
import requests
from dotenv import dotenv_values

BASE = os.environ.get("QG_BASE", "http://localhost:8001")
TOKEN = (dotenv_values("/app/backend/.env").get("ADMIN_TRIGGER_PASSWORD")
         or os.environ.get("ADMIN_TRIGGER_PASSWORD", ""))
H = {"X-Admin-Token": TOKEN, "Content-Type": "application/json"}


def _tdid():
    """Deliberately-fake test device_id, no resemblance to real codes."""
    return f"qgtest-{uuid.uuid4().hex[:10]}"


def _report(did, status, severity=None, egress=None, name="qgtest"):
    body = {"device_id": did, "status": status, "display_name": name}
    if severity:
        body["severity"] = severity
    if egress:
        body["egress"] = egress
    r = requests.post(f"{BASE}/api/status", json=body, timeout=15)
    assert r.status_code == 200, r.text


def _card_for(did):
    r = requests.get(f"{BASE}/api/admin/alarms?include_test=1",
                     headers=H, timeout=15)
    d = r.json()
    for g in d["groups"]:
        for p in g["people"]:
            if p["device_id"] == did:
                return {**p, "group_ids": g["ids"], "kind": g["kind"],
                        "group_word": g["word"], "group_headline": g["headline"]}
    return None


def _ack(ids):
    r = requests.post(f"{BASE}/api/admin/alarms/ack",
                      headers=H, json={"ids": ids}, timeout=15)
    assert r.status_code == 200, r.text


def test_story_is_strictly_time_ordered():
    """The story must read left-to-right in time. If a step has an
    `at`, every step after it with an `at` must be no earlier."""
    did = _tdid()
    _report(did, "trapped", "yellow")            # NEEDS_HELP
    time.sleep(0.05)
    _report(did, "trapped", "red")               # WORSE
    card = _card_for(did)
    _ack(card["group_ids"])
    time.sleep(0.05)
    _report(did, "trapped", "red", egress="cannot_exit")  # new fact re-opens

    card = _card_for(did)
    ats = [s["at"] for s in card["story"] if s.get("at")]
    assert ats == sorted(ats), (
        "story steps must be in time order:\n"
        + "\n".join(f"  {s.get('at')} — {s['words']}" for s in card["story"])
    )


def test_story_has_no_duplicates():
    """The same (at, words) pair must not appear twice — a `since_report`
    stamped on two sibling rows must merge into one step."""
    did = _tdid()
    _report(did, "trapped", "yellow")
    _report(did, "trapped", "red")
    card = _card_for(did)
    _ack(card["group_ids"])
    _report(did, "trapped", "red", egress="cannot_exit")

    card = _card_for(did)
    keys = [(s.get("at"), s.get("words")) for s in card["story"]]
    assert len(keys) == len(set(keys)), (
        "story has duplicate steps:\n"
        + "\n".join(f"  {a} — {w}" for a, w in keys)
    )


def test_not_acknowledged_placeholder_only_appears_when_actually_unacked():
    """When the card is fully acknowledged, the trailing 'Not
    acknowledged by anybody yet.' step must be gone — otherwise the
    story contradicts the card face."""
    did = _tdid()
    _report(did, "trapped", "red")
    card = _card_for(did)
    _ack(card["group_ids"])
    card = _card_for(did)
    assert card["acknowledged"] is True
    words = [s["words"] for s in card["story"]]
    assert not any("Not acknowledged by anybody yet." in w for w in words), words


def test_immediate_report_beats_gone_quiet_for_the_card_face():
    """Direct reproduction of #304's second half. A person reports
    IMMEDIATE, then the silence sweep raises GONE_QUIET on them. The
    card face must still show the IMMEDIATE report, not the gone-quiet
    line. The gone-quiet fact goes into the 'since' note and the story
    so nothing is hidden."""
    from motor.motor_asyncio import AsyncIOMotorClient
    import asyncio

    did = _tdid()
    _report(did, "trapped", "red", egress="cannot_exit")

    # Manually insert a GONE_QUIET row for this device — the sweep would
    # do this in production, but we don't want to wait for it here.
    async def _insert_quiet():
        mongo_url = dotenv_values("/app/backend/.env").get("MONGO_URL")
        client = AsyncIOMotorClient(mongo_url)
        db = client[dotenv_values("/app/backend/.env").get("DB_NAME", "test_database")]
        # Use board_alarms.raise_alarm so the row looks identical to a real one.
        import sys
        sys.path.insert(0, "/app/backend")
        import board_alarms
        n_later = datetime.now(timezone.utc) + timedelta(seconds=30)
        await board_alarms.raise_alarm(
            db, kind=board_alarms.GONE_QUIET, device_id=did,
            row={"device_id": did, "short_code": did[-5:].upper(),
                 "display_name": "qgtest"},
            headline=f"qgtest · {did[-5:].upper()} has gone quiet",
            action="Simulated sweep for the test.",
            now=n_later,
        )
        client.close()

    asyncio.get_event_loop().run_until_complete(_insert_quiet())

    card = _card_for(did)
    assert card is not None, "card should exist for the person"
    # Card face is the IMMEDIATE report, not the gone-quiet line.
    assert card["kind"] in ("needs_help", "worse"), card
    assert "gone quiet" not in (card["group_headline"] or "").lower(), card["group_headline"]
    # The gone-quiet fact is still visible, in the since-note or the story.
    since_words = ((card.get("since_report") or {}).get("words") or "").lower()
    story_words = " ".join(s["words"] for s in card["story"]).lower()
    assert "gone quiet" in since_words or "gone quiet" in story_words, (
        "gone-quiet fact must be visible somewhere on the card"
    )


def test_gone_quiet_alone_still_becomes_the_card_face():
    """If a person has ONLY a GONE_QUIET row open (nothing report-driven),
    the card face still shows GONE_QUIET — the rule is 'prefer
    report-driven when a choice exists', not 'never show GONE_QUIET'."""
    from motor.motor_asyncio import AsyncIOMotorClient
    import asyncio

    did = _tdid()

    async def _insert_only_quiet():
        mongo_url = dotenv_values("/app/backend/.env").get("MONGO_URL")
        client = AsyncIOMotorClient(mongo_url)
        db = client[dotenv_values("/app/backend/.env").get("DB_NAME", "test_database")]
        import sys
        sys.path.insert(0, "/app/backend")
        import board_alarms
        await board_alarms.raise_alarm(
            db, kind=board_alarms.GONE_QUIET, device_id=did,
            row={"device_id": did, "short_code": did[-5:].upper(),
                 "display_name": "qgtest"},
            headline=f"qgtest · {did[-5:].upper()} has gone quiet",
            action="Simulated sweep for the test.",
        )
        client.close()

    asyncio.get_event_loop().run_until_complete(_insert_only_quiet())

    card = _card_for(did)
    assert card is not None
    assert card["kind"] == "gone_quiet", card
