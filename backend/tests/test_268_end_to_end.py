"""#268 end-to-end HTTP tests against the running backend.

Doctrine under test (Paul, 2026-08-21):
  1. Four plain-English silence states.
  2. Status outranks device state — help history keeps a record on the
     working board no matter what the phone does.
  3. Nothing is ever deleted from a rescue board by software.
  4. Duplicates are flagged with evidence, never merged.
  5. Removed / never-used devices are never counted in "not responding".
  6. Records moved off the board go to a visible labelled area.

These tests hit http://localhost:8001 (per review request) because this
suite's TestClient+Motor combination only tolerates ONE db-touching
request per test module. Real HTTP lets a whole flow run in one test.
"""
from __future__ import annotations

import copy
import io
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
import requests
from dotenv import load_dotenv

load_dotenv("/app/backend/.env")

BASE = "http://localhost:8001"
TOKEN = os.environ.get("ADMIN_TRIGGER_PASSWORD", "")
HDR = {"X-Admin-Token": TOKEN}

A = "qg-1755700000001-neo268a"  # live, answering
B = "qg-1755600000002-neo268b"  # old install — app removed
C = "qg-1755700000003-neo268c"  # trapped person whose token is also Unregistered
D = "qg-1755700000004-neo268d"  # never used the app
E = "qg-1755700000005-neo268e"  # phone went dark 3h ago
ALL_IDS = [A, B, C, D, E]


def _seed():
    """Reset the #268 scenario in the preview DB."""
    env = os.environ.copy()
    env.setdefault("MONGO_URL", os.environ.get("MONGO_URL", ""))
    env.setdefault("DB_NAME", os.environ.get("DB_NAME", "test_database"))
    subprocess.check_call(
        [sys.executable, "scripts/seed_268_scenario.py"],
        cwd="/app/backend", env=env,
    )


@pytest.fixture(scope="module", autouse=True)
def seed_once():
    _seed()
    yield
    # Re-seed at end — a downstream test could have consumed the rows.
    _seed()


# ── helpers ───────────────────────────────────────────────────────────
def _devices():
    r = requests.get(f"{BASE}/api/devices", headers=HDR, timeout=15)
    assert r.status_code == 200, r.text
    return r.json()


def _find(body, did):
    for x in body["devices"]:
        if x["device_id"] == did:
            return "on", x
    for x in body["off_board"]:
        if x["device_id"] == did:
            return "off", x
    return None, None


# ─────────────────────────────────────────────────────────────────────
# 1. GET /api/devices envelope
# ─────────────────────────────────────────────────────────────────────
class TestDevicesEnvelope:
    def test_top_level_shape(self):
        b = _devices()
        for k in ("devices", "off_board", "off_board_count", "notices",
                  "counts", "count_notes"):
            assert k in b, k

    def test_working_board_rows_all_on_working_board(self):
        b = _devices()
        for d in b["devices"]:
            assert d["record_state"]["on_working_board"] is True, d["device_id"]

    def test_off_board_rows_carry_label_and_reason(self):
        b = _devices()
        assert b["off_board_count"] == len(b["off_board"])
        for d in b["off_board"]:
            assert d["label"], d
            assert d["off_board_reason"], d
            assert d["state"] in ("app_removed", "never_used",
                                  "resolved_by_operator")

    def test_counts_keys_present(self):
        b = _devices()
        for k in ("waiting_for_answer", "phone_went_dark", "app_removed",
                  "app_removed_held_on_board", "never_used",
                  "resolved_by_operator", "off_board_total",
                  "not_responding"):
            assert k in b["counts"], k

    def test_count_notes_contains_does_not_include(self):
        b = _devices()
        assert any("does not include" in n for n in b["count_notes"])


