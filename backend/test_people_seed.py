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
from deps import is_test_device as _is_test_device


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

    async def _resolve_every_alarm_from_test_people(
        reason: str,
        extra_ids: Optional[list[str]] = None,
    ) -> int:
        """#308 (Paul, 2026-08-28) / #320 (Paul, 2026-08-29):
        "Remove all test people must always clear every alarm they caused,
        no matter which surface created them."

        Four orthogonal predicates, unioned, so no ghost slips through:
          · every open alarm whose `device_id` starts with `qg-<SEED_TAG>-`
            (deterministic — every /seed row uses this prefix regardless of
            the random suffix that changes each batch), and
          · every open alarm flagged `is_test: True` (which is what
            raise_alarm sets when the row is synthetic), and
          · every open alarm whose `device_id` matches any of the well-known
            test-marker prefixes (`qg-loadtest-`, `qg-snippet-test-`,
            `qg-rescue-test-`, `qg-rescue-e2e-`, `qg-mob-*`, `diag-*`,
            `TEST_*`, `test-*`, `demo-*`, `playwright-*`), and
          · every open alarm whose device_id is in `extra_ids` — the exact
            list of device_status rows we just deleted in this call, so a
            real-shaped device_id that was flagged synthetic via
            /admin/devices/{id}/mark-test still gets its alarm cleaned up
            even though it doesn't match any regex or marker.

        Union means a batch of alarms that missed the `is_test` flag for
        any reason still gets cleaned up by one of the id predicates.
        Nothing fake keeps sounding after the fake person is gone.
        """
        prefix_re = f"^qg-{SEED_TAG}-"
        # Marker-based device_id predicates. Case-insensitive substring
        # matches to mirror `deps.is_test_device`'s marker set, but scoped
        # so a real UUID that happens to contain "test" doesn't get caught
        # (the real-device shape is `qg-<13-digit epoch>-<8 random>`; the
        # markers below all include a distinctive prefix or hyphen guard).
        marker_or: list[dict] = [
            {"device_id": {"$regex": r"^qg-loadtest-", "$options": "i"}},
            {"device_id": {"$regex": r"^qg-snippet-", "$options": "i"}},
            {"device_id": {"$regex": r"^qg-rescue-test-", "$options": "i"}},
            {"device_id": {"$regex": r"^qg-rescue-e2e-", "$options": "i"}},
            {"device_id": {"$regex": r"^qg-mob-", "$options": "i"}},
            {"device_id": {"$regex": r"^qg-diag-", "$options": "i"}},
            {"device_id": {"$regex": r"^TEST_", "$options": "i"}},
            {"device_id": {"$regex": r"^test-", "$options": "i"}},
            {"device_id": {"$regex": r"^demo-", "$options": "i"}},
            {"device_id": {"$regex": r"^playwright-", "$options": "i"}},
            {"device_id": {"$regex": r"^diag-", "$options": "i"}},
            {"device_id": "dashboard"},
        ]
        or_clauses: list[dict] = [
            {"is_test": True},
            {"device_id": {"$regex": prefix_re}},
            *marker_or,
        ]
        if extra_ids:
            or_clauses.append({"device_id": {"$in": list(extra_ids)}})
        result = await db.board_alarms.update_many(
            {
                "resolved_at": None,
                "$or": or_clauses,
            },
            {"$set": {
                "resolved_at": datetime.now(timezone.utc).isoformat(),
                "resolved_reason": reason,
            }},
        )
        return int(result.modified_count)

    async def _sweep_all_test_device_status(reason: str) -> tuple[int, list[str], dict]:
        """#320 (Paul, 2026-08-29): find and delete every test device_status
        row, no matter which surface created it. Returns:
          (deleted_count, deleted_ids, breakdown_by_predicate)

        Predicates unioned (defense-in-depth):
          A. `_test_seed` present (any batch of /seed, past or future)
          B. `synthetic: True` (mark-test on a real-shaped device;
             load-test seeder; #229 spec rows)
          C. `is_test: True` (defensive — any code path that ever set it)
          D. `load_test_run_id` present (B5 load-test seeder)
          E. device_id matches a known test-shape marker
             (`qg-seeded-*`, `qg-loadtest-*`, `qg-snippet-*`,
              `qg-rescue-test-*`, `qg-rescue-e2e-*`, `qg-mob-*`,
              `qg-diag-*`, `TEST_*`, `test-*`, `demo-*`,
              `playwright-*`, `diag-*`, exactly `dashboard`)

        A device flagged by ANY one of these is a test row and gets
        removed. This mirrors — and is a strict superset of — the
        `deps.is_test_device()` matcher that the dashboard already uses
        to decide whether the TEST badge should appear beside a row.
        """
        # Pull only the fields we need for classification + audit.
        cursor = db.device_status.find(
            {},
            {
                "_id": 0,
                "device_id": 1,
                "synthetic": 1,
                "is_test": 1,
                "_test_seed": 1,
                "load_test_run_id": 1,
                "display_name": 1,
            },
        )
        breakdown = {
            "seed_tag": 0,
            "synthetic_flag": 0,
            "is_test_flag": 0,
            "load_test_run": 0,
            "marker_id": 0,
        }
        to_delete: list[str] = []
        async for r in cursor:
            did = str(r.get("device_id") or "")
            matched = False
            if r.get("_test_seed"):
                breakdown["seed_tag"] += 1
                matched = True
            if r.get("synthetic") is True:
                breakdown["synthetic_flag"] += 1
                matched = True
            if r.get("is_test") is True:
                breakdown["is_test_flag"] += 1
                matched = True
            if r.get("load_test_run_id"):
                breakdown["load_test_run"] += 1
                matched = True
            # Fall through to the marker-id matcher so an old orphan row
            # that lost its flags (or never had them) is still caught.
            if not matched and _is_test_device(r):
                breakdown["marker_id"] += 1
                matched = True
            if matched and did:
                to_delete.append(did)

        deleted_count = 0
        if to_delete:
            # Delete in chunks so a large purge doesn't build a giant $in.
            for i in range(0, len(to_delete), 500):
                chunk = to_delete[i : i + 500]
                res = await db.device_status.delete_many(
                    {"device_id": {"$in": chunk}}
                )
                deleted_count += int(res.deleted_count)
        return deleted_count, to_delete, breakdown


    @api_router.post("/admin/test-people/seed")
    async def seed_test_people(request: Request):
        principal = await resolve_principal(
            request, request.headers.get("x-admin-token"),
            ADMIN_TRIGGER_PASSWORD, db,
        )
        require_role(principal, "admin", "operator")

        now = datetime.now(timezone.utc)
        # Refuse to double-seed: if any test rows already exist (from THIS
        # /seed or any other surface) sweep them first, so pressing "Add
        # 33" is always deterministic — you end up with exactly the 33
        # spec rows and no leftovers from a previous batch or a stray
        # mark-test row.
        #
        # #308 (Paul, 2026-08-28) / #320 (Paul, 2026-08-29):
        # the previous shape of this branch only touched rows whose
        # `_test_seed` matched SEED_TAG. That left mark-test-flagged
        # devices, load-test rows, and any prior batch that had lost its
        # flag as orphans in device_status — and their alarms as ghosts.
        # The sweep below removes every test row by any recognised
        # predicate, and the alarm sweeper accepts the exact list of
        # deleted ids so alarms whose device_id no longer matches any
        # regex still get resolved.
        pre_deleted, pre_ids, _ = await _sweep_all_test_device_status(
            "Test people were replaced with a fresh batch."
        )
        if pre_deleted:
            await _resolve_every_alarm_from_test_people(
                "Test people were replaced with a fresh batch.",
                extra_ids=pre_ids,
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

        # #320 (Paul, 2026-08-29): "Remove all test people" now clears
        # every test row created by ANY surface, not just the /seed
        # batch. This covers:
        #   · /admin/test-people/seed rows          (`_test_seed`)
        #   · /admin/devices/{id}/mark-test rows    (`synthetic:true`
        #                                             on a real device_id)
        #   · scripts/load_test_seed.py rows        (`load_test_run_id`)
        #   · diagnostics / e2e / snippet / demo /
        #     playwright / `dashboard` device_ids   (marker match)
        #   · any row with `is_test:true` set by any code path
        # The alarm sweeper is called with the exact list of ids we just
        # deleted so a real-shaped id that only had `synthetic:true`
        # (mark-test) still gets its open alarm resolved.
        deleted_count, deleted_ids, breakdown = await _sweep_all_test_device_status(
            "Test people were cleared."
        )
        modified = await _resolve_every_alarm_from_test_people(
            "Test people were cleared.",
            extra_ids=deleted_ids,
        )
        return {
            "removed": int(deleted_count),
            "alarms_cleared": int(modified),
            "seed_tag": SEED_TAG,
            "matched_by": breakdown,
        }
