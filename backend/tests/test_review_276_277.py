"""Review round #276 / #277 / #274-completeness — testing agent.

Covers the bullets on the review request that are NOT already covered by
test_delivery_truth_276.py or test_stand_down_split_274.py:

  * BACKEND #274: preview must return staying_test_count as an integer and
    real+test == staying_count; every listed person carries the fields the
    dialog needs (device_id, name, code, words, waiting_words, last_heard
    Malta-time, battery_pct).

  * BACKEND #276 surfaces on /api/devices: counts.no_answer exists, every
    row with an ask carries ask_state.history_words with the "Their phone
    confirmed / has not confirmed it arrived." sentence, and
    ask_state.delivery is present with the apns_status / apns_reason /
    apns_id / accepted_at / confirmed_at fields when we have asked.

  * BACKEND #276 ask-to-check-in response: on success returns
    delivery{...} and a message that does not PROMISE the phone saw
    anything. On refuse-paths the plain-English wording holds.

  * BACKEND #277: while an alert is live, GET /api/admin/incident-status
    must return the exact plain-English `reason` Paul asked for, with no
    'idle sign-out', no '72h', no 'suspended', and a 'Running for ...'
    line. We insert a `push_events` trigger row directly into Mongo and
    DELETE IT AFTERWARDS — the review request explicitly forbids calling
    /api/trigger-alert against real phones.

  * BACKEND regression: /api/devices still returns
    devices/off_board/notices/counts/count_notes; the CSV audit export
    still starts at,at_simple,kind and at_simple is Malta local time; both
    casualty PDFs still render 200.

  * FRONTEND code review only: sanity checks on pushReceipt.ts and the
    three call sites in _layout.tsx.

Everything lives on the preview backend named by EXPO_PUBLIC_BACKEND_URL /
EXPO_BACKEND_URL. Any state we write to Mongo we clean up. We NEVER call
/api/trigger-alert.
"""
import io
import os
import sys
import time
import re
import csv
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Local backend on purpose: the ask-to-check-in call below waits on a real
# APNs attempt, and through the public ingress that occasionally comes back
# as an empty 502 from the proxy rather than the backend's own answer.
BASE_URL = "http://localhost:8001"
ADMIN_TOKEN = os.environ["ADMIN_TRIGGER_PASSWORD"]

ID_A = "qg-1755700000001-neo268a"
ID_C = "qg-1755700000003-neo268c"


@pytest.fixture(scope="module")
def hdr():
    return {"X-Admin-Token": ADMIN_TOKEN}


@pytest.fixture(scope="module")
def db():
    import pymongo
    url = os.environ.get("MONGO_URL", "mongodb://localhost:27017")
    name = os.environ.get("DB_NAME", "test_database")
    return pymongo.MongoClient(url)[name]


# ─────────────────── #274 preview completeness ─────────────────────
class TestStandDownPreviewFields:
    def test_preview_returns_the_three_split_numbers(self, hdr):
        r = requests.get(
            f"{BASE_URL}/api/admin/alert/stand-down/preview",
            headers=hdr, timeout=20,
        )
        assert r.status_code == 200, r.text
        p = r.json()
        for k in ("staying_count", "staying_real_count", "staying_test_count",
                  "staying_people", "clearing_count", "total",
                  "confirmation_phrase"):
            assert k in p, k
        assert isinstance(p["staying_test_count"], int)
        assert isinstance(p["staying_real_count"], int)
        # Real + test == staying_count is the invariant that fixes the
        # "13 real + 13 test double-count" bug.
        assert p["staying_real_count"] + p["staying_test_count"] == p["staying_count"]
        # staying_real_count = len(people listed). Test entries are counted, not listed.
        assert p["staying_real_count"] == len(p["staying_people"])

    def test_every_listed_person_has_the_dialog_fields(self, hdr):
        p = requests.get(
            f"{BASE_URL}/api/admin/alert/stand-down/preview",
            headers=hdr, timeout=20,
        ).json()
        if p["staying_real_count"] == 0:
            pytest.skip("no one on the working board asking for help")
        for person in p["staying_people"]:
            for k in ("device_id", "name", "code", "words",
                      "waiting_words", "last_heard", "battery_pct"):
                assert k in person, (k, person)
            # #272: never a raw ISO string.
            assert "T" not in (person["last_heard"] or ""), person["last_heard"]
            # words are one of the four plain phrases.
            assert person["words"] in (
                "Badly hurt", "Hurt", "Not hurt, but stuck", "Asked for help",
            ), person


