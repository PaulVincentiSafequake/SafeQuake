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

import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Request

from auth import resolve_principal, require_role


SEED_TAG = "seeded-33"
CODE_PREFIX = "Z"
NAME_PREFIX = "TEST"


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
            "latitude": loc[0],
            "longitude": loc[1],
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

    return people


def register_test_people_routes(api_router, db) -> None:
    """Wire two admin-only endpoints onto the caller's router.

    - `POST /api/admin/test-people/seed`   → insert 33 seeded rows.
    - `POST /api/admin/test-people/clear`  → remove the whole seeded set.

    Both refuse without operator/admin auth. Neither queues APNs pushes
    or triggers the re-check sweep.
    """
    from server import ADMIN_TRIGGER_PASSWORD  # imported here to avoid cycles

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
        existing = await db.device_status.count_documents({"_test_seed": SEED_TAG})
        if existing:
            await db.device_status.delete_many({"_test_seed": SEED_TAG})

        people = _people_spec(now)
        if people:
            await db.device_status.insert_many([dict(p) for p in people])

        return {
            "seeded": len(people),
            "seed_tag": SEED_TAG,
            "notes": (
                "Seeded rows are visibly TEST-prefixed and use short codes "
                "starting with Z. No pushes were sent; no re-check ladder "
                "was queued."
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
        return {
            "removed": int(result.deleted_count),
            "seed_tag": SEED_TAG,
        }
