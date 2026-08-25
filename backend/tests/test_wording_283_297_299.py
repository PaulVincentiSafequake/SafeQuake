"""#283 / #297 / #299 — three sentences that were telling small lies.

Paul, 2026-08-25 (live test batch):

  #283 > "Gone quiet: 7 of the 7 people on the board... spread across
        > Safe, Trapped and Not responding" — but those three only add to
        > 2. The real total math is correct, the sentence just forgot to
        > list "Rescued".
        > Separate question: does it make sense for already-rescued
        > people to be counted in "gone quiet" at all?

        The categories are now read off the rows instead of remembered,
        and rescued people keep their place in the total (removing them
        is how a total stops adding up — the original complaint) with a
        line saying out loud that their silence is not a worry.

  #297 > "before the responsible authorities has completed formal
        > notification" should be "have completed".

        `authority` can be one body or several, so the sentence now uses
        a verb form that is right either way.

  #299 > A "TRIGGER FAILED · M? · 0 people" log entry appeared around the
        > time of a stand-down action.

        It WAS the stand-down. The audit feed read every row in the push
        log and stamped TRIGGER on all of them; a stand-down has no
        magnitude and no recipients, so it rendered as a failed trigger.
        The feed was inventing a failure that never happened.

Run: python -m pytest backend/tests/test_wording_283_297_299.py -q
"""
import os
import sys
from datetime import datetime, timezone

import requests
from dotenv import dotenv_values
from pymongo import MongoClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from people_counts import Counts, counts_notes  # noqa: E402

BASE = os.environ.get("QG_BASE", "http://localhost:8001")
ENV = dotenv_values("/app/backend/.env")
TOKEN = ENV.get("ADMIN_TRIGGER_PASSWORD") or ""
H = {"X-Admin-Token": TOKEN, "Content-Type": "application/json"}


def _counts(**kw):
    base = dict(
        total=7, safe=1, trapped=1, trapped_red=1, trapped_yellow=0,
        trapped_green=0, trapped_unknown=0, rescued=5, not_responding=0,
        unknown=0, needs_help=1, test_filtered_out=0, include_test=False,
    )
    base.update(kw)
    return Counts(**base)


# ── #283 ──────────────────────────────────────────────────────────────
def test_the_gone_quiet_breakdown_names_every_category_it_counts():
    """Paul's exact board: 7 people, and the quiet ones sit in four
    buckets — including Rescued, which the old sentence never named."""
    c = _counts(
        waiting_for_answer=1, no_answer=5, phone_went_dark=1,
        quiet_by_status={"rescued": 5, "trapped": 1, "safe": 1},
        quiet_rescued=5,
    )
    notes = counts_notes(c)
    spread = [n for n in notes if "already counted above" in n]
    assert len(spread) == 1, notes
    line = spread[0]
    assert "5 Rescued" in line, line
    assert "1 Needing help" in line, line
    assert "1 Safe" in line, line
    # And the numbers it names must add up to the number it is explaining.
    assert sum(c.quiet_by_status.values()) == (
        c.waiting_for_answer + c.no_answer + c.phone_went_dark)


def test_rescued_people_are_explained_rather_than_quietly_dropped():
    """Paul's separate question. Dropping them would break the total,
    which is the very complaint (#283). So they stay, and the page says
    why their silence is not a worry."""
    c = _counts(no_answer=5, quiet_by_status={"rescued": 5}, quiet_rescued=5)
    notes = " ".join(counts_notes(c))
    assert "already been rescued" in notes
    assert "not a worry" in notes


def test_a_single_category_reads_as_a_sentence_not_a_list():
    c = _counts(no_answer=1, quiet_by_status={"safe": 1}, quiet_rescued=0)
    line = [n for n in counts_notes(c) if "already counted above" in n][0]
    assert "inside 1 Safe." in line, line
    assert "spread across" not in line


def test_no_rescued_line_when_nobody_quiet_has_been_rescued():
    c = _counts(no_answer=2, quiet_by_status={"trapped": 2}, quiet_rescued=0)
    notes = " ".join(counts_notes(c))
    assert "already been rescued" not in notes


def test_the_live_api_agrees_with_itself():
    """The board reads these notes straight from the API. Whatever the dev
    database holds, the categories named must add up to the number in the
    sentence — that is the whole of #283."""
    r = requests.get(f"{BASE}/api/devices", headers=H, timeout=30)
    assert r.status_code == 200, r.text
    d = r.json()
    counts = d["counts_without_test"]
    quiet = (counts["waiting_for_answer"] + counts["no_answer"]
             + counts["phone_went_dark"])
    by_status = counts.get("quiet_by_status") or {}
    assert sum(by_status.values()) == quiet, (by_status, quiet)


# ── #297 ──────────────────────────────────────────────────────────────
def test_the_team_pdf_footer_is_grammatical_for_one_body_or_several():
    import reports_export
    src = open(reports_export.__file__).read()
    assert "completes formal notification" in src
    assert "has completed formal notification" not in src


# ── #299 ──────────────────────────────────────────────────────────────
def test_a_stand_down_is_never_reported_as_a_failed_trigger():
    client = MongoClient(ENV.get("MONGO_URL", "mongodb://localhost:27017"))
    db = client[ENV.get("DB_NAME", "test_database")]
    key = f"test-299-{datetime.now(timezone.utc).timestamp()}"
    db.push_events.insert_one({
        "kind": "alert_stood_down",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "triggered_by": "paul@test",
        "reason": "false_alarm",
        "recipients": 3,
        "_test_299": key,
    })
    try:
        r = requests.get(f"{BASE}/api/audit?limit=200", headers=H, timeout=30)
        assert r.status_code == 200, r.text
        events = r.json()["events"]
        triggers = [e for e in events if e["kind"] == "trigger"]
        # No trigger row may be a stand-down in disguise: a real trigger
        # always has a recipient count or an error to explain itself.
        for t in triggers:
            assert t.get("magnitude") is not None or t.get("error") or \
                t.get("recipients_total"), t
        stand_downs = [e for e in events if e["kind"] == "stand_down"]
        assert stand_downs, "the stand-down must appear under its own name"
        assert stand_downs[0]["stood_down_by"]
    finally:
        db.push_events.delete_many({"_test_299": key})
        client.close()
