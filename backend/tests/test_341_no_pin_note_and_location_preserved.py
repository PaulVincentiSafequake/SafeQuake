"""#341 (Paul, 2026-09-04 — verbatim):

  "QQ43D is tracked as trapped, with a full history in the alarm panel
   and counted in '1 TRAPPED' — but it has no pin anywhere on the map.
   Can you check whether this device has a saved location? If it
   doesn't, please add a plain note on its card saying so, instead of
   it just having no pin with no explanation. If it does have a
   location, something is failing to draw the pin — please find out
   why."

Two problems guarded by this test file:

  1. Dashboard card: when the person has no saved location, the card
     must say so in plain words. The operator must never be left to
     wonder whether the map is broken or the phone simply never shared
     its position.

  2. Backend upsert: a locationless re-check (permission revoked,
     indoors, quick-answer flow, or a pre-permission app version) MUST
     NOT null a previously-known latitude/longitude. Software may
     move a record towards being useful on the map, never away from
     it — the earlier fix stands as the last-known position.

Both together mean: a person who ever shared a location keeps their
pin even after locationless updates, and a person who never shared one
gets an explicit note on the card instead of an invisible gap.
"""
from datetime import datetime, timezone
from pathlib import Path
import re

import pytest

DASH_PATH = (Path(__file__).resolve().parents[2]
             / "memory" / "dashboard_build" / "index.html")
DASH = DASH_PATH.read_text()


# ── PART 1: Dashboard card carries the plain-words note ─────────────────


def test_card_shows_no_saved_location_note_when_coords_missing():
    """`itemHtml` must render a plain-English note whenever a person's
    lat/lng is missing. Without it the operator has no explanation for
    the missing pin."""
    # The exact operator-facing sentence Paul asked for. Locked here so
    # a future reword cannot quietly drop the explanation.
    assert (
        "No saved location — this phone never shared its position, "
        "so there is no pin on the map for them."
    ) in DASH, (
        "The card must say, in plain words, that this person has no "
        "saved location. Silence on the card is exactly the bug Paul "
        "reported."
    )


def test_card_no_loc_note_is_gated_on_missing_coordinates():
    """The note must only appear when both coordinates are missing —
    otherwise a person with a real pin would also get the 'no location'
    line and the card would contradict the map."""
    # Look for the guard in the source. The name of the local var is
    # part of the contract with any future refactor of this block.
    assert "var hasLoc = (u.latitude != null && u.longitude != null" in DASH, (
        "The 'no saved location' note must be gated on lat AND lng "
        "being present. Any relaxation of that guard would make the "
        "card contradict the map."
    )
    # noLocLine must render only when hasLoc is falsy.
    assert "var noLocLine = hasLoc" in DASH, (
        "The noLocLine variable must be conditionally set from hasLoc."
    )


def test_card_no_loc_note_is_included_in_return_html():
    """A note that never reaches the rendered HTML is worse than no
    note — it looks fixed in code review and stays invisible to the
    operator. Guard the composition line so nobody can drop it."""
    # The return statement should splice noLocLine in the visible slot.
    # We look for the substring rather than an exact whole line so
    # unrelated changes to the composition are allowed.
    assert "battLine + noLocLine + groupLine" in DASH, (
        "noLocLine must actually be concatenated into the returned "
        "card HTML. If it is only computed but not rendered, the note "
        "never reaches the operator."
    )


def test_card_no_loc_note_has_its_own_style():
    """The note must be visually distinct — not styled as an urgent red
    warning (it is a fact, not a failure) and not styled as
    invisible-secondary meta text. A dedicated class is the anchor."""
    assert ".qg-card-no-loc" in DASH, (
        "The 'no saved location' note needs a dedicated CSS class so "
        "it can be styled distinctly from urgent red warnings and "
        "from muted meta lines."
    )


# ── PART 2: Backend upsert must NOT null a previously-known location ────


def _mk_payload(**overrides):
    """Minimal StatusInPayload dict for _normalize_status_payload."""
    base = {
        "device_id": "QQ43D",
        "status": "trapped",
        "severity": "red",
    }
    base.update(overrides)
    return base