# ─────────────────── #276 surfaces on /api/devices ─────────────────
class TestDevicesEnvelopeCarriesTheNewFacts:
    @pytest.fixture(scope="class")
    def devices_payload(self, hdr):
        r = requests.get(
            f"{BASE_URL}/api/devices?limit=2000&include_test=1",
            headers=hdr, timeout=20,
        )
        assert r.status_code == 200, r.text
        return r.json()

    def test_envelope_shape_still_268(self, devices_payload):
        for k in ("devices", "off_board", "notices", "counts",
                  "count_notes", "count", "off_board_count"):
            assert k in devices_payload, k

    def test_counts_include_no_answer(self, devices_payload):
        assert "no_answer" in devices_payload["counts"]
        assert isinstance(devices_payload["counts"]["no_answer"], int)

    def test_ask_state_history_words_after_an_ask(self, hdr, db):
        """A row that has been asked once and not confirmed must read
        'Their phone has not confirmed it arrived.' — and once we drop a
        confirmed_at onto the ask, it must flip to
        'Their phone confirmed it arrived.'"""
        dev_id = "qg-test-276-history-words"
        now = datetime.now(timezone.utc)
        asked_at = (now - timedelta(minutes=15)).isoformat()
        db.device_status.update_one(
            {"device_id": dev_id},
            {"$set": {
                "display_name": "TEST 276 history-words",
                "is_test": True,
                "status": "safe",
                "updated_at": (now - timedelta(hours=1)).isoformat(),
                "asks": {"count": 1, "unanswered": 1, "last_at": asked_at,
                         "last_by": "test", "last_check_id": "ask-hw",
                         "delivery": {"apns_status": 200,
                                      "accepted_at": asked_at,
                                      "confirmed_at": None}},
            }, "$unset": {"push_receipt": ""}},
            upsert=True,
        )
        try:
            def row_for(dev_id):
                r = requests.get(
                    f"{BASE_URL}/api/devices?limit=2000&include_test=1",
                    headers=hdr, timeout=20,
                )
                assert r.status_code == 200, r.text
                for d in r.json()["devices"]:
                    if d["device_id"] == dev_id:
                        return d
                for d in r.json().get("off_board", []):
                    if d["device_id"] == dev_id:
                        return d
                return None

            row = row_for(dev_id)
            assert row is not None, "seeded test row not found on the board"
            hw = (row.get("ask_state") or {}).get("history_words") or ""
            assert "has not confirmed" in hw, hw

            # ask_state.delivery must carry the four APNs facts.
            deliv = (row.get("ask_state") or {}).get("delivery") or {}
            assert deliv.get("apns_status") == 200
            assert "accepted_at" in deliv
            assert "confirmed_at" in deliv  # present, currently null
            # confirm it — history flips.
            db.device_status.update_one(
                {"device_id": dev_id},
                {"$set": {"asks.delivery.confirmed_at": asked_at}},
            )
            row = row_for(dev_id)
            hw = (row.get("ask_state") or {}).get("history_words") or ""
            assert "confirmed it arrived" in hw and "not confirmed" not in hw, hw
        finally:
            db.device_status.delete_one({"device_id": dev_id})


