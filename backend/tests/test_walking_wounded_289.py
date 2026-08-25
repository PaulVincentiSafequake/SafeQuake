"""#289 / #290 — the walking wounded report that never reached the board.

Paul, live on 2026-08-24:

  * a real "I can walk, not badly hurt, can get out on my own" report
    "didn't appear anywhere on the board on first submission";
  * an old "IMMEDIATE, cannot move" alarm stayed in the strip,
    acknowledged but unresolved, after the same person's real status had
    changed twice.

Causes, both found in code:

  1. NOTHING was sent until the follow-up question was answered. Choosing
     a severity opened a second sheet, and both sheets can be left — via
     Back, the system gesture, or simply putting the phone down — which
     lost the entire report. Both sheets even said "This does not delay
     your report" while being the thing delaying it.
  2. A person who chose MINOR and never answered the way-out question was
     filed as walking wounded, the LOWEST rescue priority, on an
     assumption. "We do not know" is not "they can get out".
  3. Alarms carried no trace of what had happened since they were raised,
     so the strip and the card contradicted each other.

What did NOT change, and why: an alarm still only clears when a human
clears it. A self-reported improvement must never quietly close a report
of a serious injury — adrenaline and shock make people wrong about that.
The alarm now shows what has happened since and asks for a decision.
"""
import pytest


ALERT_TSX = "/app/frontend/app/alert.tsx"
DASHBOARD = "/app/memory/dashboard_build/index.html"


@pytest.fixture(scope="module")
def alert_src():
    with open(ALERT_TSX) as f:
        return f.read()


class TestTheReportGoesFirst:
    def test_choosing_serious_sends_before_asking_anything_else(self, alert_src):
        block = alert_src.split('if (severity === "yellow") {')[1][:600]
        assert 'submitCheckIn("trapped", severity, null)' in block
        assert block.index('submitCheckIn') < block.index('setMobilityOpen(true)'), (
            "the report must be sent BEFORE the follow-up sheet opens"
        )

    def test_choosing_minor_sends_before_asking_anything_else(self, alert_src):
        block = alert_src.split('if (severity === "green") {')[1][:800]
        assert 'submitCheckIn("trapped", severity, "mobile", "not_answered")' in block
        assert block.index('submitCheckIn') < block.index('setEgressOpen(true)')

    def test_the_follow_up_answer_is_an_update_not_a_first_send(self, alert_src):
        assert 'submitCheckIn("trapped", sev, mobility, null, true)' in alert_src
        assert 'submitCheckIn("trapped", sev, "mobile", egress, true)' in alert_src
        # ...and the guard lets an update through.
        assert ('if (!isFollowUp && (status === "sending" || status === "sent")) return;'
                in alert_src)

    def test_escalating_to_immediate_always_gets_through(self, alert_src):
        assert 'submitCheckIn("trapped", severity, "trapped", null, true)' in alert_src

    def test_the_sheets_no_longer_promise_something_untrue(self, alert_src):
        # The old subtitle claimed the follow-up did not delay the report,
        # while being the only thing delaying it. (The phrase survives in
        # the comment explaining the fix, hence the JSX-only check.)
        assert "pinned. This does not delay your report" not in alert_src
        assert "counts as no. This does not delay your report" not in alert_src
        assert alert_src.count("Your report is already sent") == 2


class TestWeDoNotKnowIsNotWalkingWounded:
    def test_an_unanswered_way_out_question_stays_on_the_board(self):
        from people_counts import is_walking_wounded
        row = {"status": "trapped", "severity": "green", "mobility": "mobile",
               "egress": "not_answered"}
        assert is_walking_wounded(row) is False

    def test_a_positive_answer_is_walking_wounded(self):
        from people_counts import is_walking_wounded
        row = {"status": "trapped", "severity": "green", "mobility": "mobile",
               "egress": "can_exit"}
        assert is_walking_wounded(row) is True

    def test_cannot_get_out_is_never_walking_wounded(self):
        from people_counts import is_walking_wounded
        row = {"status": "trapped", "severity": "green", "mobility": "mobile",
               "egress": "cannot_exit", "needs_extraction": True}
        assert is_walking_wounded(row) is False

    def test_the_phone_may_say_not_answered(self):
        from server import StatusInPayload
        p = StatusInPayload(deviceId="qg-1755600000000-abcdef12", status="trapped",
                            severity="green", mobility="mobile",
                            egress="not_answered")
        assert p.egress == "not_answered"

    def test_a_made_up_answer_is_still_refused(self):
        from pydantic import ValidationError

        from server import StatusInPayload
        with pytest.raises(ValidationError):
            StatusInPayload(deviceId="qg-1755600000000-abcdef12", status="trapped",
                            egress="probably")