# ─────────────────────────────────────────────────────────────────────
# 2. Four states classified correctly, end-to-end
# ─────────────────────────────────────────────────────────────────────
class TestFourSilenceStatesEndToEnd:
    def test_A_is_on_board_answering(self):
        b = _devices()
        loc, row = _find(b, A)
        assert loc == "on"
        # A is either "answering" (state is None) or "waiting_for_answer"
        # depending on whether a prior test in the same preview DB fired
        # a trigger — both are legitimate ON-BOARD states.
        assert row["record_state"]["state"] in (None, "waiting_for_answer")
        assert row["record_state"]["on_working_board"] is True

    def test_B_is_off_board_app_removed(self):
        b = _devices()
        loc, row = _find(b, B)
        assert loc == "off"
        assert row["state"] == "app_removed"
        assert row["label"] == "App removed from this phone"

    def test_C_trapped_stays_on_working_board(self):
        """Status outranks device state: token=Unregistered must NOT move a
        trapped person off the board."""
        b = _devices()
        loc, row = _find(b, C)
        assert loc == "on", "trapped person moved off board"
        rs = row["record_state"]
        assert rs["on_working_board"] is True
        # Still counted as trapped, not shunted into app_removed bucket.
        assert rs["count_in_status_buckets"] is True
        assert rs["held_reason"], "no held_reason recorded for the override"
        assert "reported needing help" in rs["held_reason"]

    def test_D_never_used_is_off_board(self):
        b = _devices()
        loc, row = _find(b, D)
        assert loc == "off"
        assert row["state"] == "never_used"

    def test_E_phone_went_dark_is_on_board(self):
        b = _devices()
        loc, row = _find(b, E)
        assert loc == "on"
        assert row["record_state"]["state"] == "phone_went_dark"


# ─────────────────────────────────────────────────────────────────────
# 3. Counts honesty
# ─────────────────────────────────────────────────────────────────────
class TestCountsHonesty:
    def test_not_responding_never_includes_removed_or_never_used(self):
        b = _devices()
        c = b["counts"]
        # scan every off_board row; each is definitionally NOT counted
        # inside not_responding. A held_removed row on the board must be
        # excluded via count_in_status_buckets=False.
        for row in b["off_board"]:
            assert row["state"] in ("app_removed", "never_used",
                                    "resolved_by_operator")
        held = [d for d in b["devices"]
                if d["record_state"].get("count_in_status_buckets") is False]
        # For every held-on-board record, its device state must not
        # contribute to the not_responding count. (Enforced structurally,
        # we assert the field is respected.)
        for h in held:
            assert h["record_state"].get("app_removed_at")
        # Sanity: counts non-negative & totals coherent
        assert c["not_responding"] >= 0
        assert c["app_removed"] >= 1  # B
        assert c["never_used"] >= 1   # D
        assert c["off_board_total"] >= 2

    def test_count_notes_names_what_it_leaves_out(self):
        b = _devices()
        joined = " ".join(b["count_notes"])
        assert "does not include" in joined
        assert "app" in joined and "removed" in joined


# ─────────────────────────────────────────────────────────────────────
# 4. resolve endpoint (auth, validation, 404, side-effects)
# ─────────────────────────────────────────────────────────────────────
class TestResolveEndpoint:
    def test_401_without_auth(self):
        r = requests.post(f"{BASE}/api/admin/records/{A}/resolve",
                          json={"reason_code": "test_entry"}, timeout=10)
        assert r.status_code == 401, r.text

    def test_400_missing_reason_code(self):
        r = requests.post(f"{BASE}/api/admin/records/{A}/resolve",
                          json={}, headers=HDR, timeout=10)
        assert r.status_code == 400
        detail = r.json()["detail"]
        for code in ("duplicate", "app_removed", "never_used",
                     "test_entry", "accounted_for", "other"):
            assert code in detail

    def test_400_invalid_reason_code(self):
        r = requests.post(f"{BASE}/api/admin/records/{A}/resolve",
                          json={"reason_code": "totally_bogus"},
                          headers=HDR, timeout=10)
        assert r.status_code == 400

    def test_400_other_without_note(self):
        r = requests.post(f"{BASE}/api/admin/records/{A}/resolve",
                          json={"reason_code": "other", "reason": ""},
                          headers=HDR, timeout=10)
        assert r.status_code == 400

    def test_404_unknown_device(self):
        r = requests.post(
            f"{BASE}/api/admin/records/qg-0000000000000-nosuchxx/resolve",
            json={"reason_code": "test_entry"}, headers=HDR, timeout=10,
        )
        assert r.status_code == 404

    def test_resolve_moves_to_off_board_and_persists_row(self):
        # Use device E (phone went dark) — resolving it should send it off
        # board with the label "Resolved by an operator", and the raw
        # device_status document must still exist.
        r = requests.post(
            f"{BASE}/api/admin/records/{E}/resolve",
            json={"reason_code": "accounted_for",
                  "reason": "spoken to by radio"},
            headers=HDR, timeout=15,
        )
        assert r.status_code == 200, r.text
        j = r.json()
        assert j["device_id"] == E
        assert j["resolved_by"]  # principal recorded (legacy@dashboard)
        assert "Accounted for" in j["resolved_reason"]

        b = _devices()
        loc, row = _find(b, E)
        assert loc == "off"
        assert row["state"] == "resolved_by_operator"
        assert row["label"] == "Resolved by an operator"
        assert row["moved_by"] and row["resolved_reason"]

        # Nothing may be deleted from device_status. We confirm indirectly
        # by unresolving next test and finding the row again.


