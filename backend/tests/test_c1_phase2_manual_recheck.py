"""C1 phase 2 — operator-initiated re-check (POST /api/admin/recheck).

The rules this file exists to hold in place:

* The first press is a PREVIEW and sends nothing. Pressing "ask now" wakes
  injured people's phones, so the cost has to be in front of the operator
  before it happens, not after.
* Two refusals are not overridable, because the Critical Alerts entitlement
  rests on them: someone whose current status is not `trapped` is never
  asked, and a dark phone is never asked (it cannot answer). There is
  deliberately no flag to force either.
* A human asking is recorded as a human asking — `initiated_by` on the
  ledger row plus a `recheck_audit` entry.
"""
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import requests
from dotenv import dotenv_values
from pymongo import MongoClient

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

BASE_URL = "http://localhost:8001"
ENV = {**dotenv_values(ROOT / "backend" / ".env"), **os.environ}
ADMIN = {"X-Admin-Token": ENV["ADMIN_TRIGGER_PASSWORD"]}

mongo = MongoClient(ENV["MONGO_URL"])
mdb = mongo[ENV.get("DB_NAME", "test_database")]


def _iso(dt):
    return dt.isoformat()


@pytest.fixture()
def trapped_device():
    did = f"qg-test-c1p2-{uuid.uuid4().hex[:8]}"
    now = datetime.now(timezone.utc)
    r = requests.post(f"{BASE_URL}/api/status", json={
        "device_id": did, "status": "trapped", "severity": "yellow",
        "mobility": "trapped", "display_name": "Phase2 Test", "battery_pct": 8,
    }, timeout=30)
    assert r.status_code == 200, r.text
    # Fresh contact so silence_state() is not `dark`.
    mdb.device_status.update_one({"device_id": did}, {"$set": {
        "updated_at": _iso(now), "trapped_since": _iso(now - timedelta(minutes=20)),
        "battery_pct": 8,
    }})
    yield did
    mdb.device_status.delete_many({"device_id": did})
    mdb.status_events.delete_many({"device_id": did})
    mdb.recheck_audit.delete_many({"device_ids": did})


def _post(payload):
    return requests.post(f"{BASE_URL}/api/admin/recheck", headers=ADMIN,
                         json=payload, timeout=60)


def test_preview_sends_nothing(trapped_device):
    before = mdb.status_events.count_documents(
        {"device_id": trapped_device, "kind": "recheck_sent"})
    r = _post({"device_ids": [trapped_device], "confirm": False})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["preview"] is True
    assert d["asked"] == 0
    assert d["would_ask"] == 1
    after = mdb.status_events.count_documents(
        {"device_id": trapped_device, "kind": "recheck_sent"})
    assert after == before, "the preview woke someone — it must never send"


def test_preview_states_the_cost_in_plain_words(trapped_device):
    d = _post({"device_ids": [trapped_device], "confirm": False}).json()
    lines = " ".join(d["cost"]["lines"])
    assert "wake 1 phone" in lines
    # 8% battery must be called out — it is the whole reason for the confirm.
    assert d["cost"]["critical_battery"] == 1
    assert "less than 10% battery" in lines
    # No invented percentage cost per prompt.
    assert "% of" not in lines


