"""Single source of truth for "how many people are in each state right now".

Created 2026-08-19 in response to Batch 7 A2. Before this, four separate
call sites each derived counts independently:

  * /api/public/summary          (server.py:603)  — unfiltered, no test flag
  * /api/devices                 (server.py:365)  — client-side test filter
  * /api/admin/recheck/status    (routes_recheck.py:141) — unfiltered, count_documents
  * PDF _bucket_by_status        (reports_export.py:1371) — status_events-derived

That produced the "3 vs 1 vs 0 for the same person" defect Paul reported
(Batch 7 A2). Every count you show anywhere in the product now goes
through `compute_counts()` here, and `include_test` is the ONE knob.

Design decisions
----------------
1. **Current-state authority = `device_status`.** The event log is for
   history and narrative ("N told us they were trapped THIS AFTERNOON"),
   never for "how many are trapped right now." A person trapped before
   the report window is still trapped now, and the live dashboard, the
   PDF aggregate table, and the re-check panel must all agree with each
   other on that.
2. **Rescued wins.** If `rescued_at` is set, the row's effective status
   is "rescued" no matter what the raw `status` field says. This
   removed the map-marker duplication defect (green rescued tick
   overlapping the amber trapped triangle) — one row, one status.
3. **`include_test` is a single parameter, defaulted to False.** Test
   entries are never silently dropped from a legal record — the
   operational read excludes them, the raw audit read includes them,
   and both are labelled.
4. **No environment lookups here.** The module reads from the passed-in
   Mongo handle and nothing else, so it can be unit-tested against a
   fake DB without touching os.environ or dotenv.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Optional

from deps import is_test_device


# ── Effective status derivation ────────────────────────────────────────
# Called from THIS module and also exposed for the dashboard-side map
# marker renderer, so "one row, one status" holds on the front-end too.
def effective_status(row: Dict[str, Any]) -> str:
    """Rescued wins. Otherwise use the raw status. 'unknown' as fallback.

    This is the single classifier every caller must use — never look at
    row['status'] directly for display. See A2 Pattern 2 (two parts
    stating different facts): the map's rescued layer and severity layer
    were both reading row['status'] and independently deciding whether
    to draw, so a person marked rescued still got their trapped triangle
    drawn underneath.
    """
    if row.get("rescued_at"):
        return "rescued"
    st = row.get("status")
    return st if st in ("safe", "trapped", "not_responding", "rescued") else "unknown"


# ── Result shape ───────────────────────────────────────────────────────
@dataclass(frozen=True)
class Counts:
    """Immutable snapshot. Fields are stable — the dashboard, the PDFs
    and any external caller can rely on this shape."""
    total: int
    safe: int
    trapped: int
    trapped_red: int
    trapped_yellow: int
    trapped_green: int
    trapped_unknown: int
    rescued: int
    not_responding: int
    unknown: int
    # People currently marked as needing help (== trapped_total). The
    # re-check panel wants this named explicitly so the code reads
    # right ("if counts.needs_help > 0 ...").
    needs_help: int
    # How many test entries were filtered out to produce these numbers.
    # Returned so the dashboard can show "2 test entries hidden" when
    # the operator has "Show test entries" unchecked.
    test_filtered_out: int
    # Whether test entries were included (for stated provenance).
    include_test: bool

    def to_dict(self) -> Dict[str, int]:
        return asdict(self)


# ── The one function ───────────────────────────────────────────────────
async def compute_counts(db, include_test: bool = False) -> Counts:
    """Aggregate counts across all devices. Read this, and nothing else.

    Args:
      db: Motor Mongo handle.
      include_test: If True, test/synthetic devices are counted alongside
        real ones. Default False — the operator-facing and public views
        both hide test entries by default.

    Returns:
      Counts (see dataclass). All fields are integers.
    """
    rows = await _load_rows(db)
    return _bucket(rows, include_test=include_test)


async def compute_people(db, include_test: bool = False) -> List[Dict[str, Any]]:
    """Per-person rows, with the same test filter applied. Used by
    /api/devices — kept in this module so the filter decision lives in
    exactly one place, no matter what the row is used for.

    Returns the raw dicts (with `is_test` and `effective_status` added).
    Consumers are expected to project their own view fields.
    """
    rows = await _load_rows(db)
    out: List[Dict[str, Any]] = []
    for r in rows:
        r["is_test"] = is_test_device(r)
        r["effective_status"] = effective_status(r)
        if r["is_test"] and not include_test:
            continue
        out.append(r)
    return out


# ── Internals ──────────────────────────────────────────────────────────
async def _load_rows(db) -> List[Dict[str, Any]]:
    """Fetch every device_status row once. The projection includes just
    enough for classification and downstream display fields — full row
    fetches for detail views go through the existing /api/devices code.
    """
    return await db.device_status.find(
        {},
        # NOTE: keep this list minimal. Anything a caller doesn't need
        # is a potential leak (see the 2026-08-04 notes incident).
        {
            "_id": 0,
            "device_id": 1, "display_name": 1,
            "status": 1, "severity": 1, "mobility": 1, "egress": 1,
            "needs_extraction": 1,
            "latitude": 1, "longitude": 1, "accuracy_m": 1,
            "battery_pct": 1, "battery_state": 1, "platform": 1,
            "updated_at": 1,
            "rescued_at": 1, "rescued_by": 1,
            "pre_rescue_status": 1, "pre_rescue_severity": 1, "pre_rescue_mobility": 1,
            "synthetic": 1,
            "recheck": 1, "deteriorating": 1, "reports_improving": 1,
        },
    ).to_list(10000)


def _bucket(rows: List[Dict[str, Any]], *, include_test: bool) -> Counts:
    """Pure function — accepts rows and returns Counts. Extracted so
    tests can call it with fixed inputs and assert on the outputs,
    without setting up a Mongo instance.
    """
    kept = []
    filtered = 0
    for r in rows:
        if is_test_device(r):
            if not include_test:
                filtered += 1
                continue
        kept.append(r)

    total = len(kept)
    safe = trapped = rescued = not_responding = unknown = 0
    t_red = t_yellow = t_green = t_unknown = 0
    for r in kept:
        st = effective_status(r)
        if st == "safe":
            safe += 1
        elif st == "rescued":
            rescued += 1
        elif st == "trapped":
            trapped += 1
            sev = (r.get("severity") or "").lower()
            if sev == "red":
                t_red += 1
            elif sev == "yellow":
                t_yellow += 1
            elif sev == "green":
                t_green += 1
            else:
                t_unknown += 1
        elif st == "not_responding":
            not_responding += 1
        else:
            unknown += 1

    return Counts(
        total=total,
        safe=safe,
        trapped=trapped,
        trapped_red=t_red,
        trapped_yellow=t_yellow,
        trapped_green=t_green,
        trapped_unknown=t_unknown,
        rescued=rescued,
        not_responding=not_responding,
        unknown=unknown,
        needs_help=trapped,       # semantic alias
        test_filtered_out=filtered,
        include_test=include_test,
    )
