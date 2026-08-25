"""#283 — a wrong number on a life-safety screen is a false fact.

Paul, live on 2026-08-24, found the same defect in three places:

  1. The call-off toast collapsed real people and test entries into one
     number ("13", then "14", when there was one real person and thirteen
     test entries) — while the confirm dialog beside it split them
     correctly.
  2. The faint sentence under the dashboard's stat boxes never matched the
     box next to it ("Waiting for an answer: 44" beside a "Not responding"
     box reading 18), because the sentence used the server's counts (which
     included test entries) and the box was recalculated in JavaScript
     from the rows on screen (which did not).
  3. The team PDF printed "Not responding: 1" and then, directly
     underneath, a breakdown reading 0 + 1 + 6.

One cause: four different places doing their own arithmetic. These tests
hold the fix — every number comes from `people_counts`, and anything
describing a population states which population.
"""
import os

import pytest
import requests

BASE_URL = "http://localhost:8001"
ADMIN = os.environ["ADMIN_TRIGGER_PASSWORD"]
HDR = {"X-Admin-Token": ADMIN}
DASHBOARD = "/app/memory/dashboard_build/index.html"


@pytest.fixture(scope="module")
def devices_payload():
    r = requests.get(f"{BASE_URL}/api/devices", headers=HDR, timeout=60)
    assert r.status_code == 200, r.text
    return r.json()


class TestBothPopulationsAreServed:
    def test_the_payload_carries_both_count_sets(self, devices_payload):
        for key in ("counts", "counts_without_test",
                    "count_notes", "count_notes_without_test"):
            assert key in devices_payload, key
        assert devices_payload["counts"]["include_test"] is True
        assert devices_payload["counts_without_test"]["include_test"] is False

    def test_the_real_only_set_is_never_larger(self, devices_payload):
        c = devices_payload["counts"]
        cw = devices_payload["counts_without_test"]
        for k in ("total", "safe", "trapped", "rescued", "not_responding",
                  "unknown", "walking_wounded", "waiting_for_answer",
                  "no_answer", "phone_went_dark"):
            assert cw[k] <= c[k], f"{k}: real-only {cw[k]} > with-test {c[k]}"

    def test_the_real_only_set_says_how_many_it_left_out(self, devices_payload):
        c = devices_payload["counts"]
        cw = devices_payload["counts_without_test"]
        assert cw["test_filtered_out"] == c["total"] - cw["total"] + (
            c["off_board_total"] - cw["off_board_total"])

    def test_walking_wounded_is_counted_by_the_server(self, devices_payload):
        # The board used to compute this in JavaScript from the rows it
        # happened to be showing.
        assert "walking_wounded" in devices_payload["counts"]
        # And they are inside the trapped total, not beside it.
        assert (devices_payload["counts"]["walking_wounded"]
                <= devices_payload["counts"]["trapped"])

    def test_each_set_adds_up(self, devices_payload):
        for key in ("counts", "counts_without_test"):
            c = devices_payload[key]
            assert (c["safe"] + c["trapped"] + c["rescued"]
                    + c["not_responding"] + c["unknown"]) == c["total"], key
            assert (c["trapped_red"] + c["trapped_yellow"] + c["trapped_green"]
                    + c["trapped_unknown"]) == c["trapped"], key


class TestTheSentencesMatchTheNumbers:
    def test_the_quiet_sentence_names_the_population_it_sits_in(self, devices_payload):
        c = devices_payload["counts_without_test"]
        notes = " ".join(devices_payload["count_notes_without_test"])
        quiet = c["waiting_for_answer"] + c["no_answer"] + c["phone_went_dark"]
        if quiet:
            assert f"Gone quiet: {quiet} of the {c['total']}" in notes, notes
            assert "not extra people" in notes, notes
        # The old claim was flatly wrong and must not come back.
        assert "All three are counted in the numbers above" not in notes

    def test_the_not_responding_sentence_matches_the_box(self, devices_payload):
        c = devices_payload["counts_without_test"]
        notes = devices_payload["count_notes_without_test"]
        head = notes[0]
        assert head.startswith(f"Not responding: {c['not_responding']} "), head


class TestTheBoardDoesNoArithmetic:
    @pytest.fixture(scope="class")
    def src(self):
        with open(DASHBOARD) as f:
            return f.read()

    def test_the_javascript_count_function_is_gone(self, src):
        assert "function computeCounts(" not in src, (
            "the board is counting people again — it must read the server's "
            "numbers, or the box and the sentence beside it will disagree"
        )

    def test_the_pills_are_a_translation_not_a_sum(self, src):
        assert "function pillsFromServerCounts(" in src
        block = src.split("function pillsFromServerCounts(")[1][:700]
        assert "+= 1" not in block and "forEach" not in block, (
            "no counting in the pill mapper — names only"
        )

    def test_the_board_picks_a_population_rather_than_filtering(self, src):
        assert "data.counts_without_test" in src
        assert "lastCounts = showTestEntries" in src


class TestTheCallOffSaysWhatIsReal:
    def test_the_preview_and_the_result_use_the_same_split(self):
        r = requests.get(f"{BASE_URL}/api/admin/alert/stand-down/preview",
                         headers=HDR, timeout=60)
        assert r.status_code == 200, r.text
        prev = r.json()
        for k in ("clearing_real_count", "clearing_test_count",
                  "staying_real_count", "staying_test_count"):
            assert k in prev, k
        assert (prev["clearing_real_count"] + prev["clearing_test_count"]
                == prev["clearing_count"])
        assert (prev["staying_real_count"] + prev["staying_test_count"]
                == prev["staying_count"])

    def test_the_toast_reads_the_real_count(self):
        with open(DASHBOARD) as f:
            src = f.read()
        assert "data.kept_on_board_real_count" in src
        assert "data.cleared_real_count" in src
        assert "they are not people" in src


class TestThePdfBreakdownCannotBeReadAsASum:
    def test_the_heading_names_the_total_and_denies_it_is_extra(self):
        with open("/app/backend/reports_export.py") as f:
            src = f.read()
        assert 'f"Gone quiet — {_quiet} of the {current_counts.total} above, "' in src
        assert '"not extra people:"' in src
        assert '"Silence right now — inside the rows above:"' not in src, (
            "the old heading read as a breakdown of the line above it"
        )