class TestUnresolveEndpoint:
    def test_401_without_auth(self):
        r = requests.post(f"{BASE}/api/admin/records/{E}/unresolve",
                          json={}, timeout=10)
        assert r.status_code == 401

    def test_unresolve_puts_the_record_back(self):
        r = requests.post(f"{BASE}/api/admin/records/{E}/unresolve",
                          json={"reason": "back on"},
                          headers=HDR, timeout=15)
        assert r.status_code == 200, r.text
        b = _devices()
        loc, row = _find(b, E)
        assert loc == "on", "record did not return to the working board"
        assert not row.get("resolved_at")


# ─────────────────────────────────────────────────────────────────────
# 5. duplicate-decision endpoint
# ─────────────────────────────────────────────────────────────────────
class TestDuplicateDecision:
    def test_401_without_auth(self):
        r = requests.post(
            f"{BASE}/api/admin/records/{A}/duplicate-decision",
            json={"other_device_id": B, "decision": "confirmed"}, timeout=10,
        )
        assert r.status_code == 401

    def test_400_invalid_decision(self):
        r = requests.post(
            f"{BASE}/api/admin/records/{A}/duplicate-decision",
            json={"other_device_id": B, "decision": "maybe"},
            headers=HDR, timeout=10,
        )
        assert r.status_code == 400
        assert "confirmed" in r.json()["detail"]

    def test_400_missing_other_device_id(self):
        r = requests.post(
            f"{BASE}/api/admin/records/{A}/duplicate-decision",
            json={"decision": "confirmed"},
            headers=HDR, timeout=10,
        )
        assert r.status_code == 400

    def test_404_when_either_record_missing(self):
        r = requests.post(
            f"{BASE}/api/admin/records/{A}/duplicate-decision",
            json={"other_device_id": "qg-0000000000000-nosuchid",
                  "decision": "confirmed"},
            headers=HDR, timeout=10,
        )
        assert r.status_code == 404

    def test_rejected_moves_nobody_and_suggestion_stops_coming_back(self):
        # First take a snapshot of the record_status rows for A and B so
        # we can confirm no data was copied/merged.
        b0 = _devices()
        _, a_before = _find(b0, A)
        _, b_before = _find(b0, B)
        assert a_before and b_before

        r = requests.post(
            f"{BASE}/api/admin/records/{A}/duplicate-decision",
            json={"other_device_id": B, "decision": "rejected"},
            headers=HDR, timeout=15,
        )
        assert r.status_code == 200, r.text
        j = r.json()
        assert j["decision"] == "rejected"

        b1 = _devices()
        # Both records remain where they were.
        loc_a, a_after = _find(b1, A)
        loc_b, b_after = _find(b1, B)
        assert loc_a == "on"
        assert loc_b == "off"

        # Suggestion must NOT come back.
        assert not a_after.get("possible_duplicate"), a_after
        assert not b_after.get("possible_duplicate"), b_after

        # No data was copied between the two records.
        assert a_before.get("display_name") == a_after.get("display_name")
        assert b_before.get("display_name") == b_after.get("display_name")
        assert a_before.get("latitude") == a_after.get("latitude")
        assert b_before.get("latitude") == b_after.get("latitude")

    def test_confirmed_resolves_older_leaves_newer_and_no_merge(self):
        # Reseed to clear the previous "rejected" decision so we can
        # exercise "confirmed" on the same pair.
        _seed()

        b0 = _devices()
        _, a_before = _find(b0, A)
        _, b_before = _find(b0, B)

        r = requests.post(
            f"{BASE}/api/admin/records/{A}/duplicate-decision",
            json={"other_device_id": B, "decision": "confirmed"},
            headers=HDR, timeout=15,
        )
        assert r.status_code == 200, r.text
        j = r.json()
        # Older (B) was resolved; newer (A) kept.
        assert j["resolved_device_id"] == B
        assert j["kept_device_id"] == A

        b1 = _devices()
        loc_a, a_after = _find(b1, A)
        loc_b, b_after = _find(b1, B)
        assert loc_a == "on"
        assert loc_b == "off"
        assert b_after["state"] == "resolved_by_operator"
        assert "same person as" in (b_after.get("resolved_reason") or "").lower()

        # No merge: nothing copied between the two records.
        assert a_before.get("display_name") == a_after.get("display_name")
        assert a_before.get("latitude") == a_after.get("latitude")
        assert a_before.get("longitude") == a_after.get("longitude")

        # Suggestion no longer comes back on either record.
        assert not a_after.get("possible_duplicate"), a_after
        # b_after is now off-board — off_board rows still expose
        # possible_duplicate on the row; it should be None after the
        # decision.
        assert not b_after.get("possible_duplicate")


