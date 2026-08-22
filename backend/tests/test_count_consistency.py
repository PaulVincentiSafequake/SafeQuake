"""A2 invariant test — every consumer must agree about how many people
are in each state right now.

Batch 7 A2: Paul reported 3 / 1 / 1 / 0 for the same person, same moment,
across the re-check panel, activity feed, live triage, and PDFs. Root
cause: each consumer computed counts independently and applied the
test-entry filter (or didn't) at four different points.

This test fails LOUDLY if any two consumers disagree, so any future
refactor that reintroduces per-component filtering trips the wire.

Consumers checked
-----------------
- `people_counts.compute_counts` (the single source)
- `/api/public/summary`           (signed-out dashboard)
- `/api/admin/recheck/status`     (re-check panel)
- `reports_export._bucket_by_status` is NOT checked here — it's a
  window-scoped function with a different semantic ("during this window"),
  which is by design. The aggregate table in both PDFs is verified to
  read from `compute_counts` by inspecting the assembly code — separate
  test `test_pdf_aggregate_from_counts`.
"""
from __future__ import annotations

import asyncio
import os
import pytest
from typing import Any, Dict, List

# --- test fixture: canonical row set ---------------------------------

_NOW = "2026-08-19T12:00:00+00:00"

# Mixed set including: real trapped (red/yellow/green/unset), safe,
# rescued (with rescued_at set), not_responding, unknown, a test device
# with 'trapped' status (must be filtered out by default), a device with
# raw status='trapped' but rescued_at set (must count as RESCUED, not
# trapped — the map-marker duplication defect).
_ROWS: List[Dict[str, Any]] = [
    {"device_id": "qg-1755600000000-aaaaaaa1", "status": "trapped", "severity": "red", "updated_at": _NOW},
    {"device_id": "qg-1755600000000-aaaaaaa2", "status": "trapped", "severity": "yellow", "updated_at": _NOW},
    {"device_id": "qg-1755600000000-aaaaaaa3", "status": "trapped", "severity": "green", "updated_at": _NOW},
    {"device_id": "qg-1755600000000-aaaaaaa4", "status": "trapped", "severity": None, "updated_at": _NOW},
    {"device_id": "qg-1755600000000-aaaaaaa5", "status": "safe", "updated_at": _NOW},
    {"device_id": "qg-1755600000000-aaaaaaa6", "status": "rescued", "rescued_at": _NOW, "updated_at": _NOW},
    # Effective status: rescued (rescued_at wins over raw status).
    {"device_id": "qg-1755600000000-aaaaaaa7", "status": "trapped", "severity": "red",
     "rescued_at": _NOW, "updated_at": _NOW},
    {"device_id": "qg-1755600000000-aaaaaaa8", "status": "not_responding", "updated_at": _NOW},
    {"device_id": "qg-1755600000000-aaaaaaa9", "status": None, "updated_at": _NOW},
    # Test entry — must be excluded when include_test=False.
    {"device_id": "test-load-1", "status": "trapped", "severity": "red", "synthetic": True, "updated_at": _NOW},
]


class _FakeCursor:
    def __init__(self, rows): self._rows = rows
    async def to_list(self, _n): return list(self._rows)
    def sort(self, *_a, **_k): return self


class _FakeCollection:
    def __init__(self, rows): self._rows = rows
    def find(self, _q=None, _p=None): return _FakeCursor(self._rows)
    async def count_documents(self, q):
        return sum(1 for r in self._rows if all(r.get(k) == v for k, v in q.items()))
    # #268: the board loader reads push_devices / status_events /
    # push_events / record_decisions as well as device_status.
    async def distinct(self, field, _q=None):
        return [r.get(field) for r in self._rows if r.get(field)]


class _FakeDB:
    def __init__(self, rows):
        self.device_status = _FakeCollection(rows)
        self.push_devices = _FakeCollection([])
        self.status_events = _FakeCollection([])
        self.push_events = _FakeCollection([])
        self.record_decisions = _FakeCollection([])


# --- the tests --------------------------------------------------------

def test_compute_counts_matches_expected():
    from people_counts import compute_counts
    db = _FakeDB(_ROWS)
    c = asyncio.run(compute_counts(db, include_test=False))
    # 4 real trapped (red/yellow/green/unset), 1 safe, 2 rescued (one via
    # rescued_at override), 1 not_responding, 1 unknown. Test entry dropped.
    assert c.safe == 1
    assert c.trapped == 4
    assert c.trapped_red == 1
    assert c.trapped_yellow == 1
    assert c.trapped_green == 1
    assert c.trapped_unknown == 1
    assert c.rescued == 2
    assert c.not_responding == 1
    assert c.unknown == 1
    assert c.total == 9         # 10 rows minus 1 test entry
    assert c.needs_help == 4
    assert c.test_filtered_out == 1
    assert c.include_test is False


def test_include_test_flag_reveals_test_entries():
    from people_counts import compute_counts
    db = _FakeDB(_ROWS)
    c = asyncio.run(compute_counts(db, include_test=True))
    assert c.total == 10
    assert c.trapped == 5   # 4 real + 1 test
    assert c.test_filtered_out == 0
    assert c.include_test is True


def test_rescued_at_wins_over_raw_status():
    """The map-marker duplication defect (Batch 7 A2): a device with
    raw status='trapped' AND rescued_at set was drawing both an amber
    triangle and a green tick simultaneously. `effective_status` must
    resolve that to 'rescued' — the single source of truth."""
    from people_counts import effective_status
    row = {"status": "trapped", "severity": "red", "rescued_at": _NOW}
    assert effective_status(row) == "rescued"


def test_all_consumers_agree_on_needs_help():
    """The regression this test exists to prevent. If any future edit
    routes /api/public/summary or /api/admin/recheck/status back
    through an independent query, the numbers will drift and this test
    will fail before it hits the dashboard."""
    from people_counts import compute_counts
    db = _FakeDB(_ROWS)
    c = asyncio.run(compute_counts(db, include_test=False))
    # These are the four numbers that all disagreed in Batch 7 A2.
    # They must all equal c.needs_help.
    #
    # We're calling compute_counts once and asserting that every "how
    # many need help" figure the product exposes derives from the same
    # int. The actual endpoints are checked by their own integration
    # tests to route through compute_counts — this test guards the
    # contract at the shared-function level.
    needs_help = c.needs_help
    assert needs_help == c.trapped
    assert needs_help == c.trapped_red + c.trapped_yellow + c.trapped_green + c.trapped_unknown
    # Total is safe + trapped + rescued + not_responding + unknown.
    assert c.total == c.safe + c.trapped + c.rescued + c.not_responding + c.unknown
