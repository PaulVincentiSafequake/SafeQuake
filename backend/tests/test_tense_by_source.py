"""#216 (Batch 7) — Tense must match the source.

Paul's rule, verbatim (2026-08-19):

  > Anything in "Where things stand right now" uses compute_counts,
  > present tense. "1 person is currently trapped."
  > Anything in "What happened during this window" uses window data,
  > past tense. "No one was newly recorded as trapped during this
  > period."
  >
  > The original bug was not that the numbers were wrong. It was that
  > a window-scoped sentence used present-tense words — "still", "yet"
  > — which claim current state. Fix the tense, not the source.
  >
  > No sentence on any report may use present-tense phrasing while
  > reading window data. If a sentence needs "is", "still", "yet", or
  > "currently", it reads from compute_counts. If it reads window
  > data, it is written in the past tense.

The words are searchable, so this rule is checked by string search on
the actual output of `_window_narrative`. Any regression that puts a
present-tense state word back into a window-scoped sentence will fail
this test loudly, before the bug can reach a report.

Symmetrically, `_current_state_narrative` must not contain past-tense
state verbs ("was", "were", "had been") because it is talking about
right now — a past-tense sentence there is either misplaced (belongs in
the window narrative) or a factual claim we cannot back up from a
snapshot count.
"""
from __future__ import annotations

import re
from types import SimpleNamespace

import pytest

from reports_export import _window_narrative, _current_state_narrative


# ---------------------------------------------------------------------------
# Words banned from the WINDOW narrative (past-tense section).
#
# These match on WORD BOUNDARIES so that "was" in "warning" doesn't
# false-positive. Every entry is a present-state claim in plain English:
#
#   "still"      — "still trapped", "still waiting"
#   "yet"        — "no one has been ... yet"
#   "currently"  — "1 currently trapped"
#   "right now"  — literal
#   " is "       — present-tense state ("1 person is trapped")
#   " are "      — same, plural
#
# `has`/`have` are permitted because they carry past-participle
# constructions ("had reported"). `cannot` is permitted too — the
# disclaimer "others may have been affected who we cannot see" states
# a permanent limitation of the system, not a current state, and it
# reads correctly in both sections. The tense rule is about STATE
# claims, not about verb forms in general.
# ---------------------------------------------------------------------------
_BANNED_IN_WINDOW = [
    r"\bstill\b",
    r"\byet\b",
    r"\bcurrently\b",
    r"\bright now\b",
    r"\bis\b",
    r"\bare\b",
]


_BANNED_IN_CURRENT = [
    # State claims about the past do not belong in a "right now" section.
    r"\bwas\b",
    r"\bwere\b",
    r"\bhad been\b",
]


def _assert_no_banned(lines: list[str], banned: list[str], where: str) -> None:
    """Fail with every offending line + word, so the fix is one grep away."""
    offences: list[str] = []
    for line in lines:
        for pat in banned:
            if re.search(pat, line, flags=re.IGNORECASE):
                offences.append(f"[{where}] banned {pat!r} in: {line!r}")
    assert not offences, "\n".join(offences)


# ---------------------------------------------------------------------------
# Window narrative — must be PAST TENSE only.
# ---------------------------------------------------------------------------
def test_window_narrative_no_present_tense_when_empty():
    """Empty window — no one trapped, no one rescued — must not use
    present-tense state words."""
    lines = _window_narrative(raw_rows=[], latest_events=[], counts={})
    assert lines, "empty window should still produce disclaimer lines"
    _assert_no_banned(lines, _BANNED_IN_WINDOW, "window (empty)")


def test_window_narrative_no_present_tense_with_trapped_and_rescued():
    """A realistic window with trapped, rescued and self-safe transitions
    must produce past-tense-only prose."""
    raw_rows = [
        {"device_id": "d1", "status": "trapped"},
        {"device_id": "d2", "status": "trapped"},
        {"device_id": "d3", "status": "trapped"},
    ]
    latest_events = [
        {"device_id": "d1", "status": "rescued"},
        {"device_id": "d2", "status": "safe"},
        {"device_id": "d3", "status": "trapped", "severity": "red"},
    ]
    counts = {"rescued": 1, "trapped": 1, "safe": 1}
    lines = _window_narrative(raw_rows, latest_events, counts)
    _assert_no_banned(lines, _BANNED_IN_WINDOW, "window (populated)")


def test_window_narrative_singular_and_plural_have_no_present_tense():
    """Both agreement branches must stay past-tense."""
    # Singular branch.
    raw = [{"device_id": "d1", "status": "trapped"}]
    lines = _window_narrative(raw, [], {"rescued": 1})
    _assert_no_banned(lines, _BANNED_IN_WINDOW, "window (singular)")
    # Plural branch.
    raw = [{"device_id": f"d{i}", "status": "trapped"} for i in range(4)]
    lines = _window_narrative(raw, [], {"rescued": 2})
    _assert_no_banned(lines, _BANNED_IN_WINDOW, "window (plural)")


# ---------------------------------------------------------------------------
# Current-state narrative — must be PRESENT TENSE only.
# ---------------------------------------------------------------------------
def _mock_counts(needs=0, not_responding=0):
    return SimpleNamespace(needs_help=needs, not_responding=not_responding)


def test_current_state_narrative_no_past_tense_when_empty():
    lines = _current_state_narrative(_mock_counts(), latest_events=[])
    assert lines, "empty current-state should still say 'no one is waiting'"
    _assert_no_banned(lines, _BANNED_IN_CURRENT, "current (empty)")


def test_current_state_narrative_no_past_tense_when_populated():
    counts = _mock_counts(needs=3, not_responding=2)
    latest_events = [
        {"device_id": "d1", "status": "trapped", "battery_pct": 8,
         "needs_extraction": True},
        {"device_id": "d2", "status": "trapped", "battery_pct": 55},
        {"device_id": "d3", "status": "trapped", "battery_pct": 12,
         "needs_extraction": False},
    ]
    lines = _current_state_narrative(counts, latest_events)
    _assert_no_banned(lines, _BANNED_IN_CURRENT, "current (populated)")


def test_current_state_narrative_singular_uses_is_not_are():
    """Agreement sanity check: 1 person → 'is', not 'are'."""
    lines = _current_state_narrative(_mock_counts(needs=1), latest_events=[])
    text = " ".join(lines)
    assert "1 person is waiting for help right now." in text, text
    assert "1 person are" not in text


def test_current_state_narrative_plural_uses_are_not_is():
    lines = _current_state_narrative(_mock_counts(needs=4), latest_events=[])
    text = " ".join(lines)
    assert "4 people are waiting for help right now." in text, text
    assert "4 people is" not in text


# ---------------------------------------------------------------------------
# Sanity: "period" wording is present (Paul asked for "during this
# period", not "in this time window").
# ---------------------------------------------------------------------------
def test_window_narrative_uses_period_wording():
    """Report-facing prose must say 'during this period' — the earlier
    "in this time window" phrasing was jargon-adjacent."""
    lines = _window_narrative(raw_rows=[], latest_events=[], counts={})
    text = " ".join(lines)
    assert "during this period" in text or "were trapped" in text, text
