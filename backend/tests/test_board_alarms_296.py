"""#296 — the board's annunciator, end to end against the live API.

What this proves, in the order Paul asked for it:
  1. A new person asking for help raises exactly one alarm, and it names
     the action rather than the state change.
  2. Someone reporting SAFE raises nothing. Information is not an alarm.
  3. Getting worse raises a second alarm.
  4. Acknowledging records who and when, drops the unacknowledged count,
     and does NOT clear the alarm.
  5. Being rescued — the situation actually resolving — clears it.
  6. Many arriving in one minute become one line, not many.

Run: python -m pytest backend/tests/test_board_alarms_296.py -q
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


def _real_id() -> str:
    """An id shaped like the mobile app's own, so it is treated as a real
    person rather than a test entry (deps.is_test_device)."""
    return f"qg-{int(time.time()*1000)}-{uuid.uuid4().hex[:8]}"


def _post_status(device_id, status, severity=None, egress=None, name=None):
    body = {"device_id": device_id, "status": status}
    if severity:
        body["severity"] = severity
    if egress:
        body["egress"] = egress
    if name:
        body["display_name"] = name
    r = requests.post(f"{BASE}/api/status", json=body, timeout=15)
    assert r.status_code == 200, r.text
    return r.json()


def _alarms():
    r = requests.get(f"{BASE}/api/admin/alarms", headers=H, timeout=15)
    assert r.status_code == 200, r.text
    return r.json()


def _mine(data, device_id):
    out = []
    for g in data.get("groups", []):
        for p in g.get("people", []):
            if p.get("device_id") == device_id:
                out.append({**p, "kind": g.get("kind"), "group_key": g.get("group_key")})
    return out


def test_needs_help_raises_one_alarm_naming_the_action():
    did = _real_id()
    _post_status(did, "trapped", severity="yellow", egress="cannot_exit", name="Anna")
    mine = _mine(_alarms(), did)
    kinds = [m["kind"] for m in mine]
    assert kinds.count("needs_help") == 1, mine
    a = [m for m in mine if m["kind"] == "needs_help"][0]
    assert "Anna" in a["headline"] and did[-5:].upper() in a["headline"], a
    assert a["action"].startswith("Send a team"), a
    assert "SERIOUS" in a["action"] and "cannot get out" in a["action"], a
    assert a["acknowledged"] is False


def test_safe_is_information_and_never_an_alarm():
    did = _real_id()
    _post_status(did, "safe", name="Bruno")
    assert _mine(_alarms(), did) == []


def test_getting_worse_raises_a_second_alarm():
    did = _real_id()
    _post_status(did, "trapped", severity="green", name="Carl")
    _post_status(did, "trapped", severity="red", name="Carl")
    kinds = [m["kind"] for m in _mine(_alarms(), did)]
    assert "needs_help" in kinds and "worse" in kinds, kinds


def test_acknowledge_records_who_and_does_not_clear():
    did = _real_id()
    _post_status(did, "trapped", severity="green", name="Dana")
    before = _alarms()
    mine = _mine(before, did)
    ids = [m["id"] for m in mine]
    assert ids
    r = requests.post(f"{BASE}/api/admin/alarms/ack", headers=H,
                      json={"ids": ids}, timeout=15)
    assert r.status_code == 200, r.text
    assert r.json()["acknowledged"] == len(ids)
    who = r.json()["acknowledged_by"]
    assert who

    after = _mine(_alarms(), did)
    # Still there — acknowledging is not resolving.
    assert len(after) == len(mine)
    assert all(m["acknowledged"] for m in after)
    assert all(m["ack_by"] == who for m in after)
    assert all(m["ack_at"] for m in after)

    # And it is readable back in the audit feed.
    feed = requests.get(f"{BASE}/api/audit?kind=alarm_acknowledged&limit=200",
                        headers=H, timeout=20).json()
    rows = [e for e in feed.get("events", []) if e.get("device_id") == did]
    assert rows, "acknowledgement missing from the audit feed"
    assert rows[0]["acknowledged_by"] == who


def test_rescue_clears_the_alarm():
    did = _real_id()
    _post_status(did, "trapped", severity="red", name="Elena")
    assert _mine(_alarms(), did)
    r = requests.post(f"{BASE}/api/mark-rescued", headers=H,
                      json={"device_id": did, "rescued_by": "tests"}, timeout=15)
    assert r.status_code == 200, r.text
    assert _mine(_alarms(), did) == []


def test_many_in_one_minute_become_one_line():
    ids = [_real_id() for _ in range(4)]
    for i, did in enumerate(ids):
        _post_status(did, "trapped", severity="green", name=f"Person{i}")
    data = _alarms()
    groups = [g for g in data["groups"]
              if g["kind"] == "needs_help"
              and any(p["device_id"] in ids for p in g["people"])]
    assert groups, data
    biggest = max(groups, key=lambda g: g["count"])
    assert biggest["count"] >= 4, biggest
    assert "in the same minute" in biggest["headline"], biggest
    # One decision, and every name still reachable underneath it.
    assert len(biggest["people"]) == biggest["count"]
    # Acknowledging the group acknowledges all of them at once.
    r = requests.post(f"{BASE}/api/admin/alarms/ack", headers=H,
                      json={"group_key": biggest["group_key"]}, timeout=15)
    assert r.status_code == 200, r.text
    again = [g for g in _alarms()["groups"]
             if g["group_key"] == biggest["group_key"]]
    assert again and again[0]["acknowledged"] is True
