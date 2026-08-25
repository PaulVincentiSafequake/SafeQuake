"""#296 — calling an alert off must also call off the phones' own reminders.

Live finding (Paul, 2026-08-24): after triggering test alerts and standing
them down, one phone kept receiving CRITICAL "Are you safe?" notifications
roughly every 90 seconds. Those reminders are scheduled ON the device by
`scheduleCheckInReminders()`, so the only thing that could reach them was
the operator's separate kill-switch button — which nobody standing down an
alert has any reason to know they must also press.

The contract this locks in:
  * POST /api/admin/alert/stand-down sends the silent cancel-reminders push
    as part of the same action, to the SAME set of phones it stands down
    (#274: a person still asking for help keeps their screen and their
    reminders).
  * It says how many it reached, in the response and in the record.
"""
import os
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

import server
from server import STAND_DOWN_CONFIRMATION, app

ADMIN = os.environ["ADMIN_TRIGGER_PASSWORD"]
HDR = {"X-Admin-Token": ADMIN}


@pytest.fixture()
def client():
    return TestClient(app)


def _split(clearing, staying_real=0, staying_test=0, staying_people=None):
    """The shape `_stand_down_split()` returns, with #283's real/test split."""
    return {
        "clearing": clearing,
        "clearing_count": len(clearing),
        "clearing_real_count": len(clearing),
        "clearing_test_count": 0,
        "staying_count": staying_real + staying_test,
        "staying_real_count": staying_real,
        "staying_test_count": staying_test,
        "staying_people": staying_people or [],
    }


def _fake_events(devices, delivered=True):
    return {
        "payload": {"aps": {"content-available": 1}},
        "events": [
            {"user_id": d.get("user_id"), "delivered": delivered,
             "status_code": 200 if delivered else 400}
            for d in devices
        ],
    }


@pytest.fixture()
def two_clearable_phones():
    """Two phones that are NOT asking for help, so both get stood down."""
    ids = [f"TEST_sd296_{uuid.uuid4().hex[:6]}" for _ in range(2)]
    return [{"user_id": i, "device_token": "a" * 64} for i in ids]


class TestStandDownCancelsReminders:
    def test_the_cancel_push_goes_to_the_same_phones(self, client, two_clearable_phones):
        sent_to = {}

        async def _fake_stand_down(db, devices, **kw):
            sent_to["stand_down"] = [d["user_id"] for d in devices]
            return _fake_events(devices)

        async def _fake_cancel(**kw):
            devices = kw["devices"]
            sent_to["cancel"] = [d["user_id"] for d in devices]
            sent_to["reason"] = kw.get("reason")
            return _fake_events(devices)

        with patch("server._stand_down_split",
                   new=AsyncMock(return_value=_split(two_clearable_phones))), \
                patch("apns.send_stand_down", new=_fake_stand_down), \
                patch.object(server, "send_silent_cancel_reminders", new=_fake_cancel):
            r = client.post(
                "/api/admin/alert/stand-down", headers=HDR,
                json={"confirmation_phrase": STAND_DOWN_CONFIRMATION,
                      "reason": "false_alarm"},
            )

        assert r.status_code == 200, r.text
        assert sent_to["cancel"] == sent_to["stand_down"], (
            "the reminder cancel must reach exactly the phones that were "
            "stood down — no more (that would silence someone still asking "
            "for help) and no fewer (that leaves a phone nagging)"
        )
        assert sent_to["reason"] == "stand_down"
        assert r.json()["reminders_cancelled"] == 2

    def test_it_says_how_many_it_reached(self, client, two_clearable_phones):
        async def _fake_cancel(**kw):
            # One phone offline: one delivery, one not.
            devices = kw["devices"]
            return {
                "payload": {},
                "events": [
                    {"user_id": devices[0]["user_id"], "delivered": True},
                    {"user_id": devices[1]["user_id"], "delivered": False},
                ],
            }

        with patch("server._stand_down_split",
                   new=AsyncMock(return_value=_split(two_clearable_phones))), \
                patch("apns.send_stand_down",
                      new=AsyncMock(return_value=_fake_events(two_clearable_phones))), \
                patch.object(server, "send_silent_cancel_reminders", new=_fake_cancel):
            r = client.post(
                "/api/admin/alert/stand-down", headers=HDR,
                json={"confirmation_phrase": STAND_DOWN_CONFIRMATION},
            )

        assert r.status_code == 200, r.text
        assert r.json()["reminders_cancelled"] == 1, (
            "an undelivered cancel must not be reported as a cancelled "
            "reminder — the operator would believe a phone had stopped "
            "nagging when it had not"
        )

    def test_nobody_to_clear_means_no_cancel_push(self, client):
        called = {"cancel": False}

        async def _fake_cancel(**kw):
            called["cancel"] = True
            return {"payload": None, "events": []}

        with patch("server._stand_down_split", new=AsyncMock(return_value=_split(
                [], staying_real=1,
                staying_people=[{"short_code": "AB12C", "why": "asked for help"}]))), \
                patch.object(server, "send_silent_cancel_reminders", new=_fake_cancel):
            r = client.post(
                "/api/admin/alert/stand-down", headers=HDR,
                json={"confirmation_phrase": STAND_DOWN_CONFIRMATION},
            )

        assert r.status_code == 200, r.text
        assert called["cancel"] is False
        assert r.json()["reminders_cancelled"] == 0
        assert r.json()["kept_on_board_count"] == 1


