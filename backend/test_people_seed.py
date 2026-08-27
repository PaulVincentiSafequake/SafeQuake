"""#229 (Batch 7) — 33 hand-designed test people.

The rule that matters most (Paul, 2026-08-19, verbatim):

  > Test people must be obviously fake to a human reading the screen.
  > Names prefixed TEST, rescue codes starting Z, visibly marked
  > wherever they appear — not only behind the existing checkbox.
  > This project has already lost time to realistic-looking fake data
  > sitting in the trapped list for over a week.

Everything this module inserts is tagged three ways so nothing about
it can quietly look real: `is_test=True`, `synthetic=True`, and a
`_test_seed="seeded-33"` document-tag so the whole set can be removed
in one operation. Display names start with `TEST ` and rescue codes
start with `Z`.

This module ONLY writes to `device_status` (and optionally a couple of
`status_events` for the audit trail). It NEVER queues APNs pushes, it
NEVER schedules re-checks, and it NEVER runs the sweep. Adding these
seeds must be free of side effects that reach a real person's phone.

Full spec: /app/memory/test-people-spec.md.
"""
from __future__ import annotations

import math
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Request

from auth import resolve_principal, require_role


SEED_TAG = "seeded-33"
CODE_PREFIX = "Z"
NAME_PREFIX = "TEST"


# #241 (Batch 7 R5): map pins that share exact coordinates stack into a
# single visible dot, which is why the manual test read as "33 test
# people stacked on one point". The spec deliberately clusters people
# at the same address (e.g. four at `same_bldg` is the collapsed-
# building grouping case), but the map still needs to show them as
# separate dots when zoomed in. Small deterministic jitter — a metre
# is roughly 1/111_111 of a degree of latitude — spreads N people at
# the same base coord onto a small ring so all N dots are visible.
# Deterministic (idx-based) so tests can assert exact positions and
# users see the same layout each time they seed.
_JITTER_RING_M = 18.0  # ~18 m — bigger than typical marker radius
_LAT_METRE = 1.0 / 111_111.0


def _jitter(lat: float, lon: float, index_within_group: int, group_size: int) -> tuple[float, float]:
    """Spread `group_size` people evenly around a small ring at the
    base coord. Returns the person's (lat, lon)."""
    if group_size <= 1:
        return (lat, lon)
    angle = (2.0 * math.pi * index_within_group) / group_size
    d_lat = _JITTER_RING_M * math.cos(angle) * _LAT_METRE
    # Longitude degree shrinks as cos(lat) — keeps the ring circular
    # in metres at Malta's latitude, not stretched east-west.
    d_lon = (_JITTER_RING_M * math.sin(angle) * _LAT_METRE
             / max(0.1, math.cos(math.radians(lat))))
    return (lat + d_lat, lon + d_lon)


def _at(minutes_ago: int, now: datetime) -> str:
    return (now - timedelta(minutes=minutes_ago)).isoformat()