# ─────────── #276 ask-to-check-in response contract ───────────────
class TestAskToCheckInResponse:
    """The *response body* returned by POST ask-to-check-in must carry the
    delivery block AND a message that does NOT promise the phone saw
    anything — the operator must not be told a fact we do not have."""

    def test_success_response_shape_when_the_send_actually_works(
        self, hdr, db, monkeypatch=None,
    ):
        # We don't want to actually push a live phone. So arrange the
        # device row to look ready, then patch push_devices with a
        # placeholder token — the send WILL fail at APNs (no valid token),
        # which is fine, we just verify the failure surface stays plain
        # English and never mentions APNs.
        dev_id = "qg-test-276-ask-response"
        now = datetime.now(timezone.utc)
        db.device_status.update_one(
            {"device_id": dev_id},
            {"$set": {
                "display_name": "TEST 276 ask-response",
                "is_test": True,
                "status": "safe",
                "battery_pct": 80,
                "updated_at": (now - timedelta(hours=2)).isoformat(),
            }, "$unset": {"asks": "", "push_receipt": ""}},
            upsert=True,
        )
        db.push_devices.update_one(
            {"user_id": dev_id},
            {"$set": {"user_id": dev_id, "platform": "ios",
                      "device_token": "0" * 64, "dead_token": False}},
            upsert=True,
        )
        try:
            r = requests.post(
                f"{BASE_URL}/api/admin/records/{dev_id}/ask-to-check-in",
                headers=hdr, json={}, timeout=20,
            )
            # On a fake token the preview backend will return 502 with the
            # plain-English "we could not get a message through" message.
            # On the very off-chance a real token slipped through and got
            # a 200 back from APNs, we also accept that.
            assert r.status_code in (200, 502), r.text
            body = r.json()
            if r.status_code == 200:
                assert "delivery" in body, body
                for k in ("apns_status", "apns_id", "accepted_at", "confirmed_at"):
                    assert k in body["delivery"], (k, body)
                # The message never promises the phone showed anything.
                msg = body["message"].lower()
                assert "phone will confirm" in msg or "confirm when it arrives" in msg
                assert "will see" not in msg  # must not promise sight
            else:
                # 502 path: still no APNs jargon in the operator-facing text.
                detail = body.get("detail", "")
                for w in ("apns", "payload", "token", "unregistered", "null"):
                    assert w.lower() not in detail.lower(), detail
        finally:
            db.device_status.delete_one({"device_id": dev_id})
            db.push_devices.delete_one({"user_id": dev_id})


# ───────────────────── #277 incident-status wording ────────────────
class TestIncidentStatusReasonReadsInPlainWords:
    """Insert a trigger row directly into `push_events`, verify the
    reason wording, DELETE IT. Do NOT call /api/trigger-alert."""

    def test_reason_wording_while_an_alert_is_live(self, hdr, db):
        # A separate `kind: "trigger"` row that a stand-down could match
        # (kind: "alert_stood_down") for cleanup, if one wasn't already
        # written after any earlier live test. We stamp our own kind
        # marker so we can find it back.
        marker = f"test-review-276-277-{int(time.time())}"
        now = datetime.now(timezone.utc)
        # Insert a trigger with `created_at` just now so it beats any older
        # stand-down. The dark-clock note in the agent-to-agent context
        # says a broadcast trigger resets the dark clock, so DO NOT do
        # anything that reads record_state on real devices after this.
        db.push_events.insert_one({
            "kind": "trigger",
            "created_at": now.isoformat(),
            "triggered_by": marker,
        })
        try:
            r = requests.get(
                f"{BASE_URL}/api/admin/incident-status",
                headers=hdr, timeout=20,
            )
            assert r.status_code == 200, r.text
            data = r.json()
            assert data["active"] is True, data
            reason = (data.get("reason") or "")
            # The forbidden jargon Paul called out explicitly.
            for banned in ("idle sign-out", "72h", "suspended", "72 hours"):
                assert banned.lower() not in reason.lower(), (banned, reason)
            # The wording Paul asked for.
            assert "An alert is running." in reason, reason
            assert "stay signed in" in reason, reason
            assert "3 days" in reason, reason
            # 'Running for ...' on its own line — a newline before it.
            assert "\nRunning for " in reason, repr(reason)
        finally:
            # ALWAYS remove the row we inserted, so subsequent runs and
            # the real dashboard both see the actual incident state.
            db.push_events.delete_many({"triggered_by": marker})
            # And stamp a stand-down AFTER the trigger too, in case the
            # earlier trigger row was inserted with a later created_at
            # somehow — belt-and-braces cleanup.
            db.push_events.insert_one({
                "kind": "alert_stood_down",
                "created_at": (datetime.now(timezone.utc)
                               + timedelta(seconds=1)).isoformat(),
                "triggered_by": marker + "-cleanup",
            })
            db.push_events.delete_many({"triggered_by": marker + "-cleanup"})

    def test_reason_is_none_when_no_alert_is_live(self, hdr):
        # The preview database may or may not have a live alert. Call and
        # simply assert the invariant.
        data = requests.get(
            f"{BASE_URL}/api/admin/incident-status",
            headers=hdr, timeout=20,
        ).json()
        if not data["active"]:
            assert data.get("reason") in (None, "")


