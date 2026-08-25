"""#291 — stop printing a fact we do not have.

Paul, live on 2026-08-24, found "Phone went dark" in four user-facing
places, including on two RESCUED people's map cards, where it reads as an
emergency about somebody already found.

The state itself was already honest — DARK means "we asked, nobody
answered, and their phone never confirmed our question arrived" — but the
LABEL asserted the phone had died, which is exactly the claim #271 said we
must never make. Worse, a BROADCAST alert counted as "asking", so 45
minutes after any test alert every phone that had not checked in read as
dark, rescued people included.

What this locks in:
  * the DARK label no longer says anything about the phone;
  * when the only thing that asked was a broadcast, the label says so and
    points at the next step ("Ask them to check in");
  * the STATE is unchanged either way — the counts and the alarms behave
    exactly as before. This is about the words, not the doctrine.
"""
from datetime import datetime, timedelta, timezone

import pytest

import record_state as rs

NOW = datetime(2026, 8, 24, 14, 0, tzinfo=timezone.utc)
ALERT_AT = NOW - timedelta(hours=2)
LAST_REPORT = NOW - timedelta(hours=3)


def _classify(row, **kw):
    kw.setdefault("push_row", None)
    kw.setdefault("ever_needed_help", False)
    kw.setdefault("ever_located", True)
    kw.setdefault("incident_active", False)
    kw.setdefault("last_alert_at", ALERT_AT)
    kw.setdefault("now", NOW)
    return rs.classify(row, **kw)


class TestTheWordsAreGone:
    def test_no_label_claims_a_phone_went_dark(self):
        assert "went dark" not in " ".join(rs.LABELS.values()).lower()

    def test_the_dark_label_describes_what_we_did(self):
        assert rs.LABELS[rs.DARK] == "We asked, no answer"


class TestBroadcastOnly:
    """An alert went to everyone. Nobody asked THIS person."""
    def test_the_label_names_the_alert_and_the_time(self):
        st = _classify({"device_id": "qg-1755600000000-abcdef12",
                        "status": "rescued", "rescued_at": LAST_REPORT.isoformat(),
                        "updated_at": LAST_REPORT.isoformat()})
        assert st.state == rs.DARK
        assert st.label.startswith("Not asked since the alert at "), st.label
        assert "went dark" not in st.label.lower()

    def test_the_detail_points_at_the_next_step(self):
        st = _classify({"device_id": "qg-1755600000000-abcdef12",
                        "status": "safe",
                        "updated_at": LAST_REPORT.isoformat()})
        assert st.state == rs.DARK
        assert "Nobody has asked this person on their own since." in st.detail
        assert "Ask them to check in to find out." in st.detail

    def test_the_state_is_unchanged_so_counts_and_alarms_are_too(self):
        # The fix must not quietly move anyone out of a bucket or silence
        # an alarm — that would trade a wording bug for a safety bug.
        st = _classify({"device_id": "qg-1755600000000-abcdef12",
                        "status": "trapped",
                        "updated_at": LAST_REPORT.isoformat()},
                       ever_needed_help=True)
        assert st.state == rs.DARK
        assert st.on_working_board is True
        assert st.count_in_status_buckets is not False


class TestWeActuallyAskedThem:
    def test_a_direct_ask_keeps_the_plain_label(self):
        st = _classify({
            "device_id": "qg-1755600000000-abcdef12",
            "status": "trapped",
            "updated_at": LAST_REPORT.isoformat(),
            "asks": {"last_at": (NOW - timedelta(hours=1)).isoformat(), "count": 1},
        }, ever_needed_help=True)
        assert st.state == rs.DARK
        assert st.label == "We asked, no answer"
        assert "We asked" in st.detail

    def test_a_confirmed_question_is_still_the_worrying_one(self):
        asked = NOW - timedelta(hours=1)
        st = _classify({
            "device_id": "qg-1755600000000-abcdef12",
            "status": "trapped",
            "updated_at": LAST_REPORT.isoformat(),
            "asks": {"last_at": asked.isoformat(), "count": 1,
                     "delivery": {"confirmed_at": asked.isoformat()}},
        }, ever_needed_help=True)
        assert st.state == rs.NO_ANSWER
        assert st.label == "Got our question, no answer"

    def test_a_missed_recheck_counts_as_a_direct_ask(self):
        st = _classify({
            "device_id": "qg-1755600000000-abcdef12",
            "status": "trapped",
            "updated_at": LAST_REPORT.isoformat(),
            "recheck": {"consecutive_missed": 2},
        }, ever_needed_help=True)
        assert st.state == rs.DARK
        assert st.label == "We asked, no answer"


class TestNothingAskedAtAll:
    def test_still_says_so(self):
        st = _classify({"device_id": "qg-1755600000000-abcdef12",
                        "status": "safe",
                        "updated_at": NOW.isoformat()},
                       last_alert_at=None)
        assert st.state == rs.NOT_ASKED
        assert st.label.startswith("Not asked since ")


class TestEveryExportAgrees:
    """The words travel with the numbers, everywhere they are printed."""
    def test_the_csv_row_name_no_longer_asserts_a_dead_phone(self):
        with open("/app/backend/reports_export.py") as f:
            src = f.read()
        assert '"asked_no_answer_delivery_not_confirmed"' in src
        assert 'writer.writerow(_pad(["phone_went_dark"' not in src

    def test_the_team_pdf_row_says_what_we_know(self):
        with open("/app/backend/reports_export.py") as f:
            src = f.read()
        assert "we asked, no answer, arrival not confirmed" in src

    def test_the_spoken_note_says_what_we_know(self):
        from people_counts import Counts, counts_notes
        c = Counts(total=3, safe=3, trapped=0, trapped_red=0, trapped_yellow=0,
                   trapped_green=0, trapped_unknown=0, rescued=0,
                   not_responding=0, unknown=0, needs_help=0,
                   test_filtered_out=0, include_test=False,
                   phone_went_dark=2)
        notes = " ".join(counts_notes(c))
        assert "never confirmed" in notes
        assert "went dark" not in notes.lower()

    def test_no_user_facing_string_in_the_dashboard_says_it(self):
        with open("/app/memory/dashboard_build/index.html") as f:
            src = f.read()
        assert "went dark" not in src.lower()
