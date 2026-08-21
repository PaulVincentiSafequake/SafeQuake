"""#276 — "no answer" and "we cannot confirm they saw it" are different facts.

Paul, 2026-08-21, after the check-in request never arrived on his phone:
  "The card must distinguish 'the phone received our question and nobody
   answered' from 'we cannot confirm the phone ever saw it'. Those are
   different facts and only one is worrying."
  "Do not solve this by making check-in requests critical alerts."
  "Check whether the same silent failure has been happening with re-check
   notifications all along."

Root cause of the disappearance, locked in by the payload/header tests
below: every check-in request went out with `apns-expiration: 0` — Apple
attempts delivery once and never stores it — and at priority 5, which
invites Apple to delay it. Delay plus "do not store" is how a push
vanishes with a 200 on our side. The re-check ladder had the same
expiration bug, which means historic "no answer to the previous re-check"
rows may be false negatives.

Live part of this file drives the real preview API.
"""
import os
import sys
import time
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
ID_A = "qg-1755700000001-neo268a"
# Created and deleted by the live test below. Clearly a test row.
DEV_276 = "qg-test-276-delivery"


@pytest.fixture(scope="module")
def hdr():
    return {"X-Admin-Token": ADMIN_TOKEN}


@pytest.fixture(scope="module")
def db():
    import pymongo
    url = os.environ.get("MONGO_URL", "mongodb://localhost:27017")
    name = os.environ.get("DB_NAME", "test_database")
    return pymongo.MongoClient(url)[name]


# ── The delivery contract ─────────────────────────────────────────────
class TestTheQuestionCanSurviveTheJourney:
    def test_check_in_request_is_not_a_critical_alert(self):
        import apns
        aps = apns._build_check_in_request_payload(
            title="Are you all right?",
            body="No new earthquake. Please tap to tell us how you are.",
            check_id="ask-1", device_id="dev-1",
        )["aps"]
        assert aps["interruption-level"] == "time-sensitive"
        assert aps["sound"] == "default"          # never the siren file
        assert "critical" not in str(aps).lower()  # never the entitlement

    def test_it_asks_the_phone_to_confirm_it_arrived(self):
        import apns
        aps = apns._build_check_in_request_payload(
            title="t", body="b", check_id="ask-1", device_id="dev-1",
        )["aps"]
        assert aps["content-available"] == 1

    def test_rechecks_ask_for_confirmation_too(self):
        import apns
        aps = apns._build_recheck_payload(
            title="t", body="b", check_id="c1", device_id="dev-1",
        )["aps"]
        assert aps["content-available"] == 1
        assert aps["interruption-level"] == "time-sensitive"

    @pytest.mark.asyncio
    async def test_the_ask_is_stored_by_apple_not_dropped(self, monkeypatch):
        """The bug that lost it: expiration 0 + priority 5. Both fixed, and
        pinned here on the HEADER values actually handed to _send_one."""
        import apns
        seen = {}

        async def fake_send_one(cfg, **kw):
            seen.update(kw)

            class R:
                def as_dict(self):
                    return {"status_code": 200, "reason": None,
                            "apns_id": "x", "environment": "production"}
            return R()

        async def fake_cfg(_db):
            return object()

        async def fake_prune(_db, _res):
            return 0

        monkeypatch.setattr(apns, "_send_one", fake_send_one)
        monkeypatch.setattr(apns, "load_apns_config", fake_cfg)
        monkeypatch.setattr(apns, "_prune_dead_devices", fake_prune)
        await apns.send_check_in_request(
            None, {"user_id": "d1", "device_token": "t1", "check_id": "ask-1"},
            idempotency_key="ask-1",
        )
        assert seen["apns_priority"] == "10"
        # Stored for half an hour, not "one attempt then bin it".
        assert int(seen["apns_expiration"]) - int(time.time()) > 1200

    @pytest.mark.asyncio
    async def test_rechecks_are_stored_too(self, monkeypatch):
        """Historic re-checks went out with expiration 0. Any 'no answer to
        the previous re-check' from before this fix may be a false
        negative — that is why this test exists."""
        import apns
        seen = {}

        async def fake_send_one(cfg, **kw):
            seen.update(kw)

            class R:
                def as_dict(self):
                    return {"status_code": 200}
            return R()

        async def fake_cfg(_db):
            return object()

        async def fake_prune(_db, _res):
            return 0

        monkeypatch.setattr(apns, "_send_one", fake_send_one)
        monkeypatch.setattr(apns, "load_apns_config", fake_cfg)
        monkeypatch.setattr(apns, "_prune_dead_devices", fake_prune)
        await apns.send_recheck_prompts(
            None, [{"user_id": "d1", "device_token": "t1", "check_id": "c1"}],
            title="t", body="b", idempotency_key="k",
        )
        assert int(seen["apns_expiration"]) - int(time.time()) > 400