def _people_spec(now: datetime) -> list[dict]:
    """The 33 people, spec-locked. All timestamps are relative to `now`."""

    # Malta reference points — real coordinates so map pins land where a
    # real incident would. Group of four at one address tests the
    # collapsed-building grouping case.
    valletta = (35.8989, 14.5145)
    same_bldg = (35.8998, 14.5122)          # four at this address
    sliema = (35.9126, 14.5027)
    msida = (35.8952, 14.4933)
    mosta = (35.9096, 14.4257)
    victoria_gozo = (36.0433, 14.2418)      # Gozo
    xewkija_gozo = (36.0330, 14.2610)       # Gozo
    approx = (35.9375, 14.3754)             # low-accuracy fix

    people: list[dict] = []
    idx = 0

    def add(status: str, severity: Optional[str], mobility: Optional[str],
            battery: Optional[int], loc: tuple, minutes_ago: int,
            *, needs_extraction: bool = False, accuracy_m: float = 12.0,
            rescued_at_minutes_ago: Optional[int] = None) -> None:
        nonlocal idx
        idx += 1
        # #229 (Batch 7): device_id is engineered so the last five
        # characters begin with 'Z'. `short_code(device_id)` takes the
        # last 5 uppercased chars, so every seeded row's rescue code
        # starts with Z on every surface (map pin, sidebar list, PDF,
        # activity feed) without patching each read site. Real device
        # IDs come from UUID4 hex and won't accidentally hit this
        # pattern — Z at position -5 is effectively reserved for test.
        z_suffix = f"Z{uuid.uuid4().hex[:4].lower()}"
        row = {
            # Real-device-shaped id so filters that look at "qg-<uuid>"
            # still see it, but obviously synthetic once you read the tag
            # OR the last-5 (which becomes a Z-prefixed rescue code).
            "device_id": f"qg-{SEED_TAG}-{idx:02d}-{z_suffix}",
            "display_name": f"{NAME_PREFIX} Person {idx:02d}",
            "short_code": f"{CODE_PREFIX}{idx:04d}",
            "status": status,
            "severity": severity,
            "mobility": mobility,
            "needs_extraction": bool(needs_extraction),
            "battery_pct": battery,
            # #241 (Batch 7 R5): store the BASE coord and the position
            # within the group here; the real jittered lat/lon is
            # filled in after the whole spec is enumerated, so we know
            # each base-coord's group size before assigning angles.
            "_base_loc": loc,
            "accuracy_m": accuracy_m,
            "platform": "ios" if idx % 2 else "android",
            "updated_at": _at(minutes_ago, now),
            "created_at": _at(minutes_ago, now),
            "trapped_since": (
                _at(minutes_ago, now) if status == "trapped" else None
            ),
            "rescued_at": (
                _at(rescued_at_minutes_ago, now)
                if rescued_at_minutes_ago is not None else None
            ),
            # #229 hard rule: three tags mean nothing about this row
            # can quietly look real.
            "is_test": True,
            "synthetic": True,
            "_test_seed": SEED_TAG,
        }
        people.append(row)

    # ── Immediate: 3 ───────────────────────────────────────────────────
    # One at 4% battery (very-low badge), others at 22% and 47%.
    add("trapped", "red", "trapped", 4,  same_bldg, minutes_ago=42)
    add("trapped", "red", "trapped", 22, same_bldg, minutes_ago=38)
    add("trapped", "red", "trapped", 47, valletta, minutes_ago=27)

    # ── Serious: 5 ─────────────────────────────────────────────────────
    #   1 × 8 h ago
    #   1 × 5 h ago
    #   1 × 3 h ago
    #   2 × within the last ~20 min, a few seconds apart (tie-break test)
    # Battery: one 9%, one 11% (near-simultaneous with an 80% row for the
    # battery-tiebreak test), the rest spread.
    add("trapped", "yellow", "trapped", 9,   sliema,        minutes_ago=8 * 60,
        needs_extraction=True)
    add("trapped", "yellow", "mobile",  35,  msida,         minutes_ago=5 * 60)
    add("trapped", "yellow", "trapped", 55,  mosta,         minutes_ago=3 * 60,
        needs_extraction=True)
    # The tie-break pair — reported ~5 seconds apart, one at 11%, one at 80%.
    add("trapped", "yellow", "trapped", 11,  same_bldg,     minutes_ago=18)
    add("trapped", "yellow", "mobile",  80,  same_bldg,     minutes_ago=19)

    # ── Minor: 4 ───────────────────────────────────────────────────────
    add("trapped", "green", "mobile", 63, sliema,        minutes_ago=95)
    add("trapped", "green", "mobile", 42, victoria_gozo, minutes_ago=70)
    add("trapped", "green", "mobile", 71, msida,         minutes_ago=55)
    # One with an approximate fix — large accuracy_m tests the uncertainty
    # display without changing the pin's colour.
    add("trapped", "green", "mobile", 88, approx,        minutes_ago=30,
        accuracy_m=250.0)

    # ── Not responding, phone still alive: 3 ───────────────────────────
    # Silence-is-information: they DID report trapped, then stopped
    # answering, but the phone is still sending heartbeats.
    add("not_responding", "yellow", "trapped", 33, valletta,     minutes_ago=180)
    add("not_responding", "red",    "trapped", 12, mosta,        minutes_ago=210)
    add("not_responding", "green",  "mobile",  46, xewkija_gozo, minutes_ago=140)

    # ── Not responding, phone gone dark: 2 ─────────────────────────────
    # No battery reading at all (last known was very low or unknown).
    add("not_responding", "yellow", "trapped", None, msida,   minutes_ago=300)
    add("not_responding", "red",    "trapped", None, valletta, minutes_ago=270)

    # ── Rescued: 4 ─────────────────────────────────────────────────────
    add("rescued", None, None, 39, sliema,        minutes_ago=120,
        rescued_at_minutes_ago=10)
    add("rescued", None, None, 88, valletta,      minutes_ago=200,
        rescued_at_minutes_ago=30)
    add("rescued", None, None, 51, xewkija_gozo,  minutes_ago=240,
        rescued_at_minutes_ago=55)
    add("rescued", None, None, 74, mosta,         minutes_ago=310,
        rescued_at_minutes_ago=90)

    # ── Safe: 12 ───────────────────────────────────────────────────────
    for i, (loc, mins, bat) in enumerate([
        (sliema, 20, 91), (valletta, 45, 88), (mosta, 60, 67),
        (msida, 75, 82), (same_bldg, 30, 55), (valletta, 100, 41),
        (sliema, 110, 76), (mosta, 125, 62), (victoria_gozo, 140, 58),
        (msida, 160, 33), (valletta, 180, 79), (sliema, 200, 94),
    ]):
        add("safe", None, "mobile", bat, loc, minutes_ago=mins)

    # #241 (Batch 7 R5): apply deterministic per-group jitter so people
    # sharing an address show as separate map dots. Group by the base
    # coord, then spread each group evenly around a small ring.
    groups: dict[tuple, list[int]] = {}
    for i, p in enumerate(people):
        groups.setdefault(p["_base_loc"], []).append(i)
    for base, indices in groups.items():
        for k, i in enumerate(indices):
            lat, lon = _jitter(base[0], base[1], k, len(indices))
            people[i]["latitude"] = lat
            people[i]["longitude"] = lon
    # Drop the transient key so it never reaches the DB.
    for p in people:
        p.pop("_base_loc", None)

    return people


