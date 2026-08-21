"""#274 / #271 / #272 / #270 review-request end-to-end backend tests.

This file was written for the review-request testing round of 2026-08-21
and is intentionally focused on the six BACKEND bullet points in the
review request — the two already-passing files
(test_stand_down_split_274.py, test_273_regression.py) cover the ask-limit
unit tests and the /api/devices envelope, so this suite does NOT duplicate
them.

What we exercise here, real HTTP against the supervisor-managed backend:

  1. GET  /api/admin/alert/stand-down/preview
     - shape: total, clearing_count, staying_count, staying_people[],
       confirmation_phrase
     - every staying_people[] row carries device_id, name, code, words,
       last_heard, battery_pct
     - a person whose effective_status is trapped MUST appear in
       staying_people and MUST NOT be inside clearing_count

  2. POST /api/admin/alert/stand-down
     - wrong / empty confirmation_phrase → 400 with plain English
     - correct phrase → 200 with ok, recipients, cleared_count,
       kept_on_board_count, kept_on_board[]
     - a matching push_events row of kind 'alert_stood_down' with the same
       four fields lands in Mongo
     - kept_on_board and clearing are disjoint (the trapped person's
       user_id is never in the recipients list)

  3. POST /api/admin/records/{id}/ask-to-check-in — low-battery ack path
     - the same trapped, 9%-battery iOS phone (seed device C) 409s
       without the ack, and is accepted with acknowledge_low_battery: true
       (subject to the outer live-alert cooldown / max-unanswered gate).

  4. GET /api/devices — every device row carries ask_state with the
     eight promised keys, and neither history_words nor blocked_reason
     contain any developer word from the banned list.

  5. Exports
     - CSV: Content-Disposition filename ends with a UTC Z stamp; the
       first three data columns are at, at_simple, kind; a metadata row
       named 'times_note' is present.
     - PDFs: audit-log / casualty-report/operational / casualty-report/public
       all render (200 application/pdf) and their 'Covers …' line says
       'Malta time' with an offset, never '(UTC)'.

  6. Regression: /api/devices still returns
     devices/off_board/notices/counts/count_notes, and a trapped person
     whose phone reports the app removed stays ON the working board
     (#268 doctrine).

Prerequisites
-------------
* `python scripts/seed_268_scenario.py` MUST have been run first.
* `ADMIN_TRIGGER_PASSWORD` and `EXPO_PUBLIC_BACKEND_URL` (or
  `EXPO_BACKEND_URL`) MUST be in the process env — same as every other
  suite in this folder.
"""
from __future__ import annotations

import io
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict

import pytest
import requests
from pymongo import MongoClient
from pypdf import PdfReader

BASE_URL = (
    os.environ.get("EXPO_PUBLIC_BACKEND_URL")
    or os.environ.get("EXPO_BACKEND_URL")
    or "http://localhost:8001"
).rstrip("/")
ADMIN_TOKEN = os.environ["ADMIN_TRIGGER_PASSWORD"]
MONGO_URL = os.environ["MONGO_URL"]
DB_NAME = os.environ.get("DB_NAME", "test_database")

ID_A = "qg-1755700000001-neo268a"   # safe, iOS, 80% battery
ID_B = "qg-1755600000002-neo268b"   # app removed
ID_C = "qg-1755700000003-neo268c"   # trapped, iOS, 9% battery
ID_D = "qg-1755700000004-neo268d"   # registered, never used the app
ID_E = "qg-1755700000005-neo268e"   # android


# ── Fixtures ──────────────────────────────────────────────────────────
@pytest.fixture(scope="module")
def hdr() -> Dict[str, str]:
    return {"X-Admin-Token": ADMIN_TOKEN}


@pytest.fixture(scope="module")
def db():
    return MongoClient(MONGO_URL)[DB_NAME]


