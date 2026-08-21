"""#274 — a stand-down must not clear the people still asking for help,
and #271 — the ask limits an operator is held to.

Paul, 2026-08-21:
  "It should only clear those who reported safe, keep those asking for
   help, and clearly list WHO is being left behind before confirming."
  "Once per person per hour as the cap ... widen the gap when the battery
   is low ... never make them guess whether someone has already been
   chased."

The stand-down tests read the preview endpoint (the exact data the
confirm dialog shows). The ask-limit tests are pure unit tests on
server._ask_state, so they can never be skipped for environment reasons.

Seed prerequisite for the HTTP part:
    python backend/scripts/seed_268_scenario.py
"""
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

BASE_URL = (
    os.environ.get("EXPO_PUBLIC_BACKEND_URL")
    or os.environ.get("EXPO_BACKEND_URL")
    or "http://localhost:8001"
).rstrip("/")
ADMIN_TOKEN = os.environ.get("ADMIN_TRIGGER_PASSWORD", "m11vRwfDoxnHvIMLkKzjUwQy")

ID_C = "qg-1755700000003-neo268c"   # trapped, seeded by seed_268_scenario


@pytest.fixture(scope="module")
def hdr():
    return {"X-Admin-Token": ADMIN_TOKEN}


@pytest.fixture(scope="module")
def preview(hdr):
    r = requests.get(
        f"{BASE_URL}/api/admin/alert/stand-down/preview", headers=hdr, timeout=20,
    )
    assert r.status_code == 200, r.text
    return r.json()


class TestStandDownPreviewNamesWhoStays:
    def test_shape(self, preview):
        for key in ("total", "clearing_count", "staying_count",
                    "staying_people", "confirmation_phrase"):
            assert key in preview, preview

    def test_people_asking_for_help_are_listed_by_name(self, preview):
        # Test entries are held back too, but counted rather than listed,
        # so thirteen TEST rows cannot bury the one real name (#274).
        assert preview["staying_real_count"] == len(preview["staying_people"])
        assert (preview["staying_real_count"] + preview["staying_test_count"]
                == preview["staying_count"])
        assert preview["staying_count"] >= 1, (
            "seeded trapped record C should be on the staying list"
        )
        ids = {p["device_id"] for p in preview["staying_people"]}
        assert ID_C in ids, preview["staying_people"]

    def test_each_line_says_who_and_how_bad_in_plain_words(self, preview):
        person = next(p for p in preview["staying_people"]
                      if p["device_id"] == ID_C)
        assert person["code"], person
        assert person["words"] in (
            "Badly hurt", "Hurt", "Not hurt, but stuck", "Asked for help",
        ), person
        # #272: the time a person reads is Malta time, never a raw ISO string.
        assert "T" not in person["last_heard"], person["last_heard"]

    def test_the_two_groups_never_exceed_the_phones_we_have(self, preview):
        assert preview["clearing_count"] <= preview["total"]


class TestAskLimits:
    """#271 — the limits, in the one place the button and the server agree."""

    def _state(self, row, minutes_ago=None, count=0, unanswered=0):
        import server
        now = datetime(2026, 8, 21, 22, 0, tzinfo=timezone.utc)
        if minutes_ago is not None:
            row = {**row, "asks": {
                "count": count, "unanswered": unanswered,
                "last_at": (now - timedelta(minutes=minutes_ago)).isoformat(),
            }}
        return server._ask_state(row, now)

    def test_never_asked_says_so(self):
        st = self._state({})
        assert st["can_ask"] is True
        assert st["history_words"] == "Not asked yet."

    def test_one_hour_gap_for_an_ordinary_phone(self):
        st = self._state({"battery_pct": 80}, minutes_ago=40, count=1, unanswered=1)
        assert st["gap_minutes"] == 60
        assert st["can_ask"] is False
        assert "Wait" in st["blocked_reason"]
        # The operator must be able to READ the history, not deduce it.
        assert "Asked once" in st["history_words"]
        assert "no answer" in st["history_words"]

    def test_after_the_hour_it_is_allowed_again(self):
        st = self._state({"battery_pct": 80}, minutes_ago=70, count=1, unanswered=1)
        assert st["can_ask"] is True
        assert st["blocked_reason"] is None

    def test_low_battery_widens_the_gap_and_says_why(self):
        st = self._state({"battery_pct": 9}, minutes_ago=70, count=1, unanswered=1)
        assert st["low_battery"] is True
        assert st["gap_minutes"] == 180
        assert st["can_ask"] is False
        assert "battery is low" in st["blocked_reason"]

    def test_two_unanswered_asks_is_the_cap(self):
        st = self._state({"battery_pct": 80}, minutes_ago=600, count=2, unanswered=2)
        assert st["can_ask"] is False
        assert "radio" in st["blocked_reason"]

    def test_an_answer_resets_the_counter(self):
        st = self._state({"battery_pct": 80}, minutes_ago=600, count=2, unanswered=0)
        assert st["can_ask"] is True
        assert "they answered" in st["history_words"]

    def test_no_developer_words_anywhere_in_the_operator_text(self):
        banned = ("null", "token", "payload", "unregistered", "endpoint",
                  "APNs", "device_id", "true", "false")
        for st in (
            self._state({}),
            self._state({"battery_pct": 9}, minutes_ago=10, count=2, unanswered=2),
            self._state({"battery_pct": 80}, minutes_ago=10, count=1, unanswered=1),
        ):
            text = (st["history_words"] + " " + (st["blocked_reason"] or "")).lower()
            for word in banned:
                assert word.lower() not in text, (word, text)


class TestCheckInRequestIsNotAnEarthquakeAlert:
    """The push an operator's ask sends must never look, sound or route
    like a real alert (#207, and Paul's rule on the wording)."""

    def test_payload(self):
        import apns
        p = apns._build_check_in_request_payload(
            title="Are you all right?",
            body="No new earthquake. Please tap to tell us how you are.",
            check_id="ask-1", device_id="dev-1",
        )
        aps = p["aps"]
        assert aps["sound"] == "default"
        assert aps["interruption-level"] == "active"
        assert "critical" not in str(aps).lower()
        assert p["body"]["kind"] == "check_in_request"
        # Reassurance first, always.
        assert p["aps"]["alert"]["body"].startswith("No new earthquake.")