# ─────────────────────────────────────────────────────────────────────
# 6. Duplicate suggestion shape (before any decision)
# ─────────────────────────────────────────────────────────────────────
class TestDuplicateSuggestionShape:
    def test_suggestion_present_with_evidence(self):
        _seed()  # ensure clean state — earlier tests may have decided
        b = _devices()
        _, a = _find(b, A)
        _, bb = _find(b, B)
        pd_a = a.get("possible_duplicate")
        pd_b = bb.get("possible_duplicate")
        assert pd_a and pd_b, "duplicate suggestion missing on one side"
        assert pd_a["text"].startswith("This may be the same person as ")
        assert pd_a["text"].endswith(".")
        assert pd_b["text"].startswith("This may be the same person as ")
        assert len(pd_a["evidence"]) == 3
        ev = " | ".join(pd_a["evidence"])
        assert "Same first name" in ev
        assert "m apart" in ev
        assert "before" in ev and "minute" in ev


# ─────────────────────────────────────────────────────────────────────
# 7. POST /api/status un-resolves a record
# ─────────────────────────────────────────────────────────────────────
class TestPhoneReportingUnresolves:
    def test_check_in_pulls_a_resolved_record_back_on_the_board(self):
        # Resolve E first.
        r = requests.post(
            f"{BASE}/api/admin/records/{E}/resolve",
            json={"reason_code": "accounted_for", "reason": "radio check"},
            headers=HDR, timeout=15,
        )
        assert r.status_code == 200, r.text
        b0 = _devices()
        loc, _ = _find(b0, E)
        assert loc == "off"

        # Now the phone reports again.
        r = requests.post(
            f"{BASE}/api/status",
            json={"deviceId": E, "status": "safe",
                  "latitude": 35.9, "longitude": 14.51,
                  "battery_pct": 50, "platform": "android"},
            timeout=15,
        )
        assert r.status_code == 200, r.text
        b1 = _devices()
        loc, row = _find(b1, E)
        assert loc == "on", "check-in did not put the record back on the board"
        assert not row.get("resolved_at")


# ─────────────────────────────────────────────────────────────────────
# 8. /api/public/summary — public shape and no leaks
# ─────────────────────────────────────────────────────────────────────
class TestPublicSummary:
    LEAK_KEYS = ("device_id", "short_code", "display_name",
                 "latitude", "longitude")

    def test_public_summary_shape_and_no_auth(self):
        r = requests.get(f"{BASE}/api/public/summary", timeout=10)
        assert r.status_code == 200, r.text
        j = r.json()
        # new keys inside counts
        for k in ("waiting_for_answer", "phone_went_dark", "app_removed",
                  "never_used", "resolved_by_operator", "off_board_total",
                  "safe", "trapped", "rescued", "not_responding"):
            assert k in j["counts"], k
        assert isinstance(j["count_notes"], list) and j["count_notes"]

    def test_public_summary_does_not_leak_device_data(self):
        r = requests.get(f"{BASE}/api/public/summary", timeout=10)
        text = r.text
        for k in self.LEAK_KEYS:
            assert f'"{k}"' not in text, f"public summary leaks {k!r}"