# ── The two different facts ───────────────────────────────────────────
class TestTheBoardTellsThemApart:
    def _classify(self, minutes_ago, confirmed=False):
        from record_state import classify
        now = datetime(2026, 8, 21, 22, 0, tzinfo=timezone.utc)
        asked_at = now - timedelta(minutes=minutes_ago)
        delivery = {"apns_status": 200, "accepted_at": asked_at.isoformat()}
        if confirmed:
            delivery["confirmed_at"] = (asked_at + timedelta(seconds=20)).isoformat()
        return classify(
            {"status": "safe",
             "updated_at": (now - timedelta(hours=5)).isoformat(),
             "asks": {"count": 1, "unanswered": 1,
                      "last_at": asked_at.isoformat(),
                      "delivery": delivery}},
            push_row=None, ever_needed_help=False, ever_located=True,
            incident_active=False, last_alert_at=None, now=now,
        )

    def test_confirmed_and_still_silent_is_the_worrying_one(self):
        import record_state as rs
        st = self._classify(120, confirmed=True)
        assert st.state == rs.NO_ANSWER
        assert st.label == "Got our question, no answer"
        assert "got our question" in st.detail.lower()
        assert st.on_working_board is True

    def test_unconfirmed_says_we_cannot_tell(self):
        import record_state as rs
        st = self._classify(120, confirmed=False)
        assert st.state == rs.DARK
        assert "never confirmed" in st.detail
        assert "cannot tell whether they saw it" in st.detail

    def test_inside_the_window_it_is_still_just_waiting(self):
        import record_state as rs
        assert self._classify(5, confirmed=True).state == rs.WAITING
        assert self._classify(5, confirmed=False).state == rs.WAITING

    def test_waiting_says_which_kind_of_waiting_it_is(self):
        confirmed = self._classify(5, confirmed=True).detail
        unconfirmed = self._classify(5, confirmed=False).detail
        assert "got our question" in confirmed.lower()
        assert "has not confirmed" in unconfirmed

    def test_no_operator_facing_jargon(self):
        for st in (self._classify(120, True), self._classify(120, False),
                   self._classify(5, False)):
            text = (st.label + " " + st.detail).lower()
            for word in ("apns", "payload", "token", "200", "push", "null"):
                assert word not in text, (word, text)


# ── Live: the receipt changes what the operator is told ───────────────
class TestReceiptEndToEnd:
    def test_receipt_flips_the_card_from_unconfirmed_to_no_answer(self, hdr, db):
        # A dedicated TEST row, not one of the seeded people: the dark clock
        # runs from the most recent ask OR broadcast, so any test that fires
        # a trigger would otherwise reset this one's clock underneath it.
        now = datetime.now(timezone.utc)
        asked_at = (now - timedelta(hours=2)).isoformat()
        db.device_status.update_one(
            {"device_id": DEV_276},
            {"$set": {
                "display_name": "TEST 276 delivery",
                "is_test": True,
                "status": "safe",
                "updated_at": (now - timedelta(hours=5)).isoformat(),
                "asks": {"count": 1, "unanswered": 1, "last_at": asked_at,
                         "last_by": "test", "last_check_id": "ask-live-276",
                         "delivery": {"apns_status": 200,
                                      "accepted_at": asked_at,
                                      "confirmed_at": None}},
            }, "$unset": {"push_receipt": "", "recheck": ""}},
            upsert=True,
        )

        def state_of():
            r = requests.get(f"{BASE_URL}/api/devices?limit=1000&include_test=1",
                             headers=hdr, timeout=20)
            assert r.status_code == 200, r.text
            row = next(d for d in r.json()["devices"]
                       if d["device_id"] == DEV_276)
            return row["record_state"], row.get("ask_state") or {}

        # Live, the dark clock also runs from the most recent BROADCAST, so
        # right after any trigger this row reads "waiting for an answer".
        # What matters here — and what was missing entirely before #276 — is
        # that the card SAYS whether the phone confirmed our question.
        st, ask = state_of()
        assert st["state"] in ("phone_went_dark", "waiting_for_answer"), st
        assert "not confirmed" in st["detail"] or "never confirmed" in st["detail"]
        assert "has not confirmed it arrived" in ask["history_words"], ask

        r = requests.post(
            f"{BASE_URL}/api/push/receipt",
            json={"device_id": DEV_276, "check_id": "ask-live-276",
                  "kind": "check_in_request", "how": "shown",
                  "seen_at": asked_at},
            timeout=20,
        )
        assert r.status_code == 200, r.text

        st, ask = state_of()
        assert st["state"] in ("no_answer", "waiting_for_answer"), st
        assert "got our question" in st["detail"].lower(), st
        assert st["on_working_board"] is True
        assert "Their phone confirmed it arrived." in ask["history_words"], ask

        db.device_status.delete_one({"device_id": DEV_276})

    def test_receipt_needs_a_phone(self):
        r = requests.post(f"{BASE_URL}/api/push/receipt", json={}, timeout=20)
        assert r.status_code == 400
        assert "which phone" in r.json()["detail"]

    def test_counts_carry_the_new_number(self, hdr):
        r = requests.get(f"{BASE_URL}/api/devices?limit=5", headers=hdr, timeout=20)
        assert "no_answer" in r.json()["counts"]