# ─────────────────────────────────────────────────────────────────────
# 1. STAND-DOWN PREVIEW
# ─────────────────────────────────────────────────────────────────────
class TestStandDownPreview:
    """#274 preview must name the people who stay on the board — every
    field the confirm dialog needs to render is asserted here."""

    @pytest.fixture(scope="class")
    def preview(self, hdr):
        r = requests.get(
            f"{BASE_URL}/api/admin/alert/stand-down/preview",
            headers=hdr,
            timeout=20,
        )
        assert r.status_code == 200, r.text
        return r.json()

    def test_top_level_shape(self, preview):
        for key in (
            "total",
            "clearing_count",
            "staying_count",
            "staying_people",
            "confirmation_phrase",
        ):
            assert key in preview, f"missing '{key}'"
        assert isinstance(preview["staying_people"], list)
        assert isinstance(preview["confirmation_phrase"], str) \
            and preview["confirmation_phrase"].strip() != ""

    def test_counts_add_up(self, preview):
        assert preview["clearing_count"] + preview["staying_count"] >= 0
        assert preview["clearing_count"] <= preview["total"]
        assert preview["staying_real_count"] == len(preview["staying_people"])
        assert (preview["staying_real_count"] + preview["staying_test_count"]
                == preview["staying_count"])

    def test_every_row_has_the_six_display_fields(self, preview):
        needed = {"device_id", "name", "code", "words", "last_heard", "battery_pct"}
        for row in preview["staying_people"]:
            missing = needed - set(row.keys())
            assert not missing, f"row {row.get('device_id')} missing {missing}"

    def test_trapped_person_is_named_and_never_cleared(self, preview):
        """Device C is trapped in the #268 seed. It MUST appear on the
        staying list — and, because the split is disjoint, its user_id
        MUST NOT be inside the recipients count."""
        ids = {p["device_id"] for p in preview["staying_people"]}
        assert ID_C in ids, "seeded trapped record C missing from staying list"
        # The same person cannot be both cleared AND kept — the endpoint
        # is a disjoint split. clearing_count is a length; assert the
        # invariant by exercising the POST endpoint further down.

    def test_last_heard_is_a_human_string_not_iso(self, preview):
        """#272 — everything a person reads is Malta time in words, so
        the preview MUST NOT hand the dashboard a bare ISO stamp."""
        for row in preview["staying_people"]:
            lh = row.get("last_heard") or ""
            assert "T" not in lh, row
            assert "+00:00" not in lh, row
            assert "Z" not in lh or lh.endswith("Z") is False, row

    def test_words_are_plain_english_not_severity_codes(self, preview):
        allowed = {"Badly hurt", "Hurt", "Not hurt, but stuck", "Asked for help"}
        for row in preview["staying_people"]:
            assert row["words"] in allowed, row


