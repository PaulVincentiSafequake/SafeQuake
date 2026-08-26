"""Batch 2026-08-26 — 6 items, verified with `qgtest-*` device IDs on
preview only. No writes touch anything that could be confused for a
real record.

Run: python -m pytest backend/tests/test_batch_2026_08_26.py -q
"""
import asyncio
import os
import time
import uuid
from datetime import datetime, timezone

import pytest
import requests
from dotenv import dotenv_values

BASE = os.environ.get("QG_BASE", "http://localhost:8001")
ENV = dotenv_values("/app/backend/.env")
TOKEN = ENV.get("ADMIN_TRIGGER_PASSWORD") or ""
H = {"X-Admin-Token": TOKEN, "Content-Type": "application/json"}


def _tdid():
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
                return {**p, "group_ids": g["ids"],
                        "group_headline": g["headline"],
                        "group_since": g.get("since_report") or {}}
    return None


def _audit_events_for(did):
    r = requests.get(f"{BASE}/api/audit?limit=200", headers=H, timeout=15)
    return [e for e in r.json()["events"] if e.get("device_id") == did]


# ── #306 — raw ISO timestamp must not leak into user sentences ────────
def test_306_no_raw_iso_in_since_note():
    """#306: 'Their phone has also gone quiet since 2026-08-26T08:16:...'
    was rendered on a card. A raw ISO string can never appear in a
    user-facing sentence. The at-timestamp travels on the `at` field
    the client formats itself."""
    from motor.motor_asyncio import AsyncIOMotorClient
    import sys
    sys.path.insert(0, "/app/backend")
    import board_alarms

    did = _tdid()
    _report(did, "trapped", "red")

    async def _insert_quiet():
        client = AsyncIOMotorClient(ENV.get("MONGO_URL"))
        db = client[ENV.get("DB_NAME", "test_database")]
        await board_alarms.raise_alarm(
            db, kind=board_alarms.GONE_QUIET, device_id=did,
            row={"device_id": did, "short_code": did[-5:].upper(),
                 "display_name": "qgtest"},
            headline=f"qgtest · {did[-5:].upper()} has gone quiet",
            action="Simulated silence sweep.",
        )
        client.close()
    asyncio.new_event_loop().run_until_complete(_insert_quiet())

    card = _card_for(did)
    since_words = (card.get("since_report") or {}).get("words") or ""
    # No T-separated ISO date can be in the sentence.
    assert "T" not in since_words or "+00:00" not in since_words, (
        f"Raw ISO timestamp leaked into sentence: {since_words!r}"
    )
    # And nothing that looks like a computer-format date fragment.
    for fragment in ("+00:00", "T08:", "T09:", "T10:", "T11:", "T12:"):
        assert fragment not in since_words, (
            f"Timestamp fragment {fragment!r} leaked: {since_words!r}"
        )
    # But the fact is still there.
    assert "gone quiet" in since_words.lower(), since_words


# ── #290 — activity feed status matches alarm panel status ────────────
def test_290_ack_event_carries_current_status():
    """#290: an alarm_acknowledged event now carries the person's
    CURRENT status/severity, so 'Recent activity → People' and the
    alarm panel read the same fact per person."""
    did = _tdid()
    _report(did, "trapped", "red")
    card = _card_for(did)
    requests.post(f"{BASE}/api/admin/alarms/ack",
                  headers=H, json={"ids": card["group_ids"]}, timeout=15)

    events = _audit_events_for(did)
    acked = [e for e in events if e["kind"] == "alarm_acknowledged"]
    assert acked, "expected an alarm_acknowledged event"
    e = acked[0]
    assert e.get("status") == "trapped", e
    assert e.get("severity") == "red", e


# ── #305 — banner wording matches acknowledge-vs-standdown behaviour ──
def test_305_banner_wording_separates_ack_from_stand_down():
    """#305: the banner used to say 'This lasts until you call the
    alert off, or 3 days pass' — which read as though pressing
    Acknowledge would end the alert. It now spells out that
    Acknowledge silences an alarm without ending the incident."""
    r = requests.get(f"{BASE}/api/admin/incident-status", headers=H, timeout=15)
    assert r.status_code == 200, r.text
    d = r.json()
    if not d.get("active"):
        pytest.skip("No live alert running — wording verified only when active")
    reason = d.get("reason") or ""
    assert "Acknowledging an alarm silences it" in reason, reason
    assert "does NOT call the alert off" in reason, reason


# ── #301 — seeded test people are actually visible in the counts ─────
def test_301_seeded_test_people_show_in_counts_and_alarms():
    """The seed endpoint has been correct all along. The failure Paul
    kept seeing was that 'Show test entries' was left off, so the
    counts and alarm panel filtered the newly-seeded rows straight
    back out. The fix is in the button handler (auto-tick + refresh).
    This test confirms the server side of the contract still holds:
    counts INCLUDE test people when include_test=True."""
    requests.post(f"{BASE}/api/admin/test-people/clear",
                  headers=H, timeout=15)
    r = requests.post(f"{BASE}/api/admin/test-people/seed",
                      headers=H, timeout=15)
    assert r.status_code == 200
    seeded = r.json()["seeded"]
    assert seeded == 33

    # counts WITH test people include them.
    r = requests.get(f"{BASE}/api/devices", headers=H, timeout=15)
    d = r.json()
    total_with = (d.get("counts") or {}).get("total") or 0
    total_without = (d.get("counts_without_test") or {}).get("total") or 0
    assert total_with >= total_without + 33, (total_with, total_without)

    # Alarm panel WITH include_test=1 shows the test rows.
    r = requests.get(f"{BASE}/api/admin/alarms?include_test=1",
                     headers=H, timeout=15)
    people = [p for g in r.json()["groups"] for p in g["people"]
              if p.get("is_test")]
    assert len(people) >= 12, len(people)  # 12 trapped rows in the spec

    # Clean up so we don't pollute subsequent tests with 33 stale rows.
    requests.post(f"{BASE}/api/admin/test-people/clear",
                  headers=H, timeout=15)


# ── #295 — sticky top strip caps its height so content below is reachable ──
def test_295_topstrip_has_max_height_cap():
    """CSS-level check that the sticky top strip and the alarm rows
    list both carry a max-height so a run of many alarms cannot push
    the working board below the viewport."""
    with open("/app/memory/dashboard_build/index.html", "r") as f:
        html = f.read()
    # The topstrip must cap and scroll.
    idx = html.find(".qg-topstrip {")
    assert idx > 0
    block = html[idx:idx + 800]
    assert "max-height:" in block, block[:400]
    assert "overflow-y: auto" in block, block[:400]


# ── #300 — the radius input has a legible text colour ────────────────
def test_300_radius_input_has_explicit_dark_color():
    """Regression guard for #300 — the number input on the amber
    radius-override card must set its own dark text colour so the
    dark theme's near-white default does not make typed text
    invisible on the cream background."""
    with open("/app/memory/dashboard_build/index.html", "r") as f:
        html = f.read()
    idx = html.find('.qa-pv-radius-row input[type="number"]')
    assert idx > 0
    block = html[idx:idx + 500]
    assert "color: #1B1005" in block or "color:#1B1005" in block, block
    assert "color-scheme: light" in block, block
