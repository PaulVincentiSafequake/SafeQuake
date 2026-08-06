"""
EMSC soak-continuity migration test.

## What this protects against

The EMSC monitoring pipeline has been in Phase 1 "shadow soak" for 7+
days at the point of migration prep. That continuity data is the
evidence that our poller is production-ready — without it we'd have
to reset the soak clock, which means another 7+ days of not being
able to enable real alerts.

This test asserts, given two snapshots of /api/admin/emsc/continuity:
  - one captured BEFORE the Fly.io migration (`SRC_CONTINUITY`)
  - one captured AFTER  the Fly.io migration (`DST_CONTINUITY`)

...that the migration preserved the soak. Specifically:

  1. `soak_started_at` is UNCHANGED. If someone (a startup path in
     `server.py`, a bug in `emsc_poller.py`, or an over-eager reset in
     `migrate_mongo.py`) wiped or re-seeded the meta document, the
     start time would jump forward and we'd be pretending to have less
     history than we actually do.

  2. The post-migration `emsc_poller_gaps` array contains AT MOST ONE
     new gap compared to the pre-migration snapshot, and that gap's
     duration is BOUNDED by MAX_ACCEPTABLE_MIGRATION_GAP_SECONDS
     (default: 45 min). Anything longer than that means the migration
     took longer than planned and we should retune the runbook —
     or the poller crashed during startup on Fly.io.

  3. The total observed uptime (excluding gaps) has NOT gone
     backwards. It can go up (more time elapsed) but never down (which
     would indicate we lost history).

## How this test is used

- **Before cutover:** run against a single snapshot (SRC only) to
  smoke-test that `/api/admin/emsc/continuity` returns the expected
  shape. Skip cross-file comparisons.

- **After cutover:** set BOTH env vars, run the full suite.
  Any failure = the migration didn't preserve the soak → do not tear
  down Emergent, roll back mobile URL to the old backend, and
  investigate.

The test intentionally does NOT talk to a real Mongo — it operates on
JSON snapshots so it can be re-run after the fact for auditability.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest


# The migration runbook budgets ~30 minutes of downtime; 45 minutes is
# the outer envelope beyond which the migration is considered slow
# enough to warrant a post-mortem. Bump this if a long-planned outage
# is expected (e.g. Atlas re-sharding), but the default is intentionally
# tight.
MAX_ACCEPTABLE_MIGRATION_GAP_SECONDS = 45 * 60


@pytest.fixture(scope="module")
def src_snapshot() -> dict | None:
    """Continuity snapshot taken BEFORE the Fly.io migration."""
    path = os.environ.get("SRC_CONTINUITY")
    if not path:
        pytest.skip("SRC_CONTINUITY env var not set")
    p = Path(path)
    if not p.exists():
        pytest.skip(f"SRC_CONTINUITY file {path!r} does not exist")
    return json.loads(p.read_text())


@pytest.fixture(scope="module")
def dst_snapshot() -> dict | None:
    """Continuity snapshot taken AFTER the Fly.io migration."""
    path = os.environ.get("DST_CONTINUITY")
    if not path:
        pytest.skip("DST_CONTINUITY env var not set")
    p = Path(path)
    if not p.exists():
        pytest.skip(f"DST_CONTINUITY file {path!r} does not exist")
    return json.loads(p.read_text())


# -----------------------------------------------------------------------------
# Shape-only checks (run on SRC alone, no cross-file comparison).
# -----------------------------------------------------------------------------

def test_src_snapshot_has_expected_shape(src_snapshot: dict):
    """Sanity check the endpoint hasn't drifted since we baselined it."""
    for key in ("soak_started_at", "gaps", "total_uptime_seconds"):
        assert key in src_snapshot, (
            f"pre-migration snapshot missing key {key!r}. If the API "
            f"shape changed, update this test AND the runbook."
        )
    assert isinstance(src_snapshot["gaps"], list), "gaps must be an array"


# -----------------------------------------------------------------------------
# Cross-snapshot invariants (require both files).
# -----------------------------------------------------------------------------