class TestTheRecordSaysSo:
    def test_the_push_event_row_records_the_cancel(self, client, two_clearable_phones):
        with patch("server._stand_down_split",
                   new=AsyncMock(return_value=_split(two_clearable_phones))), \
                patch("apns.send_stand_down",
                      new=AsyncMock(return_value=_fake_events(two_clearable_phones))), \
                patch.object(server, "send_silent_cancel_reminders",
                             new=AsyncMock(
                                 return_value=_fake_events(two_clearable_phones))):
            r = client.post(
                "/api/admin/alert/stand-down", headers=HDR,
                json={"confirmation_phrase": STAND_DOWN_CONFIRMATION,
                      "reason": "record_check_296"},
            )
        assert r.status_code == 200, r.text

        import pymongo
        m = pymongo.MongoClient(os.environ["MONGO_URL"])
        db = m[os.environ.get("DB_NAME", "test_database")]
        row = db.push_events.find_one(
            {"kind": "alert_stood_down", "reason": "record_check_296"},
            sort=[("created_at", -1)],
        )
        m.close()
        assert row is not None
        assert row["reminders_cancelled"] == 2
        assert len(row["reminder_apns_events"]) == 2


class TestThePhoneAlsoStopsItself:
    """Belt and braces on the device.

    The backend now sends the cancel, but a phone that receives only the
    stand-down push (dropped cancel, app killed, whatever) must still stop
    nagging on its own. Static checks because notification behaviour cannot
    be exercised in Expo Go or on web — it needs a real build on a device.
    """
    @pytest.fixture(scope="class")
    def layout(self):
        with open("/app/frontend/app/_layout.tsx") as f:
            return f.read()

    def test_background_task_treats_a_stand_down_as_a_cancel(self, layout):
        assert 'kind === "cancel_reminders" || kind === "alert_stood_down"' in layout

    def test_both_live_handlers_cancel_the_ladder(self, layout):
        # One in the tap handler, one in the received listener, plus the
        # background task above.
        blocks = layout.split('kind === "alert_stood_down"')
        assert len(blocks) >= 3, "expected the stand-down kind in 3 places"
        for b in blocks[1:]:
            head = b[:600]
            assert "cancelCheckInReminders" in head, (
                "a stand-down path that does not cancel the reminder ladder"
            )