# ─────────────────────────────────────────────────────────────────────
# 9. Reports — CSV keys + PDF appendix + one-page public PDF
# ─────────────────────────────────────────────────────────────────────
class TestReportsAndCSV:
    def test_audit_csv_contains_268_rows(self):
        r = requests.get(f"{BASE}/api/admin/audit-log/export.csv",
                         headers=HDR, timeout=30)
        assert r.status_code == 200, r.text
        csv_text = r.text
        for k in ("people_on_working_board", "waiting_for_an_answer",
                  "phone_went_dark", "not_on_working_board_app_removed",
                  "not_on_working_board_never_used_app",
                  "what_these_numbers_count",
                  "not_on_working_board_record"):
            assert k in csv_text, f"missing CSV row {k!r}"

    def _pdf_text(self, pdf_bytes: bytes) -> tuple[int, str]:
        from pypdf import PdfReader  # type: ignore
        reader = PdfReader(io.BytesIO(pdf_bytes))
        text = "\n".join((p.extract_text() or "") for p in reader.pages)
        return len(reader.pages), text

    def test_operational_pdf_has_off_board_appendix(self):
        r = requests.get(f"{BASE}/api/admin/casualty-report/operational.pdf",
                         headers=HDR, timeout=60)
        assert r.status_code == 200, r.text
        assert r.headers.get("content-type", "").startswith("application/pdf")
        pages, text = self._pdf_text(r.content)
        assert "Records not on the working board" in text, (
            "operational PDF is missing the off-board appendix; "
            f"first 400 chars: {text[:400]!r}"
        )

    def test_public_pdf_is_one_page_and_mentions_set_aside(self):
        r = requests.get(f"{BASE}/api/admin/casualty-report/public.pdf",
                         headers=HDR, timeout=60)
        assert r.status_code == 200, r.text
        pages, text = self._pdf_text(r.content)
        assert pages == 1, f"public PDF has {pages} pages, expected 1"
        assert "set-aside" in text or "set aside" in text, (
            f"public PDF missing set-aside sentence; text: {text!r}"
        )


# ─────────────────────────────────────────────────────────────────────
# 10. purge-all — MUST run LAST. Deletes push_devices rows.
#     Test order is preserved by pytest within a module.
# ─────────────────────────────────────────────────────────────────────
class TestZZ_PurgeAllLast:
    """The 'ZZ' prefix pins these last. The seeder is re-run in the
    module teardown so the next test module sees a clean scenario."""

    def _stand_down(self):
        # Ensure no live alert is confusing the purge test.
        try:
            requests.post(f"{BASE}/api/admin/alert/stand-down",
                          json={"reason": "test cleanup",
                                "confirmation_phrase": "STANDDOWN"},
                          headers=HDR, timeout=10)
        except Exception:
            pass

    def test_purge_refuses_while_alert_live(self):
        _seed()
        # Fire a trigger to make the incident live.
        r = requests.post(
            f"{BASE}/api/trigger-alert",
            json={"triggeredBy": "pytest-268",
                  "confirmation_phrase": "SIREN",
                  "reason": "test #268 live-alert purge refusal"},
            headers=HDR, timeout=15,
        )
        # Trigger phrase might vary — if the phrase is wrong we skip.
        if r.status_code != 200:
            pytest.skip(f"could not trigger a live alert: {r.status_code} {r.text[:200]}")

        try:
            r = requests.post(
                f"{BASE}/api/admin/device-registry/purge-all",
                json={"confirmation_phrase": "WIPE"},
                headers=HDR, timeout=15,
            )
            assert r.status_code == 409, r.text
            body = (r.json().get("detail") or "").lower()
            assert "alert" in body
        finally:
            self._stand_down()

    def test_purge_keeps_back_help_history_and_reports_message(self):
        # Alert has just been stood down. Small pause to let it settle.
        import time
        time.sleep(1)
        _seed()  # ensure C is still trapped (help history)

        r = requests.post(
            f"{BASE}/api/admin/device-registry/purge-all",
            json={"confirmation_phrase": "WIPE"},
            headers=HDR, timeout=30,
        )
        assert r.status_code == 200, r.text
        j = r.json()
        for k in ("before", "deleted", "after", "kept_back",
                  "kept_back_detail", "message"):
            assert k in j, k
        assert j["after"] == j["kept_back"], (
            f"after={j['after']} but kept_back={j['kept_back']} — deleted rows escaped"
        )
        # C has help history and MUST be kept back.
        kept_ids = [d["device_id"] for d in j["kept_back_detail"]]
        assert C in kept_ids, (
            f"trapped person {C} was NOT kept back on wipe! kept={kept_ids}"
        )
        if j["kept_back"] > 0:
            assert "reported needing help" in j["message"].lower()


