"""#286 — acknowledge all, for the day there are forty-seven of them.

Paul, 2026-08-24 (live test batch):
  > At mass-casualty scale, clicking fifty individual acknowledge buttons
  > is unusable.

`POST /api/admin/alarms/ack {"all": true}` acknowledges every alarm that
is currently open and unacknowledged. What it must NOT do is any kind of
resolving: acknowledging stops the sound and the flashing, and that is
all. Every alarm stays on the board, and every row keeps a name and a
time against it so a bulk acknowledgement reads back in an inquiry
exactly like fifty individual ones would.

Run: python -m pytest backend/tests/test_bulk_ack_286.py -q
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


def _real_id():
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


def _alarms():
    r = requests.get(f"{BASE}/api/admin/alarms", headers=H, timeout=15)
    assert r.status_code == 200, r.text
    return r.json()


def _ack_all():
    r = requests.post(f"{BASE}/api/admin/alarms/ack", json={"all": True},
                      headers=H, timeout=20)
    assert r.status_code == 200, r.text
    return r.json()


def _rows_for(data, device_ids):
    out = []
    for g in data.get("groups", []):
        for p in g.get("people", []):
            if p.get("device_id") in device_ids:
                out.append(p)
    return out


def test_acknowledge_all_silences_every_open_alarm():
    ids = [_real_id() for _ in range(5)]
    for i, did in enumerate(ids):
        _post_status(did, "trapped", severity="red", egress="cannot_exit",
                     name=f"Bulk{i}")
    before = _alarms()
    assert before["unacknowledged"] >= 5
    mine = _rows_for(before, set(ids))
    assert len(mine) == 5
    assert all(p["acknowledged"] is False for p in mine)

    result = _ack_all()
    assert result["acknowledged"] >= 5
    assert result["acknowledged_by"]

    after = _alarms()
    assert after["unacknowledged"] == 0, after["unacknowledged"]
    mine_after = _rows_for(after, set(ids))
    assert len(mine_after) == 5
    assert all(p["acknowledged"] is True for p in mine_after)


def test_acknowledging_everything_resolves_nothing():
    """The whole point of the ISA-18.1 sequence: acknowledging is not
    resolving. Every person is still on the board afterwards."""
    ids = [_real_id() for _ in range(3)]
    for i, did in enumerate(ids):
        _post_status(did, "trapped", severity="yellow", name=f"Stay{i}")
    _ack_all()
    after = _alarms()
    assert after["open"] >= 3
    assert len(_rows_for(after, set(ids))) == 3


def test_every_bulk_acknowledged_row_records_who_and_when():
    ids = [_real_id() for _ in range(3)]
    for i, did in enumerate(ids):
        _post_status(did, "trapped", severity="red", name=f"Named{i}")
    _ack_all()
    for p in _rows_for(_alarms(), set(ids)):
        assert p["ack_by"], p
        assert p["ack_at"], p


def test_acknowledge_all_when_there_is_nothing_to_do_is_not_an_error():
    _ack_all()                      # clear whatever is open
    second = _ack_all()             # and again, on an empty board
    assert second["acknowledged"] == 0
    assert _alarms()["unacknowledged"] == 0


def test_a_new_alarm_after_a_bulk_acknowledge_sounds_again():
    """Acknowledging everything must not deafen the board. The next person
    to ask for help arrives unacknowledged, like any other."""
    _ack_all()
    did = _real_id()
    _post_status(did, "trapped", severity="red", name="AfterBulk")
    data = _alarms()
    assert data["unacknowledged"] >= 1
    mine = _rows_for(data, {did})
    assert mine and mine[0]["acknowledged"] is False


def test_acknowledge_all_requires_authentication():
    r = requests.post(f"{BASE}/api/admin/alarms/ack", json={"all": True},
                      timeout=15)
    assert r.status_code in (401, 403), r.text


def test_an_empty_payload_is_still_refused():
    """`all` must be explicit. An empty body acknowledging the whole board
    would be a very expensive typo."""
    r = requests.post(f"{BASE}/api/admin/alarms/ack", json={},
                      headers=H, timeout=15)
    assert r.status_code == 400, r.text