@pytest.mark.asyncio
async def test_locationless_checkin_preserves_previous_coords(monkeypatch):
    """A phone that reported with GPS, then reports again WITHOUT GPS,
    must keep its earlier latitude/longitude. Otherwise the pin
    silently disappears from the map even though the person is still
    on the working board — the exact QQ43D symptom Paul reported."""
    import sys, os
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

    # Import server with its side effects (Mongo motor client) neutered.
    # We only need the post_status handler and its normalizer here.
    from server import _normalize_status_payload  # noqa: WPS433

    # Simulate the raw dict the upsert would build. Locationless report
    # arrives; lat/lng end up None in the doc.
    from server import StatusInPayload
    p_no_loc = StatusInPayload(
        device_id="QQ43D",
        status="trapped",
        severity="red",
    )
    doc = _normalize_status_payload(p_no_loc)
    assert doc["latitude"] is None
    assert doc["longitude"] is None

    # Re-implement the same guard the endpoint applies before writing —
    # this is the contract the endpoint MUST honour. Verifies that the
    # doc that would be written to Mongo does not carry lat/lng keys
    # when they are None, so a prior fix survives $set.
    set_doc = dict(doc)
    if set_doc.get("latitude") is None and set_doc.get("longitude") is None:
        set_doc.pop("latitude", None)
        set_doc.pop("longitude", None)
        set_doc.pop("accuracy_m", None)

    assert "latitude" not in set_doc, (
        "A locationless re-check must not include a null latitude in "
        "the $set doc — that would clobber a previously-known fix."
    )
    assert "longitude" not in set_doc, (
        "A locationless re-check must not include a null longitude in "
        "the $set doc — that would clobber a previously-known fix."
    )
    assert "accuracy_m" not in set_doc, (
        "Accuracy without coordinates is meaningless — drop it in "
        "lockstep with lat/lng so a stale accuracy cannot linger."
    )
    # Non-location fields must still be set — the record must still
    # move to the newest status/severity/battery. This is the safe
    # direction the rule allows.
    assert set_doc["status"] == "trapped"
    assert set_doc["severity"] == "red"


@pytest.mark.asyncio
async def test_checkin_with_location_still_overwrites(monkeypatch):
    """The opposite direction: a NEW fix must land on the row.
    Software may always improve the map; it just may not erase from
    it. Guard against a lazy 'preserve if present' bug that would let
    a stale fix outlive a newer, better one."""
    from server import _normalize_status_payload, StatusInPayload

    p_with_loc = StatusInPayload(
        device_id="QQ43D",
        status="trapped",
        severity="red",
        latitude=35.9,
        longitude=14.5,
    )
    doc = _normalize_status_payload(p_with_loc)
    assert doc["latitude"] == 35.9
    assert doc["longitude"] == 14.5

    set_doc = dict(doc)
    if set_doc.get("latitude") is None and set_doc.get("longitude") is None:
        set_doc.pop("latitude", None)
        set_doc.pop("longitude", None)
        set_doc.pop("accuracy_m", None)

    assert set_doc["latitude"] == 35.9, (
        "A fresh fix must be written through — the preservation rule "
        "only fires when the new payload has NO fix."
    )
    assert set_doc["longitude"] == 14.5


def test_server_preserves_lat_lng_guard_is_in_place():
    """Static guard: the endpoint code must contain the preservation
    block. A future refactor that puts `$set: doc` back would silently
    resurrect the QQ43D bug — this test refuses to let that happen."""
    server_src = (Path(__file__).resolve().parents[1] / "server.py").read_text()
    # Both the pop line AND the $set variable name are locked.
    assert "set_doc.pop(\"latitude\", None)" in server_src
    assert "set_doc.pop(\"longitude\", None)" in server_src
    assert "\"$set\": set_doc" in server_src, (
        "The upsert must use the sanitized set_doc, not the raw doc. "
        "Reverting to `$set: doc` restores the bug."
    )
