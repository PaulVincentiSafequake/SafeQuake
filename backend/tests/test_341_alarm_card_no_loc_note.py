"""#341 follow-up (Paul, 2026-09-04 — live re-test):

  "We checked live: the code for the 'no saved location' note is
   deployed and correct, but it does not appear on the red 'NEEDS HELP'
   alarm card for QQ43D — that card only shows the alarm history,
   nothing about location."

The first pass added the note to the sidebar triage card only. Paul
was reading the ALARM PANEL card, which is rendered from a different
data source (`board_alarms.list_open`) with a different renderer in
the dashboard. This test file locks the fix on that surface:

  1. Backend `board_alarms.list_open` must attach a `has_location`
     boolean to every alarm-card person, driven by device_status
     latitude/longitude.
  2. The card-level `has_location` must mirror the single-person case
     exactly.
  3. For multi-person minute-clusters, `missing_location_count` must
     name how many are missing — a partial gap must never be hidden.
  4. The dashboard alarm renderer must emit the "no saved location"
     note, with the exact wording, whenever the card carries
     `has_location === false`.
"""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
import uuid

import pytest


DASH_PATH = (Path(__file__).resolve().parents[2]
             / "memory" / "dashboard_build" / "index.html")
DASH = DASH_PATH.read_text()


# ── PART 1: dashboard alarm renderer carries the note ─────────────────


def test_alarm_card_renderer_emits_no_loc_note_for_single_person():
    """The alarm card must include a `noLocLine` computed from
    `g.has_location === false` and splice it into the rendered HTML.
    Anything less and Paul sees exactly what he reported — a red card
    with no explanation for the missing pin."""
    assert "if (g.count === 1 && g.has_location === false)" in DASH, (
        "The single-person alarm card must render the note when the "
        "person has no saved location. Missing guard === missing note."
    )
    # Exact operator-facing sentence, matching the sidebar card, so an
    # operator reading either surface sees the same fact worded the
    # same way.
    assert (
        "No saved location — this phone never shared its position, "
        "so there is no pin on the map for them."
    ) in DASH


def test_alarm_card_renderer_emits_partial_gap_note_for_group():
    """When a minute-cluster has SOME people missing locations, the
    card must say 'N of M have no saved location' rather than hide the
    gap. A partial gap silently rounded to 'all pinned' is worse than
    the original bug — it looks fine."""
    assert "g.missing_location_count > 0" in DASH
    # The exact wording for the multi-person case.
    assert (
        "have no saved location — those phones never shared their "
        "position, so no pin is drawn for them on the map."
    ) in DASH


def test_alarm_card_no_loc_note_is_spliced_into_the_card_html():
    """A computed noLocLine that never reaches the DOM is a silent
    regression. Guard the composition line explicitly."""
    # The noLocLine token must appear in the card's return HTML,
    # positioned AFTER the meta line and BEFORE the story so it reads
    # in the natural order of the card.
    assert re.search(
        r'"</div>"\s*\+\s*\n\s*noLocLine\s*\+\s*\n\s*story',
        DASH,
    ), (
        "noLocLine must be concatenated between the alarm-meta line "
        "and the story disclosure, or the note never reaches the "
        "operator."
    )


def test_alarm_card_no_loc_note_has_its_own_style():
    """The alarm card lives on the dark red panel. Its note needs its
    own class — the sidebar's muted-grey style would vanish on that
    background. Lock the class name so a future refactor cannot drop
    it."""
    assert ".qg-alarm-no-loc" in DASH


# ── PART 2: board_alarms carries the flag on every person ─────────────


class _FakeCursor:
    """Minimal async cursor for db.board_alarms.find(...).sort().to_list()."""

    def __init__(self, docs):
        self._docs = list(docs)

    def sort(self, *_args, **_kwargs):
        return self

    async def to_list(self, _limit):
        return list(self._docs)


class _StatusCursor:
    """Minimal async iterator for db.device_status.find(query, proj)."""

    def __init__(self, docs):
        self._docs = list(docs)

    def __aiter__(self):
        self._it = iter(self._docs)
        return self

    async def __anext__(self):
        try:
            return next(self._it)
        except StopIteration:
            raise StopAsyncIteration


class _FakeDB:
    """Only the collections board_alarms.list_open touches are needed."""

    def __init__(self, alarms, statuses, meta=None):
        self._alarms = alarms
        self._statuses = statuses
        self._meta = meta or {}
        self.board_alarms = self._AlarmsColl(alarms)
        self.device_status = self._StatusColl(statuses)
        self.board_meta = self._MetaColl(self._meta)

    class _AlarmsColl:
        def __init__(self, docs):
            self._docs = docs

        def find(self, *_args, **_kwargs):
            return _FakeCursor(self._docs)

    class _StatusColl:
        def __init__(self, docs):
            self._docs = docs

        def find(self, query, _proj=None):
            ids = set()
            try:
                ids = set(query["device_id"]["$in"])
            except Exception:
                ids = set()
            return _StatusCursor([d for d in self._docs if d.get("device_id") in ids])

    class _MetaColl:
        def __init__(self, doc):
            self._doc = doc

        async def find_one(self, *_args, **_kwargs):
            return None