def register_test_people_routes(api_router, db) -> None:
    """Wire two admin-only endpoints onto the caller's router.

    - `POST /api/admin/test-people/seed`   → insert 33 seeded rows.
    - `POST /api/admin/test-people/clear`  → remove the whole seeded set.

    Both refuse without operator/admin auth. Neither queues APNs pushes
    or triggers the re-check sweep.
    """
    from server import ADMIN_TRIGGER_PASSWORD  # imported here to avoid cycles

    async def _resolve_every_alarm_from_test_people(reason: str) -> int:
        """#308 (Paul, 2026-08-28): "removing test people must always
        clear every alarm they caused, no matter how many times I've
        added and removed batches before."

        Two orthogonal predicates, unioned, so no ghost slips through:
          · every open alarm whose `device_id` starts with `qg-<SEED_TAG>-`
            (which is deterministic — every seeded row uses this prefix,
            regardless of the random suffix that changes each batch), and
          · every open alarm flagged `is_test: True` (which is what
            raise_alarm sets when the row is synthetic).

        Union means a batch of alarms that missed the `is_test` flag
        for any reason still gets cleaned up by the prefix match, and a
        batch of alarms that came from a different SEED_TAG one day
        would still get cleaned up by the `is_test` match. Nothing
        fake keeps sounding after the fake person is gone.
        """
        prefix_re = f"^qg-{SEED_TAG}-"
        result = await db.board_alarms.update_many(
            {
                "resolved_at": None,
                "$or": [
                    {"is_test": True},
                    {"device_id": {"$regex": prefix_re}},
                ],
            },
            {"$set": {
                "resolved_at": datetime.now(timezone.utc).isoformat(),
                "resolved_reason": reason,
            }},
        )
        return int(result.modified_count)

    @api_router.post("/admin/test-people/seed")
    async def seed_test_people(request: Request):
        principal = await resolve_principal(
            request, request.headers.get("x-admin-token"),
            ADMIN_TRIGGER_PASSWORD, db,
        )
        require_role(principal, "admin", "operator")

        now = datetime.now(timezone.utc)
        # Refuse to double-seed: if any of these rows already exist, clear
        # them first (idempotent). Otherwise the "add all 33" button would
        # produce 66 people.
        #
        # #308 (Paul, 2026-08-28): the previous shape of this branch
        # deleted device_status rows without touching the open alarms
        # those prior rows had raised. On seed → seed (without a clear
        # between), the first batch's alarms became orphans whose
        # device_ids no longer existed in device_status — still sounding.
        # Adding a batch is now as complete a cleanup as clearing one.
        existing = await db.device_status.count_documents({"_test_seed": SEED_TAG})
        if existing:
            await db.device_status.delete_many({"_test_seed": SEED_TAG})
            await _resolve_every_alarm_from_test_people(
                "Test people were replaced with a fresh batch."
            )

        people = _people_spec(now)
        if people:
            await db.device_status.insert_many([dict(p) for p in people])

        # #301 (Paul, 2026-08-25): "the 33 test people didn't appear in the
        # alarm panel at all — this defeats the whole purpose of test
        # people." The rows were written straight into the board, so
        # nothing ever went through the code that decides what is an
        # alarm. They now raise the same alarms a real report would, each
        # flagged as a test so the board can hide them until an operator
        # ticks "Show test entries".
        import board_alarms
        alarms = 0
        for p in people:
            if p.get("status") != "trapped":
                continue
            row = dict(p)
            row["is_test"] = True
            a = await board_alarms.raise_alarm(
                db, kind=board_alarms.NEEDS_HELP, device_id=row["device_id"],
                row=row, headline=f"{board_alarms._who(row)} needs help",
                action=board_alarms._help_action(row),
            )
            if a:
                alarms += 1

        return {
            "seeded": len(people),
            "alarms_raised": alarms,
            "seed_tag": SEED_TAG,
            "notes": (
                "Seeded rows are visibly TEST-prefixed and use short codes "
                "starting with Z. They behave like real reports everywhere "
                "on the board — counts, map and alarm panel — but only while "
                "\u201cShow test entries\u201d is ticked. No pushes were sent; "
                "no re-check ladder was queued."
            ),
        }

    @api_router.post("/admin/test-people/clear")
    async def clear_test_people(request: Request):
        principal = await resolve_principal(
            request, request.headers.get("x-admin-token"),
            ADMIN_TRIGGER_PASSWORD, db,
        )
        require_role(principal, "admin", "operator")

        result = await db.device_status.delete_many({"_test_seed": SEED_TAG})
        # #301: the rehearsal's alarms go with it. Resolved rather than
        # deleted, so the alarm ledger still reads back honestly.
        # #308: matches by both `is_test: True` AND `qg-<SEED_TAG>-*`
        # device_id prefix, so a batch whose alarms lost the `is_test`
        # flag for any reason still gets cleaned up. Nothing fake ever
        # keeps sounding after the fake person is gone.
        modified = await _resolve_every_alarm_from_test_people(
            "Test people were cleared."
        )
        return {
            "removed": int(result.deleted_count),
            "alarms_cleared": modified,
            "seed_tag": SEED_TAG,
        }