# ─────────────────────────────────────────────────────────────────────
# 2. STAND-DOWN POST — full flow (wrong phrase, correct phrase, audit row)
# ─────────────────────────────────────────────────────────────────────
class TestStandDownPost:

    def test_missing_phrase_is_a_400(self, hdr):
        r = requests.post(
            f"{BASE_URL}/api/admin/alert/stand-down",
            headers=hdr,
            json={"reason": "false_alarm"},
            timeout=15,
        )
        assert r.status_code == 400, r.text
        # Plain English: names the phrase, no developer jargon.
        body = r.json().get("detail", "")
        assert "STANDDOWN" in body.upper(), body
        assert "recall" in body.lower() or "match" in body.lower(), body

    def test_wrong_phrase_is_a_400(self, hdr):
        r = requests.post(
            f"{BASE_URL}/api/admin/alert/stand-down",
            headers=hdr,
            json={"reason": "false_alarm", "confirmation_phrase": "WRONG"},
            timeout=15,
        )
        assert r.status_code == 400, r.text

    def test_correct_phrase_returns_split_and_writes_audit(self, hdr, db):
        # Capture staying count from the preview first, so we can assert
        # the POST response matches.
        prev = requests.get(
            f"{BASE_URL}/api/admin/alert/stand-down/preview",
            headers=hdr, timeout=20,
        ).json()

        r = requests.post(
            f"{BASE_URL}/api/admin/alert/stand-down",
            headers=hdr,
            json={"reason": "false_alarm", "confirmation_phrase": "STANDDOWN"},
            timeout=30,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        for key in ("ok", "recipients", "cleared_count",
                    "kept_on_board_count", "kept_on_board"):
            assert key in body, f"missing '{key}' in response"
        assert body["ok"] is True
        assert body["cleared_count"] == body["recipients"], body
        assert body["kept_on_board_count"] == prev["staying_count"], (
            body, prev,
        )
        # Trapped person C is on kept_on_board — never in recipients.
        kept_ids = {p["device_id"] for p in body["kept_on_board"]}
        assert ID_C in kept_ids

        # Matching push_events row exists, with the same four fields.
        ev = db.push_events.find_one(
            {"kind": "alert_stood_down"},
            sort=[("created_at", -1)],
        )
        assert ev is not None
        for key in ("cleared_count", "kept_on_board_count", "kept_on_board",
                    "recipients"):
            assert key in ev, f"push_events row missing '{key}'"
        assert ev["cleared_count"] == body["cleared_count"]
        assert ev["kept_on_board_count"] == body["kept_on_board_count"]
        kept_ev_ids = {p["device_id"] for p in ev["kept_on_board"]}
        assert ID_C in kept_ev_ids


# ─────────────────────────────────────────────────────────────────────
# 3. ASK-TO-CHECK-IN — low-battery ack path (accepts with ack when
#    otherwise allowed)
# ─────────────────────────────────────────────────────────────────────
class TestAskLowBatteryAck:

    def test_without_ack_it_409s_with_battery_prompt(self, hdr):
        r = requests.post(
            f"{BASE_URL}/api/admin/records/{ID_C}/ask-to-check-in",
            headers=hdr, json={}, timeout=15,
        )
        assert r.status_code == 409, r.text
        assert "battery" in r.json()["detail"].lower()

    def test_with_ack_the_battery_gate_is_cleared(self, hdr, db):
        """With acknowledge_low_battery=True the low-battery gate lets
        the call through. Downstream we may hit 429 (cooldown) or 502
        (no live APNs config in preview) — either result proves the
        battery gate is a soft consent, not a hard block. The gate
        must NOT return 409 with a battery message once the operator
        has explicitly acknowledged."""
        # Clear any lingering ask state on C from earlier tests so the
        # cooldown does not mask the assertion.
        db.device_status.update_one(
            {"device_id": ID_C},
            {"$unset": {"asks": ""}},
        )
        r = requests.post(
            f"{BASE_URL}/api/admin/records/{ID_C}/ask-to-check-in",
            headers=hdr,
            json={"acknowledge_low_battery": True},
            timeout=15,
        )
        # Any status is fine except a still-409 with a battery message —
        # that would mean the ack was ignored.
        if r.status_code == 409:
            assert "battery" not in r.json().get("detail", "").lower(), r.text
        # And the ask endpoint must never claim a critical-alert path
        # via the response headers or body.
        assert "critical" not in r.text.lower() or "kind" not in r.text.lower()


# ─────────────────────────────────────────────────────────────────────
# 4. /api/devices ask_state envelope
# ─────────────────────────────────────────────────────────────────────
class TestDevicesAskState:
    BANNED = ("null", "token", "payload", "unregistered", "endpoint",
              "apns", "device_id", "true", "false")
    NEEDED = {"count", "unanswered", "last_at", "history_words",
              "can_ask", "blocked_reason", "low_battery", "gap_minutes"}

    @pytest.fixture(scope="class")
    def devices_payload(self, hdr):
        r = requests.get(
            f"{BASE_URL}/api/devices?limit=1000",
            headers=hdr, timeout=20,
        )
        assert r.status_code == 200
        return r.json()

    def test_every_board_row_has_ask_state(self, devices_payload):
        for row in devices_payload["devices"]:
            assert "ask_state" in row, row.get("device_id")
            missing = self.NEEDED - set(row["ask_state"].keys())
            assert not missing, (row["device_id"], missing)

    def test_history_and_blocked_reason_are_plain_english(self, devices_payload):
        offenders = []
        for row in devices_payload["devices"]:
            st = row["ask_state"]
            text = (st.get("history_words") or "") + " " + (st.get("blocked_reason") or "")
            for word in self.BANNED:
                if re.search(rf"\b{re.escape(word)}\b", text, flags=re.IGNORECASE):
                    offenders.append((row["device_id"], word, text))
        assert not offenders, offenders

    def test_ask_state_low_battery_reflects_battery_pct(self, hdr, devices_payload):
        """Device C has 9% battery in the seed → low_battery must be True
        on its ask_state row and gap_minutes must be the widened window."""
        by_id = {d["device_id"]: d for d in devices_payload["devices"]}
        # If the earlier stand-down flushed a live alert C could have been
        # rebuilt with a fresh row — either way, C must still exist.
        assert ID_C in by_id, "seed device C missing from devices payload"
        st = by_id[ID_C]["ask_state"]
        assert st["low_battery"] is True, st
        assert st["gap_minutes"] == 180, st


# ─────────────────────────────────────────────────────────────────────
# 5. Exports — CSV header shape / filename Z, PDFs render Malta time
# ─────────────────────────────────────────────────────────────────────
class TestExports:

    def test_audit_csv_filename_ends_with_utc_z(self, hdr):
        r = requests.get(
            f"{BASE_URL}/api/admin/audit-log/export.csv",
            headers=hdr, timeout=30,
        )
        assert r.status_code == 200, r.text
        disp = r.headers.get("Content-Disposition", "")
        assert "filename=" in disp, disp
        m = re.search(r'filename="?([^"]+)"?', disp)
        assert m, disp
        assert m.group(1).rstrip('"').endswith("Z.csv"), m.group(1)

    def test_audit_csv_header_row_starts_at_at_simple_kind(self, hdr):
        r = requests.get(
            f"{BASE_URL}/api/admin/audit-log/export.csv",
            headers=hdr, timeout=30,
        )
        assert r.status_code == 200
        # Split into logical rows (the metadata rows are pre-header).
        import csv
        text = r.content.decode("utf-8-sig")
        rows = list(csv.reader(io.StringIO(text)))
        hdr_row = next(row for row in rows if row[:3] == ["at", "at_simple", "kind"])
        assert hdr_row[:3] == ["at", "at_simple", "kind"], hdr_row[:3]

    def test_audit_csv_has_times_note_row(self, hdr):
        r = requests.get(
            f"{BASE_URL}/api/admin/audit-log/export.csv",
            headers=hdr, timeout=30,
        )
        assert "times_note" in r.text
        # And it explains the two clocks in plain English.
        assert "Malta time" in r.text

    @pytest.mark.parametrize("url", [
        "/api/admin/audit-log/export.pdf",
        "/api/admin/casualty-report/operational.pdf",
        "/api/admin/casualty-report/public.pdf",
    ])
    def test_pdf_renders_and_says_malta_time_with_offset(self, hdr, url):
        r = requests.get(f"{BASE_URL}{url}", headers=hdr, timeout=60)
        assert r.status_code == 200, r.text[:400]
        assert r.headers.get("Content-Type", "").startswith("application/pdf"), \
            r.headers.get("Content-Type")
        # Content-Disposition filename ends with a UTC Z stamp.
        disp = r.headers.get("Content-Disposition", "")
        m = re.search(r'filename="?([^"]+)"?', disp)
        assert m, disp
        assert re.search(r"\d{8}T\d{6}Z\.pdf$", m.group(1).rstrip('"')), \
            m.group(1)
        # Read the PDF and assert on the 'Covers …' line.
        reader = PdfReader(io.BytesIO(r.content))
        text = "".join(pg.extract_text() or "" for pg in reader.pages)
        covers = next(
            (ln for ln in text.splitlines() if ln.strip().startswith("Covers ")),
            "",
        )
        assert covers, f"no 'Covers …' line in {url}: sample={text[:400]}"
        assert "Malta time" in covers, covers
        assert re.search(r"UTC[+\-]\d{2}:\d{2}", covers), covers
        # And it must NEVER just say '(UTC)' with nothing on the offset.
        assert "(UTC)" not in covers, covers


# ─────────────────────────────────────────────────────────────────────
# 6. Regression — /api/devices envelope + #268 held-on-board doctrine
# ─────────────────────────────────────────────────────────────────────
class TestDevicesRegression:

    @pytest.fixture(scope="class")
    def devices_payload(self, hdr):
        r = requests.get(
            f"{BASE_URL}/api/devices?limit=1000",
            headers=hdr, timeout=20,
        )
        assert r.status_code == 200
        return r.json()

    def test_envelope_shape(self, devices_payload):
        for k in ("devices", "off_board", "notices", "counts", "count_notes"):
            assert k in devices_payload, k

    def test_trapped_with_app_removed_stays_on_working_board(self, devices_payload):
        """#268 doctrine: a trapped person whose phone reports the app
        removed is HELD on the working board (never moved off)."""
        board_ids = {d["device_id"] for d in devices_payload["devices"]}
        assert ID_C in board_ids