def test_soak_started_at_preserved(src_snapshot: dict, dst_snapshot: dict):
    """The soak start time must not move during migration.

    If this fails, the meta document was clobbered — the migration
    fetched a fresh Mongo instance without the pre-existing meta row,
    OR the FastAPI startup path re-seeded meta unconditionally.

    Recovery: restore `emsc_soak_meta` from the migration JSONL dump
    (`migration-dump/emsc_soak_meta.jsonl`) and re-run this test.
    """
    src_start = src_snapshot["soak_started_at"]
    dst_start = dst_snapshot["soak_started_at"]
    assert src_start == dst_start, (
        f"soak_started_at changed from {src_start!r} to {dst_start!r} "
        "during migration. The pre-existing meta document was not "
        "preserved — see docstring for recovery steps."
    )


def test_total_uptime_never_decreases(src_snapshot: dict, dst_snapshot: dict):
    """Uptime can grow (more time elapsed) but must never shrink.

    A shrink means we lost recorded uptime history — likely a Mongo
    collection wasn't fully migrated, or the compute changed. Either
    is a red flag.
    """
    src_uptime = float(src_snapshot["total_uptime_seconds"])
    dst_uptime = float(dst_snapshot["total_uptime_seconds"])
    assert dst_uptime >= src_uptime, (
        f"total_uptime_seconds went backwards: {src_uptime} -> {dst_uptime}. "
        "This should be impossible if the migration preserved emsc_soak_meta. "
        "Investigate the meta doc directly in the destination Mongo."
    )


def test_at_most_one_new_gap_from_migration(src_snapshot: dict, dst_snapshot: dict):
    """The migration window itself is allowed to produce ONE gap, but
    not multiple gaps — multiple gaps would mean the Fly.io poller
    started, crashed, started again, etc.
    """
    src_gaps = src_snapshot["gaps"]
    dst_gaps = dst_snapshot["gaps"]
    new_gap_count = len(dst_gaps) - len(src_gaps)
    assert new_gap_count >= 0, (
        f"Gaps disappeared during migration: {len(src_gaps)} -> {len(dst_gaps)}. "
        "Gaps should be append-only. Investigate the destination Mongo."
    )
    assert new_gap_count <= 1, (
        f"Migration produced {new_gap_count} new gaps, expected at most 1. "
        "The Fly.io poller likely crashed and restarted, or the migration "
        "runbook was not followed atomically. Check Fly.io machine logs "
        "for restart events."
    )


def test_migration_gap_within_budget(src_snapshot: dict, dst_snapshot: dict):
    """If the migration produced a new gap, its duration must be within
    the runbook's downtime budget. Anything longer is a signal that
    something went wrong on cutover day.
    """
    src_gaps = src_snapshot["gaps"]
    dst_gaps = dst_snapshot["gaps"]
    if len(dst_gaps) == len(src_gaps):
        # No new gap. Perfect world.
        return
    # Identify the new gap: it's the last one in dst_gaps not present in
    # src_gaps. Use started_at as the identity key.
    src_starts = {g.get("started_at") for g in src_gaps}
    new_gaps = [g for g in dst_gaps if g.get("started_at") not in src_starts]
    assert len(new_gaps) == 1, "Assertion mismatch — see test_at_most_one_new_gap_from_migration"
    gap = new_gaps[0]
    gap_duration = float(gap.get("duration_seconds", 0))
    assert gap_duration <= MAX_ACCEPTABLE_MIGRATION_GAP_SECONDS, (
        f"Migration gap lasted {gap_duration:.0f}s "
        f"(budget: {MAX_ACCEPTABLE_MIGRATION_GAP_SECONDS}s). "
        "The cutover took longer than planned. Post-mortem the runbook, "
        "identify the step that overran, tune the budget if it was "
        "genuinely necessary — but if it can be shrunk, shrink it, so "
        "the next migration is faster."
    )


def test_no_gap_predates_soak_start(dst_snapshot: dict):
    """Sanity check: a gap can't start before soak_started_at.

    If it does, someone forged a gap entry manually or `soak_started_at`
    was moved backwards (which we protected against in
    test_soak_started_at_preserved, but this catches gap-side bugs).
    """
    soak_start = dst_snapshot["soak_started_at"]
    for gap in dst_snapshot["gaps"]:
        gs = gap.get("started_at")
        if gs is None:
            continue
        assert gs >= soak_start, (
            f"Gap started at {gs!r} which is BEFORE soak_started_at {soak_start!r}. "
            "The meta document is inconsistent — investigate immediately."
        )
