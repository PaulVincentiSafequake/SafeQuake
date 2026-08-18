"""Singular/plural agreement across EVERY generated narrative sentence.

Three separate singular/plural bugs have reached a rendered PDF (#124, the
A1 chart/sentence contradiction, and the egress wording). Each was fixed as
an instance. This file fixes the class: it generates every narrative
sentence the reports can produce, over every count that changes the wording
(0, 1, 2, many, and n == t), and runs one shared bank of grammar rules over
all of them. A new sentence added anywhere in reports_export is covered the
moment it is reachable from one of the generators below.

Pure functions only — no DB, no HTTP, runs in milliseconds.
"""
import re
import sys
from datetime import timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import reports_export as R  # noqa: E402


# ─── the shared rule bank ───────────────────────────────────────────────
# Each rule: (compiled pattern, why it is wrong). Digit-guarded everywhere
# so "31 people" is not read as "1 people".
BANNED = [
    (re.compile(r"(?<!\d)\b1 (people|persons)\b"), "count of 1 with a plural noun"),
    (re.compile(r"(?<!\d)\b1 (days|hours|minutes|tremors|teams)\b"), "count of 1 with a plural noun"),
    (re.compile(r"(?<!\d)\b1 person (have|are|were|do)\b"), "singular subject with a plural verb"),
    (re.compile(r"\b(?<!1 )(?<!\d)([02-9]|\d\d+) (person|day|hour|minute|tremor|team)\b"),
     "count above 1 with a singular noun"),
    (re.compile(r"\b\d+ people (has|is|was|does)\b"), "plural subject with a singular verb"),
    (re.compile(r"\bthe 1 (person|people)\b", re.I), "'the 1 person' — write 'the only person'"),
    (re.compile(r"\bof the 1 people\b"), "'of the 1 people'"),
    (re.compile(r"\ball 1 (person|people)\b", re.I), "'all 1 …' — write 'the only person'"),
    (re.compile(r"\b(they|them|those) (?:person)\b"), "pronoun/noun disagreement"),
    (re.compile(r"\b0 (people|person)\b"), "bare zero count — write 'No one'"),
    (re.compile(r"\bSome of them\b.*"), None),   # context rule, see below
]


def _check(text: str, where: str):
    for pattern, why in BANNED:
        if why is None:
            continue
        m = pattern.search(text)
        assert not m, f"{where}: {why} — {m.group(0)!r} in {text!r}"
    # sentence must end in a full stop and start with a capital
    assert text[:1] == text[:1].upper(), f"{where}: not capitalised — {text!r}"
    assert text.rstrip().endswith((".", "%.", "?")), f"{where}: no full stop — {text!r}"


def _events(n_trapped, needs_extraction=0, low_battery=0):
    out = []
    for i in range(n_trapped):
        e = {"device_id": f"d{i}", "status": "trapped"}
        if i < needs_extraction:
            e["needs_extraction"] = True
        if i < low_battery:
            e["battery_pct"] = 5
        out.append(e)
    return out


# ─── generators ─────────────────────────────────────────────────────────
@pytest.mark.parametrize("total", [0, 1, 2, 5, 31])
@pytest.mark.parametrize("rescued", [0, 1, 2, 4])
@pytest.mark.parametrize("still", [0, 1, 2, 3])
def test_progress_narrative_grammar(total, rescued, still):
    raw_rows = [{"device_id": f"d{i}", "status": "trapped"} for i in range(total)]
    # self-safe: some of the trapped devices later reported safe
    latest = [{"device_id": f"d{i}", "status": "safe"} for i in range(min(total, 2))]
    counts = {"rescued": rescued, "trapped": still}
    for line in R._plain_language_progress(raw_rows, latest, counts):
        _check(line, f"progress(total={total},resc={rescued},still={still})")


@pytest.mark.parametrize("t,n", [(1, 1), (2, 1), (2, 2), (3, 1), (3, 3), (31, 1), (31, 31)])
def test_extraction_lines_grammar(t, n):
    lines = R._extraction_lines(_events(t, needs_extraction=n))
    assert lines, "extraction lines missing when someone cannot get out"
    for line in lines:
        _check(line, f"extraction(t={t},n={n})")
    if n == 1:
        assert "Some of them" not in " ".join(lines), (
            "plural 'Some of them' used for a single person"
        )


@pytest.mark.parametrize("t,n", [(1, 1), (2, 1), (2, 2), (3, 1), (3, 3), (31, 4)])
def test_low_battery_lines_grammar(t, n):
    lines = R._low_battery_lines(_events(t, low_battery=n))
    assert lines, "battery lines missing when someone is below 20%"
    for line in lines:
        _check(line, f"battery(t={t},n={n})")


@pytest.mark.parametrize("minutes", [0, 1, 2, 59, 60, 61, 120, 1440, 1441, 1500, 2880, 4321])
def test_duration_words_grammar(minutes):
    text = R._duration_words(timedelta(minutes=minutes)) + "."
    _check(text[0].upper() + text[1:], f"duration({minutes}m)")


def test_no_extraction_or_battery_lines_when_none():
    assert R._extraction_lines(_events(3)) == []
    assert R._low_battery_lines(_events(3)) == []
    assert R._extraction_lines([]) == []


def test_subject_agreement_helper():
    assert R._subject_of_still_trapped(1, 1) == ("The only person still trapped", "has")
    assert R._subject_of_still_trapped(3, 3) == ("All 3 people still trapped", "have")
    assert R._subject_of_still_trapped(1, 3) == ("1 of the 3 people still trapped", "has")
    assert R._subject_of_still_trapped(2, 3) == ("2 of the 3 people still trapped", "have")


def test_n_people_helper():
    assert R._n_people(1) == "1 person"
    assert R._n_people(0) == "0 people"
    assert R._n_people(31) == "31 people"