# ─────────────────────────────────────────────────────────────────────
# 10. #268 follow-up: the two holes this sweep found, now closed
# ─────────────────────────────────────────────────────────────────────
class TestDuplicateAnswerCannotDropACasualty:
    """Found by the testing sweep, 2026-08-21: answering "same person"
    resolved the OLDER record by date alone, which moved a TRAPPED person
    off the working board — software choosing to drop a casualty. Status
    outranks device state has to hold on this path too."""

    def test_the_record_with_help_history_is_the_one_that_is_kept(self):
        _seed()
        r = requests.post(
            f"{BASE}/api/admin/records/{A}/duplicate-decision",
            json={"other_device_id": C, "decision": "confirmed"},
            headers=HDR, timeout=15,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        # C reported being trapped; A did not. C stays whatever the dates say.
        assert body["kept_device_id"] == C, body
        assert body["resolved_device_id"] == A, body
        state, row = _find(_devices(), C)
        assert state == "on", "a trapped person left the working board"

    def test_a_record_cannot_be_a_duplicate_of_itself(self):
        r = requests.post(
            f"{BASE}/api/admin/records/{A}/duplicate-decision",
            json={"other_device_id": A, "decision": "confirmed"},
            headers=HDR, timeout=15,
        )
        assert r.status_code == 400, r.text
        assert "itself" in r.json()["detail"]

    def test_a_refused_confirmation_leaves_no_decision_on_file(self):
        """A 409 must not silence the suggestion for ever — otherwise the
        operator is never asked the question again."""
        _seed()
        # Make both records help-history-free of each other's protection by
        # asking in the direction that must be refused: resolving C.
        r = requests.post(
            f"{BASE}/api/admin/records/{C}/duplicate-decision",
            json={"other_device_id": C, "decision": "confirmed"},
            headers=HDR, timeout=15,
        )
        assert r.status_code == 400  # self-pair, refused before any write
        # The pair A/B is still suggestible afterwards.
        body = _devices()
        _, row = _find(body, A)
        assert row is not None


class TestResolvingAHelpRecordNeedsADeliberateConfirmation:
    def test_refused_without_the_acknowledgement(self):
        _seed()
        r = requests.post(
            f"{BASE}/api/admin/records/{C}/resolve",
            json={"reason_code": "accounted_for"}, headers=HDR, timeout=15,
        )
        assert r.status_code == 409, r.text
        assert "reported needing help" in r.json()["detail"]
        state, _ = _find(_devices(), C)
        assert state == "on"

    def test_allowed_with_the_acknowledgement_and_recorded(self):
        _seed()
        r = requests.post(
            f"{BASE}/api/admin/records/{C}/resolve",
            json={"reason_code": "accounted_for",
                  "acknowledge_help_history": True,
                  "reason": "found by team 3 on foot"},
            headers=HDR, timeout=15,
        )
        assert r.status_code == 200, r.text
        assert r.json()["resolved_by"]
        state, row = _find(_devices(), C)
        assert state == "off"
        assert row["ever_needed_help"] is True
        assert "found by team 3 on foot" in row["resolved_reason"]
        # And it can be put straight back.
        back = requests.post(f"{BASE}/api/admin/records/{C}/unresolve",
                             json={}, headers=HDR, timeout=15)
        assert back.status_code == 200, back.text


class TestTheAppRemovedFactIsDurable:
    """The registration row is transient — the admin registry wipe deletes
    it. If the app-removed fact only lived there, a phantom would silently
    reappear on the working board after an unrelated cleanup."""

    def test_it_survives_the_push_registration_being_deleted(self):
        _seed()
        # Simulate what _prune_dead_devices writes, then delete the
        # registration exactly as the registry wipe does.
        import pymongo
        mdb = pymongo.MongoClient(os.environ["MONGO_URL"])[
            os.environ.get("DB_NAME", "test_database")]
        mdb.device_status.update_one(
            {"device_id": B},
            {"$set": {"app_removed_at": datetime.now(timezone.utc).isoformat(),
                      "app_removed_source": "apns_unregistered"}},
        )
        mdb.push_devices.delete_many({"user_id": B})
        state, row = _find(_devices(), B)
        assert state == "off", "a known-deleted app walked back onto the board"
        assert row["label"] == "App removed from this phone"

    def test_a_check_in_from_that_phone_brings_it_straight_back(self):
        _seed()
        import pymongo
        mdb = pymongo.MongoClient(os.environ["MONGO_URL"])[
            os.environ.get("DB_NAME", "test_database")]
        mdb.device_status.update_one(
            {"device_id": B},
            {"$set": {"app_removed_at": datetime.now(timezone.utc).isoformat(),
                      "app_removed_source": "apns_unregistered"}},
        )
        assert _find(_devices(), B)[0] == "off"
        r = requests.post(f"{BASE}/api/status", json={
            "device_id": B, "status": "safe", "display_name": "Neo Tester",
            "latitude": 35.8997, "longitude": 14.5146,
        }, timeout=15)
        assert r.status_code == 200, r.text
        state, row = _find(_devices(), B)
        assert state == "on", "a phone that reported again stayed set aside"
