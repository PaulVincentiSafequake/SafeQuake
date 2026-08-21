"""#268 — the phantom-casualty tests.

The defect: the rescue board showed `F6XJY` — an old install that no
longer exists on any phone — as "Not responding · Phone dark since
21:08". To an operator that is a missing person with a last known
position. It is a deleted app. A team gets dispatched to find nobody
while a real missing person waits.

Every test in this file pins one sentence of the doctrine Paul set out
on 2026-08-21. They are pure unit tests on purpose — no Mongo, no HTTP —
so they run in milliseconds and can never be skipped for environment
reasons in a regression sweep.
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import record_state as rs                      # noqa: E402
from duplicates import find_duplicate_candidates  # noqa: E402
from people_counts import _bucket, counts_notes   # noqa: E402

NOW = datetime(2026, 8, 21, 22, 0, tzinfo=timezone.utc)


def ago(minutes):
    return (NOW - timedelta(minutes=minutes)).isoformat()


def push(reason=None, at_min=30, **kw):
    row = {"user_id": "x", "created_at": ago(10000), **kw}
    if reason:
        row.update({"dead_token": True, "dead_token_reason": reason,
                    "dead_token_at": ago(at_min)})
    return row


def cls(row, push_row=None, **kw):
    kw.setdefault("ever_needed_help", False)
    kw.setdefault("ever_located", True)
    kw.setdefault("incident_active", False)
    return rs.classify(row, push_row, now=NOW, **kw)


# ── 1. Status outranks device state. The guarantee. ────────────────────
class TestStatusOutranksDeviceState:
    def test_trapped_person_whose_app_is_removed_stays_on_the_board(self):
        st = cls(
            {"status": "trapped", "updated_at": ago(200), "trapped_since": ago(400)},
            push("Unregistered"),
            ever_needed_help=True,
        )
        assert st.on_working_board is True
        assert st.state != rs.APP_REMOVED
        assert "reported needing help" in st.held_reason

    def test_rescued_person_whose_app_is_removed_stays_on_the_board(self):
        st = cls({"status": "safe", "rescued_at": ago(100), "updated_at": ago(300)},
                 push("Unregistered"), ever_needed_help=True)
        assert st.on_working_board is True

    def test_help_history_is_read_from_the_row_as_well_as_the_ledger(self):
        # The live row can be overwritten; both sources must count.
        assert rs.ever_needed_help_row({"status": "trapped"}) is True
        assert rs.ever_needed_help_row({"trapped_since": ago(10)}) is True
        assert rs.ever_needed_help_row({"needs_extraction": True}) is True
        assert rs.ever_needed_help_row({"rescued_at": ago(10)}) is True
        assert rs.ever_needed_help_row({"status": "safe"}) is False


# ── 2. A token dying mid-incident must not move anybody. ──────────────
class TestLiveIncidentHoldsEveryone:
    def test_app_removed_during_a_live_alert_does_not_move_the_record(self):
        st = cls({"status": "safe", "updated_at": ago(300)},
                 push("Unregistered"), incident_active=True)
        assert st.on_working_board is True
        assert "an alert is live" in st.held_reason
        assert "not a report that they are safe" in st.held_reason

    def test_same_record_moves_off_only_once_the_alert_is_stood_down(self):
        st = cls({"status": "safe", "updated_at": ago(300)},
                 push("Unregistered"), incident_active=False)
        assert st.on_working_board is False
        assert st.state == rs.APP_REMOVED

    def test_a_held_record_is_never_counted_as_not_responding(self):
        # Paul's rule 1 (do not move them) and rule 4 (never count a
        # removed device as not responding) must BOTH hold at once.
        st = cls({"status": "safe", "updated_at": ago(300)},
                 push("Unregistered"), incident_active=True)
        assert st.on_working_board is True
        assert st.count_in_status_buckets is False
        assert "not as a person who is not responding" in st.held_reason

    def test_a_trapped_person_is_still_counted_as_trapped(self):
        # Status outranks device state in the COUNTS too, not just the board.
        st = cls({"status": "trapped", "updated_at": ago(300)},
                 push("Unregistered"), ever_needed_help=True, incident_active=True)
        assert st.on_working_board is True
        assert st.count_in_status_buckets is True


# ── 3. Only Apple's `Unregistered` may claim the app was removed. ─────
class TestOnlyUnregisteredMeansRemoved:
    def test_bad_device_token_reads_as_phone_went_dark(self):
        st = cls({"status": "safe", "updated_at": ago(300)}, push("BadDeviceToken"))
        assert st.state == rs.DARK
        assert st.on_working_board is True
        assert "not proof the app was removed" in st.token_note

    def test_destroyed_phone_produces_no_removal_signal_at_all(self):
        # No APNs reason at all — the safe default is "went dark".
        st = cls({"status": "safe", "updated_at": ago(300)}, push())
        assert st.state == rs.DARK
        assert st.app_removed_at is None

    def test_apns_module_and_classifier_agree_on_the_reason_string(self):
        import apns
        assert apns.APP_REMOVED_REASON == rs.APP_REMOVED_REASON == "Unregistered"


# ── 4. Two thresholds, and the real elapsed time always shown. ────────
class TestDarkThresholds:
    def test_fifteen_minutes_for_someone_who_needed_help(self):
        st = cls({"status": "trapped", "updated_at": ago(20)}, ever_needed_help=True)
        assert st.dark_after_minutes == rs.DARK_AFTER_MINUTES_NEEDS_HELP == 15
        assert st.state == rs.DARK

    def test_forty_five_minutes_for_everyone_else(self):
        st = cls({"status": "safe", "updated_at": ago(20)})
        assert st.dark_after_minutes == 45
        assert st.state is None
        assert cls({"status": "safe", "updated_at": ago(50)}).state == rs.DARK

    def test_the_card_always_carries_the_actual_elapsed_time(self):
        st = cls({"status": "safe", "updated_at": ago(72)})
        assert st.silent_minutes == 72
        assert "1 hour 12 minutes" in st.detail


# ── 5. Never used the app, and the never-demote exception. ───────────
class TestNeverUsed:
    def test_registered_but_never_opened(self):
        st = cls(None, push(), ever_located=False)
        assert st.state == rs.NEVER_USED
        assert st.on_working_board is False
        assert "no position has ever been recorded" in st.detail

    def test_someone_ever_located_is_never_demoted_to_never_used(self):
        st = cls({"status": None, "updated_at": ago(500)}, push(), ever_located=True)
        assert st.state != rs.NEVER_USED
        assert st.on_working_board is True


# ── 6. A human resolution, and nothing else, takes a record off. ─────
class TestHumanResolution:
    def test_resolved_record_states_who_and_why(self):
        st = cls({
            "status": "safe", "updated_at": ago(300),
            "resolved_at": ago(5), "resolved_by": "paul@quakeangel.app",
            "resolved_reason": "Same person as another record — confirmed as CW7EF",
        })
        assert st.state == rs.RESOLVED
        assert st.on_working_board is False
        assert "paul@quakeangel.app" in st.detail
        assert "Nothing has been deleted" in st.detail


# ── 7. Labels an operator reads aloud at 4am. ─────────────────────────
class TestLabelsSurviveBeingSpoken:
    def test_four_labels_are_plain_english_and_distinct(self):
        labels = [rs.LABELS[k] for k in
                  (rs.WAITING, rs.DARK, rs.APP_REMOVED, rs.NEVER_USED)]
        assert labels == ["Waiting for an answer", "Phone went dark",
                          "App removed from this phone", "Never used the app"]
        firsts = [l.split()[0].lower() for l in labels]
        assert len(set(firsts)) == 4, "two labels start with the same word"

    def test_no_jargon_anywhere_in_the_labels_or_details(self):
        banned = ("not responding", "dead token", "unregistered", "token",
                  "apns", "device_status", "null")
        for state in (rs.WAITING, rs.DARK, rs.APP_REMOVED, rs.NEVER_USED):
            assert not any(b in rs.LABELS[state].lower() for b in banned)
        st = cls({"status": "safe", "updated_at": ago(300)}, push("Unregistered"))
        assert not any(b in st.detail.lower()
                       for b in ("not responding", "dead token", "apns"))


# ── 8. Mass-dark is a network failure, not many missing people. ──────
class TestMassDark:
    def test_cluster_is_reported_with_both_tests_passing(self):
        n = rs.detect_mass_dark([ago(m) for m in (60, 61, 63, 64, 66)], 8, now=NOW)
        assert n and n["count"] == 5
        assert "network or power failure" in n["text"]
        assert "Nobody has been moved or reclassified" in n["text"]

    def test_below_the_absolute_floor_says_nothing(self):
        assert rs.detect_mass_dark([ago(60), ago(61), ago(62)], 3, now=NOW) is None

    def test_below_the_share_threshold_says_nothing(self):
        # 5 dark out of 100 reporting — 5%, not an outage.
        assert rs.detect_mass_dark([ago(m) for m in (60, 61, 62, 63, 64)],
                                   100, now=NOW) is None

    def test_spread_out_over_hours_is_not_a_cluster(self):
        stamps = [ago(m) for m in (60, 200, 400, 700, 1000)]
        assert rs.detect_mass_dark(stamps, 6, now=NOW) is None


# ── 9. Duplicates are suggested with evidence, never merged. ─────────
class TestDuplicateSuggestions:
    def _pair(self):
        old = {"device_id": "old", "short_code": "F6XJY", "display_name": "Paul",
               "latitude": 35.8997, "longitude": 14.5146,
               "created_at": ago(600), "updated_at": ago(60)}
        new = {"device_id": "new", "short_code": "CW7EF", "display_name": "Paul",
               "latitude": 35.8998, "longitude": 14.5147,
               "created_at": ago(57), "updated_at": ago(1)}
        return old, new

    def test_reinstall_is_flagged_on_both_records_with_evidence(self):
        old, new = self._pair()
        flags = find_duplicate_candidates([old, new])
        assert set(flags) == {"old", "new"}
        assert flags["new"]["text"] == "This may be the same person as F6XJY."
        ev = " | ".join(flags["new"]["evidence"])
        assert "Same first name (Paul)" in ev
        assert "m apart" in ev
        assert "before" in ev

    def test_different_names_are_never_suggested_as_one_person(self):
        old, new = self._pair()
        new["display_name"] = "Anna"
        assert find_duplicate_candidates([old, new]) == {}

    def test_two_live_records_are_not_suggested(self):
        old, new = self._pair()
        old["updated_at"] = ago(1)       # the old phone is still reporting
        assert find_duplicate_candidates([old, new]) == {}

    def test_a_rejected_suggestion_does_not_come_back(self):
        old, new = self._pair()
        decisions = {"new": {"kind": "duplicate_rejected", "other_device_id": "old"}}
        assert find_duplicate_candidates([old, new], decisions) == {}

    def test_far_apart_and_long_after_is_not_suggested(self):
        old, new = self._pair()
        new["created_at"] = ago(5)       # 55 minutes after the old went quiet
        assert find_duplicate_candidates([old, new]) == {}


# ── 10. Counts never include a deleted app. ──────────────────────────
class TestCountsExcludeOffBoardRecords:
    def _rows(self):
        on = {"device_id": "qg-1755600000000-aaaaaaa1", "status": "not_responding",
              "record_state": {"on_working_board": True, "state": rs.DARK}}
        off = {"device_id": "qg-1755600000000-aaaaaaa2", "status": "not_responding",
               "record_state": {"on_working_board": False, "state": rs.APP_REMOVED}}
        return [on, off]

    def test_a_record_held_on_the_board_is_still_not_counted(self):
        held = {"device_id": "qg-1755600000000-aaaaaaa3",
                "status": "not_responding",
                "record_state": {"on_working_board": True, "state": rs.DARK,
                                 "count_in_status_buckets": False,
                                 "app_removed_at": "2026-08-21T21:08:00+00:00"}}
        c = _bucket(self._rows() + [held], include_test=False)
        assert c.not_responding == 1
        assert c.total == 1

    def test_a_removed_app_is_not_counted_as_not_responding(self):
        c = _bucket(self._rows(), include_test=False)
        assert c.not_responding == 1
        assert c.total == 1

    def test_every_number_states_what_it_leaves_out(self):
        c = _bucket(self._rows(), include_test=False)
        notes = counts_notes(c)
        joined = " ".join(notes)
        assert "does not include" in joined
        assert "app" in joined and "removed" in joined

    def test_the_note_names_the_removed_records_when_there_are_any(self):
        from dataclasses import replace
        c = replace(_bucket(self._rows(), include_test=False), app_removed=2,
                    never_used=1, resolved_by_operator=1)
        joined = " ".join(counts_notes(c))
        assert "deleted apps, not missing people" in joined
        assert "we have no location for them" in joined
        assert "resolved by an operator" in joined