def _iso(dt):
    return dt.astimezone(timezone.utc).isoformat()


def _mk_alarm(device_id, kind="needs_help", **overrides):
    now = datetime.now(timezone.utc)
    base = {
        "id": str(uuid.uuid4()),
        "device_id": device_id,
        "kind": kind,
        "word": "NEEDS HELP",
        "shape": "circle",
        "headline": device_id + " needs help now",
        "action": "Send a team.",
        "created_at": _iso(now - timedelta(minutes=1)),
        "group_key": device_id + ":needs_help:2026-09-04-10-30",
        "resolved_at": None,
        "ack_at": None,
        "ack_by": None,
        "is_test": False,
        "severity": "red",
    }
    base.update(overrides)
    return base


@pytest.mark.asyncio
async def test_list_open_flags_missing_location_on_single_person_alarm():
    """The primary QQ43D case: one open NEEDS HELP row, one
    device_status row with NULL lat/lng. The alarm card must come back
    with has_location=False so the dashboard note appears."""
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import board_alarms  # noqa: WPS433

    alarms = [_mk_alarm("QQ43D")]
    statuses = [{"device_id": "QQ43D", "latitude": None, "longitude": None}]
    db = _FakeDB(alarms, statuses)

    out = await board_alarms.list_open(db)
    assert out["open"] == 1
    assert len(out["groups"]) == 1
    g = out["groups"][0]
    assert g["count"] == 1
    assert g["has_location"] is False, (
        "A device with null lat/lng must be reported as has_location=False "
        "on the alarm card — that is the flag the dashboard renders on."
    )
    assert g["missing_location_count"] == 1
    # The per-person entry must carry the flag too, so a multi-person
    # cluster can name each missing person from its `people` list.
    assert g["people"][0]["has_location"] is False


@pytest.mark.asyncio
async def test_list_open_flags_saved_location_on_single_person_alarm():
    """The positive case: same alarm, real coordinates on the row.
    has_location must be True, missing_location_count 0, so the note
    does NOT render on a person who is actually on the map."""
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import board_alarms  # noqa: WPS433

    alarms = [_mk_alarm("QQ43D")]
    statuses = [{"device_id": "QQ43D", "latitude": 35.9, "longitude": 14.5}]
    db = _FakeDB(alarms, statuses)

    out = await board_alarms.list_open(db)
    g = out["groups"][0]
    assert g["has_location"] is True
    assert g["missing_location_count"] == 0
    assert g["people"][0]["has_location"] is True


@pytest.mark.asyncio
async def test_list_open_flags_missing_when_device_status_row_absent():
    """No device_status row at all → treated as no saved location. A
    device that raised an alarm but somehow has no status doc must not
    be reported as pinned."""
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import board_alarms  # noqa: WPS433

    alarms = [_mk_alarm("QQGHOST")]
    statuses: list = []  # nothing in device_status
    db = _FakeDB(alarms, statuses)

    out = await board_alarms.list_open(db)
    g = out["groups"][0]
    assert g["has_location"] is False
    assert g["people"][0]["has_location"] is False


@pytest.mark.asyncio
async def test_list_open_partial_gap_in_group_is_counted():
    """A minute-cluster where SOME people have locations and some don't
    must report the count of missing ones, not just a boolean. Hiding a
    partial gap would let the operator assume everyone is pinned."""
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import board_alarms  # noqa: WPS433

    # Two devices, both in the SAME group_key so they cluster on one card.
    at = _iso(datetime.now(timezone.utc) - timedelta(minutes=1))
    a1 = _mk_alarm("QQPINNED", created_at=at,
                   group_key="cluster:needs_help:2026-09-04-10-30")
    a2 = _mk_alarm("QQGHOST", created_at=at,
                   group_key="cluster:needs_help:2026-09-04-10-30")
    alarms = [a1, a2]
    statuses = [
        {"device_id": "QQPINNED", "latitude": 35.9, "longitude": 14.5},
        {"device_id": "QQGHOST", "latitude": None, "longitude": None},
    ]
    db = _FakeDB(alarms, statuses)

    out = await board_alarms.list_open(db)
    # Both rows collapse into ONE card because they share a group_key.
    assert len(out["groups"]) == 1
    g = out["groups"][0]
    assert g["count"] == 2
    assert g["has_location"] is False, (
        "Any missing location in the cluster flips the card-level flag "
        "so the note renders on the group card."
    )
    assert g["missing_location_count"] == 1
    # Person-level flags: one True, one False.
    per_flag = {p["device_id"]: p["has_location"] for p in g["people"]}
    assert per_flag == {"QQPINNED": True, "QQGHOST": False}