def test_confirm_asks_and_reschedules(trapped_device):
    r = _post({"device_ids": [trapped_device], "confirm": True,
               "reason": "coordinator requested a sweep"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["preview"] is False
    assert d["asked"] == 1
    row = mdb.device_status.find_one({"device_id": trapped_device})
    rc = row.get("recheck") or {}
    assert rc.get("last_check_at"), "manual ask did not record a check"
    assert rc.get("next_check_at"), "manual ask left the ladder unscheduled"
    assert int(rc.get("checks_sent") or 0) >= 1
    audit = mdb.recheck_audit.find_one({"device_ids": trapped_device})
    assert audit, "no audit row for an operator-initiated ask"
    assert audit["initiated_by"]
    assert audit["reason"] == "coordinator requested a sweep"


def test_a_safe_person_is_never_asked():
    did = f"qg-test-c1p2-safe-{uuid.uuid4().hex[:6]}"
    requests.post(f"{BASE_URL}/api/status",
                  json={"device_id": did, "status": "safe"}, timeout=30)
    try:
        d = _post({"device_ids": [did], "confirm": True}).json()
        assert d["asked"] == 0
        reasons = [s["reason"] for s in d["skipped"]]
        assert any("no longer" in r or "not currently marked" in r for r in reasons), reasons
        assert mdb.status_events.count_documents(
            {"device_id": did, "kind": "recheck_sent"}) == 0
    finally:
        mdb.device_status.delete_many({"device_id": did})
        mdb.status_events.delete_many({"device_id": did})


def test_a_dark_phone_is_never_asked():
    did = f"qg-test-c1p2-dark-{uuid.uuid4().hex[:6]}"
    requests.post(f"{BASE_URL}/api/status", json={
        "device_id": did, "status": "trapped", "severity": "red"}, timeout=30)
    old = datetime.now(timezone.utc) - timedelta(hours=3)
    mdb.device_status.update_one({"device_id": did},
                                 {"$set": {"updated_at": _iso(old)}})
    try:
        d = _post({"device_ids": [did], "confirm": True}).json()
        assert d["asked"] == 0
        assert any("dark" in s["reason"] for s in d["skipped"]), d["skipped"]
    finally:
        mdb.device_status.delete_many({"device_id": did})
        mdb.status_events.delete_many({"device_id": did})


def test_severity_filter_targets_one_band(trapped_device):
    # The device is yellow: a red-only ask must leave it alone.
    d = _post({"severity": "red", "device_ids": [trapped_device],
               "confirm": False}).json()
    assert d["would_ask"] == 0
    d2 = _post({"severity": "yellow", "device_ids": [trapped_device],
                "confirm": False}).json()
    assert d2["would_ask"] == 1


def test_requires_operator_auth(trapped_device):
    r = requests.post(f"{BASE_URL}/api/admin/recheck",
                      json={"device_ids": [trapped_device], "confirm": True},
                      timeout=30)
    assert r.status_code in (401, 403), r.status_code
    r2 = requests.post(f"{BASE_URL}/api/admin/recheck",
                       headers={"X-Admin-Token": "wrong"},
                       json={"confirm": True}, timeout=30)
    assert r2.status_code in (401, 403), r2.status_code


def test_broadcast_preview_does_not_list_every_safe_device():
    """A skip list with every safe phone in the database buries the two
    entries that matter."""
    d = _post({"confirm": False}).json()
    reasons = [s["reason"] for s in d["skipped"]]
    assert not any("not currently marked" in r for r in reasons), (
        "broadcast preview is reporting non-trapped devices as skipped"
    )


# ─── the dashboard panel (static guards) ────────────────────────────────
DASH = (ROOT / "memory" / "dashboard_build" / "index.html").read_text()


def test_panel_exists_and_is_two_step():
    assert 'id="qg-recheck"' in DASH
    assert "Ask everyone how they are now" in DASH
    assert 'JSON.stringify({ confirm: false })' in DASH, "no preview step"
    assert 'JSON.stringify({ confirm: true })' in DASH, "no confirm step"
    assert "Yes — ask them now" in DASH


def test_panel_shows_the_cost_before_sending():
    start = DASH.index('askBtn.addEventListener')
    block = DASH[start:start + 4000]
    assert "cost.lines" in block, "confirm dialog does not show the cost lines"
    assert block.index("confirm: false") < block.index("confirm: true"), (
        "the panel sends before it previews"
    )


def test_panel_explains_a_paused_ladder():
    assert "Automatic checks are PAUSED — nobody is being asked." in DASH
    assert "Only an admin can pause or resume automatic checks." in DASH
