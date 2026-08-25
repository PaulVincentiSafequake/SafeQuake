"""#301 — test people behave like real reports, labelled, and only when asked.

Paul, 2026-08-25 (live test):
  > Tried using "Add 33 test people" to get multiple simultaneous alarms
  > for bulk-acknowledge testing. They appeared as markers on the map,
  > but didn't change any stat box, didn't appear in the alarm panel at
  > all, and aren't even clickable for details. This defeats the whole
  > purpose of test people.

His ruling: identical everywhere — counts, list, map, alarms — but only
while "Show test entries" is ticked, and always labelled.

So the alarm panel now takes `include_test`, mirroring that tick:
  * off  → an operator sees only real casualties (the default);
  * on   → the rehearsal appears too, flagged `is_test`, with the same
           shapes, sounds and buttons, so the panel can be practised on.

Run: python -m pytest backend/tests/test_test_people_alarms_301.py -q
"""
import os

import pytest
import requests
from dotenv import dotenv_values

BASE = os.environ.get("QG_BASE", "http://localhost:8001")
TOKEN = (dotenv_values("/app/backend/.env").get("ADMIN_TRIGGER_PASSWORD")
         or os.environ.get("ADMIN_TRIGGER_PASSWORD", ""))
H = {"X-Admin-Token": TOKEN, "Content-Type": "application/json"}


@pytest.fixture()
def seeded():
    r = requests.post(f"{BASE}/api/admin/test-people/seed", headers=H, timeout=30)
    assert r.status_code == 200, r.text
    yield r.json()
    requests.post(f"{BASE}/api/admin/test-people/clear", headers=H, timeout=30)


def _alarms(include_test=False):
    url = f"{BASE}/api/admin/alarms" + ("?include_test=1" if include_test else "")
    r = requests.get(url, headers=H, timeout=20)
    assert r.status_code == 200, r.text
    return r.json()


def test_seeding_test_people_raises_alarms(seeded):
    """The whole point: you can now rehearse the alarm panel."""
    assert seeded["seeded"] == 33
    assert seeded["alarms_raised"] > 0, seeded
    with_test = _alarms(include_test=True)
    mine = [p for g in with_test["groups"] for p in g["people"]
            if str(p["device_id"]).startswith("qg-seeded-33")]
    # At least one alarm per trapped test person; the silence sweep adds
    # "gone quiet" alarms for the ones who asked for help and then stopped
    # answering, exactly as it would for real people.
    assert len(mine) >= seeded["alarms_raised"], len(mine)


def test_every_test_alarm_is_labelled(seeded):
    """Nothing labelled TEST can ever be mistaken for a real casualty."""
    with_test = _alarms(include_test=True)
    for g in with_test["groups"]:
        for p in g["people"]:
            if str(p["device_id"]).startswith("qg-seeded-33"):
                assert p["is_test"] is True, p
                assert "TEST" in str(p["who"]).upper(), p


def test_test_alarms_are_hidden_unless_asked_for(seeded):
    """Default view is real casualties only — an operator must never be
    pulled out of a real incident by a rehearsal they did not ask to see."""
    real_only = _alarms(include_test=False)
    ids = [p["device_id"] for g in real_only["groups"] for p in g["people"]]
    assert not [d for d in ids if str(d).startswith("qg-seeded-33")], ids[:5]
    with_test = _alarms(include_test=True)
    assert with_test["open"] > real_only["open"]
    assert with_test["unacknowledged"] > real_only["unacknowledged"]


def test_acknowledge_all_respects_what_the_operator_can_see(seeded):
    """Acknowledge-all must not silence alarms the operator was never
    shown, and must include the rehearsal when the rehearsal is on show."""
    before_real = _alarms(False)["unacknowledged"]
    assert before_real >= 0
    r = requests.post(f"{BASE}/api/admin/alarms/ack", json={"all": True},
                      headers=H, timeout=30)
    assert r.status_code == 200, r.text
    assert _alarms(False)["unacknowledged"] == 0
    # The rehearsal is untouched, because it was hidden.
    assert _alarms(True)["unacknowledged"] > 0

    r = requests.post(f"{BASE}/api/admin/alarms/ack",
                      json={"all": True, "include_test": True},
                      headers=H, timeout=30)
    assert r.status_code == 200, r.text
    assert _alarms(True)["unacknowledged"] == 0


def test_clearing_test_people_clears_their_alarms(seeded):
    """A rehearsal must not leave alarms behind on a live board."""
    assert _alarms(True)["open"] > _alarms(False)["open"]
    r = requests.post(f"{BASE}/api/admin/test-people/clear", headers=H, timeout=30)
    assert r.status_code == 200, r.text
    assert r.json()["removed"] == 33
    after = _alarms(True)
    left = [p for g in after["groups"] for p in g["people"]
            if str(p["device_id"]).startswith("qg-seeded-33")]
    assert left == [], left


def test_test_people_change_the_numbers_they_are_included_in(seeded):
    """Paul: "didn't change any stat box". The board reads counts with and
    without test entries from the API; both must exist and differ."""
    r = requests.get(f"{BASE}/api/devices", headers=H, timeout=30)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["counts"]["total"] > d["counts_without_test"]["total"]
    assert d["counts"]["trapped"] > d["counts_without_test"]["trapped"]
    # And each seeded row is on the working board, flagged, so the list and
    # the map can show it with a TEST label and open its details.
    seeded_rows = [x for x in d["devices"]
                   if str(x["device_id"]).startswith("qg-seeded-33")]
    assert len(seeded_rows) == 33
    assert all(x["is_test"] is True for x in seeded_rows)
    assert all(x["short_code"] for x in seeded_rows)
    assert all(x["latitude"] and x["longitude"] for x in seeded_rows)