class TestAnAlarmSaysWhatHappenedSince:
    def test_a_status_report_stamps_the_open_alarms(self):
        import asyncio

        import board_alarms

        class _Col:
            def __init__(self): self.updates = []
            async def find_one(self, *a, **k): return None
            async def insert_one(self, doc): return None
            async def update_many(self, q, u):
                self.updates.append((q, u)); return None
            async def update_one(self, *a, **k): return None

        class _DB:
            def __init__(self): self.board_alarms = _Col()

        db = _DB()
        doc = {"device_id": "qg-1755600000000-abcdef12", "status": "trapped",
               "severity": "green", "mobility": "mobile", "egress": "can_exit",
               "display_name": "Sam", "short_code": "AB12C"}
        asyncio.run(board_alarms.on_status_change(db, {"status": "trapped",
                                                      "severity": "red"}, doc))
        assert db.board_alarms.updates, "no since-stamp was written"
        q, u = db.board_alarms.updates[-1]
        assert q["resolved_at"] is None
        assert q["kind"]["$in"] == [board_alarms.NEEDS_HELP, board_alarms.WORSE]
        words = u["$set"]["since_report"]["words"]
        assert "MINOR" in words and "can get out" in words

    def test_reporting_safe_is_recorded_but_does_not_clear_the_alarm(self):
        import asyncio

        import board_alarms

        class _Col:
            def __init__(self): self.updates = []; self.resolved = []
            async def find_one(self, *a, **k): return None
            async def insert_one(self, doc): return None
            async def update_many(self, q, u):
                # resolve_for_device also uses update_many; tell them apart
                if "resolved_at" in (u.get("$set") or {}):
                    self.resolved.append((q, u))
                else:
                    self.updates.append((q, u))
                return None
            async def update_one(self, *a, **k): return None

        class _DB:
            def __init__(self): self.board_alarms = _Col()

        db = _DB()
        doc = {"device_id": "qg-1755600000000-abcdef12", "status": "safe",
               "display_name": "Sam", "short_code": "AB12C"}
        asyncio.run(board_alarms.on_status_change(db, {"status": "trapped",
                                                      "severity": "red"}, doc))
        words = db.board_alarms.updates[-1][1]["$set"]["since_report"]["words"]
        assert words == "reported they are safe"
        # Only the gone-quiet alarm is resolved by a phone speaking again.
        for q, _ in db.board_alarms.resolved:
            assert board_alarms.GONE_QUIET in q["kind"]["$in"]
            assert board_alarms.NEEDS_HELP not in q["kind"]["$in"]


class TestTheBoardShowsIt:
    @pytest.fixture(scope="class")
    def src(self):
        with open(DASHBOARD) as f:
            return f.read()

    def test_the_alarm_row_prints_what_happened_since(self, src):
        assert "Since this alarm: " in src
        assert "This alarm still needs your decision." in src

    def test_the_names_underneath_carry_it_too(self, src):
        assert "p.since_report && p.since_report.words" in src


class TestTheListAndTheCountUseTheSameRule:
    def test_the_board_excludes_an_unknown_way_out_from_walking_wounded(self):
        with open(DASHBOARD) as f:
            src = f.read()
        assert 'u.egress === "not_answered"' in src
        block = src.split("var walking = minor.filter(")[1][:260]
        assert "wayOutUnknown(u)" in block, (
            "the walking wounded LIST must apply the same rule as the count"
        )