# ─────────────────────── #268 regression ───────────────────────────
class TestRegressions:
    def test_devices_envelope_still_there(self, hdr):
        r = requests.get(f"{BASE_URL}/api/devices?limit=5", headers=hdr, timeout=20)
        assert r.status_code == 200, r.text
        j = r.json()
        for k in ("devices", "off_board", "notices", "counts", "count_notes"):
            assert k in j

    def test_csv_export_header_row_shape(self, hdr):
        r = requests.get(
            f"{BASE_URL}/api/admin/audit-log/export.csv",
            headers=hdr, timeout=30,
        )
        assert r.status_code == 200, r.text[:400]
        text = r.text
        # Find the DATA header row. It starts "at,at_simple,kind" per
        # _CSV_COLUMNS, but sits after some metadata rows.
        lines = text.splitlines()
        header_idx = next(
            (i for i, ln in enumerate(lines) if ln.startswith("at,at_simple,kind")),
            None,
        )
        assert header_idx is not None, "\n".join(lines[:20])
        # And the metadata row must say at_simple is Malta time.
        joined = "\n".join(lines[:header_idx])
        assert "at_simple" in joined and "Malta" in joined, joined

    def test_operational_casualty_pdf_renders(self, hdr):
        r = requests.get(
            f"{BASE_URL}/api/admin/casualty-report/operational.pdf",
            headers=hdr, timeout=45,
        )
        assert r.status_code == 200, r.text[:400]
        assert r.headers.get("content-type", "").startswith("application/pdf")
        assert r.content.startswith(b"%PDF"), r.content[:16]

    def test_public_casualty_pdf_renders(self, hdr):
        r = requests.get(
            f"{BASE_URL}/api/admin/casualty-report/public.pdf",
            headers=hdr, timeout=45,
        )
        assert r.status_code == 200, r.text[:400]
        assert r.headers.get("content-type", "").startswith("application/pdf")
        assert r.content.startswith(b"%PDF"), r.content[:16]


# ─────────────────── FRONTEND code review (statics) ────────────────
class TestPushReceiptStaticCodeReview:
    """pushReceipt.ts is impossible to exercise from a web browser (it
    early-returns Platform.OS === 'web'). What we CAN do is read it back
    and pin its guarantees: never throws, only sends for the four kinds
    Paul listed, and dedupes so the same receipt is not sent twice."""

    def _file(self):
        p = Path("/app/frontend/src/utils/pushReceipt.ts")
        assert p.exists(), p
        return p.read_text()

    def test_never_throws(self):
        src = self._file()
        # Both async functions must have a try/catch that swallows
        # everything — best-effort by design.
        assert src.count("try {") >= 2, "expected try/catch in reportPushSeen and reportPresentedPushes"
        assert "} catch" in src

    def test_only_the_four_asking_kinds(self):
        src = self._file()
        # The kinds list must be exactly these four.
        m = re.search(r"ASKING_KINDS\s*=\s*\[([^\]]+)\]", src)
        assert m, src
        listed = {s.strip().strip('"').strip("'") for s in m.group(1).split(",") if s.strip()}
        assert listed == {"check_in_request", "recheck", "critical_alert",
                          "quakeguard-reminder"}, listed

    def test_dedupes_by_kind_and_check_id(self):
        src = self._file()
        assert "sent.has(key)" in src and "sent.add(key)" in src

    def test_web_short_circuit(self):
        src = self._file()
        assert 'Platform.OS === "web"' in src

    def test_wired_from_three_call_sites_in_layout(self):
        layout = Path("/app/frontend/app/_layout.tsx").read_text()
        # (1) tap
        assert 'reportPushSeen(data, "tapped")' in layout
        # (2) received
        assert 'reportPushSeen(data, "shown")' in layout
        # (3) cold start + AppState 'active'
        assert layout.count("reportPresentedPushes()") >= 2
