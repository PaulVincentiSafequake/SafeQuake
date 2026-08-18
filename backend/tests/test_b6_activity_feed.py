"""B6 (2026-06-18) — activity feed grouped by person, in plain sentences.

Two halves:

* BACKEND — /api/audit must label C1 re-check rows with their own kinds.
  They live in the same `status_events` ledger as self-reports, and until now
  they came out labelled "status": an automatic re-check answer was
  indistinguishable from a person opening the app and reporting themselves,
  which is exactly the distinction an operator needs.
* DASHBOARD — static guards on memory/dashboard_build/index.html, the same
  approach as test_no_external_map_links.py. The feed must group by person,
  must never print a wire value (`not_responding`, `red`) at an operator, and
  every state must carry a shape and a word, never colour alone.
"""
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
import requests
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

BASE_URL = "http://localhost:8001"
ENV = {**dotenv_values(ROOT / "backend" / ".env"), **os.environ}
ADMIN_TOKEN = ENV["ADMIN_TRIGGER_PASSWORD"]
DASHBOARD = ROOT / "memory" / "dashboard_build" / "index.html"

HEADERS = {"X-Admin-Token": ADMIN_TOKEN}


# ─── backend: re-check rows keep their own kind in /api/audit ────────────
@pytest.fixture(scope="module")
def seeded_recheck():
    device_id = f"qg-test-b6-{uuid.uuid4().hex[:8]}"
    r = requests.post(f"{BASE_URL}/api/status", json={
        "device_id": device_id,
        "status": "trapped",
        "severity": "yellow",
        "mobility": "trapped",
        "display_name": "B6 Test",
    }, timeout=30)
    assert r.status_code == 200, r.text
    r = requests.post(f"{BASE_URL}/api/recheck/answer", json={
        "device_id": device_id,
        "answer": "much_worse",
        "battery_pct": 11,
    }, timeout=30)
    assert r.status_code == 200, r.text
    yield device_id
    # Clean up both collections — a stray trapped device skews every report.
    from pymongo import MongoClient
    c = MongoClient(ENV["MONGO_URL"])
    db = c[ENV.get("DB_NAME", "test_database")]
    db.device_status.delete_many({"device_id": device_id})
    db.status_events.delete_many({"device_id": device_id})


def _audit(**params):
    r = requests.get(f"{BASE_URL}/api/audit", headers=HEADERS,
                     params={"limit": 500, **params}, timeout=60)
    assert r.status_code == 200, r.text
    return r.json()["events"]


def test_recheck_answer_has_its_own_kind(seeded_recheck):
    mine = [e for e in _audit() if e.get("device_id") == seeded_recheck]
    kinds = {e.get("kind") for e in mine}
    assert "recheck_answered" in kinds, (
        f"re-check answer not labelled as such — kinds seen: {kinds}"
    )
    answered = next(e for e in mine if e["kind"] == "recheck_answered")
    assert answered["answer"] == "much_worse"
    # Tap time is what every human surface reads.
    assert answered.get("answered_at"), "answered_at missing from the feed row"
    assert answered.get("deteriorating") is True
    assert answered.get("severity") == "red", "much_worse must reach red"


def test_self_report_is_still_a_status_row(seeded_recheck):
    mine = [e for e in _audit() if e.get("device_id") == seeded_recheck]
    statuses = [e for e in mine if e["kind"] == "status"]
    assert statuses, "the original self-report vanished from the feed"
    assert all(e.get("answer") is None for e in statuses)


def test_no_recheck_row_masquerades_as_a_self_report():
    for e in _audit():
        if e.get("answer") is not None or e.get("check_id"):
            assert str(e.get("kind", "")).startswith("recheck"), (
                f"row carrying re-check fields labelled {e.get('kind')!r}"
            )


# ─── dashboard: person grouping, plain words, shape + word + colour ─────
@pytest.fixture(scope="module")
def dash():
    return DASHBOARD.read_text()


def test_feed_groups_by_person(dash):
    for needle in ("renderPersonFeed", "groupByPerson", "qg-person-row",
                   'id="qg-feed-person"', 'id="qg-feed-event"'):
        assert needle in dash, f"B6 person-grouped feed missing {needle!r}"


def test_person_view_is_the_default(dash):
    assert 'var feedMode = "person"' in dash
    assert 'id="qg-feed-person" aria-pressed="true"' in dash


def test_every_state_has_a_word_and_a_shape(dash):
    # stateOf() is the single source of the feed's vocabulary: each branch
    # must return BOTH a word and a shape, so colour is never alone.
    start = dash.index("function stateOf(")
    block = dash[start:dash.index("function stateChip(")]
    returns = [line for line in block.split("\n") if "return {" in line]
    assert len(returns) >= 6, "stateOf lost a state"
    for line in returns:
        assert "word:" in line and "shape:" in line, f"colour-only state: {line.strip()}"


def test_wire_values_are_never_printed_at_an_operator(dash):
    start = dash.index("function eventSentence(")
    block = dash[start:dash.index("function feedAgoWords(")]
    # The sentences are English, not enum values.
    assert "Recorded as not responding." in block
    assert "Told us they need help" in block
    assert "Checked in safe." in block
    # And the old raw-status renderer is gone.
    assert '" → <b>" + esc(e.status' not in dash
    assert "+ esc(e.severity) + \"</span>\"" not in dash


def test_recheck_answers_read_as_english(dash):
    for phrase in ("no change since last time", "getting worse",
                   "much worse — urgent", "We asked how they are"):
        assert phrase in dash, f"missing plain wording for {phrase!r}"


def test_history_modal_is_shared_not_duplicated(dash):
    assert "window.qgOpenHistory = openHistoryModal;" in dash
    assert 'data-qg-history=' in dash


def test_feed_never_links_coordinates_out(dash):
    # Same GDPR rule as everywhere else: our own map, never a third party.
    start = dash.index("function personRow(")
    block = dash[start:dash.index("function groupByPerson(")]
    assert "google.com/maps" not in block
    assert "data-qg-map-lat" in block
