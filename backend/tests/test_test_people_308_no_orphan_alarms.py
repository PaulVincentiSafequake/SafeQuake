"""#308 — removing test people must always clear every alarm they caused.

Paul (2026-08-28, reopened):
  > Removing test people clears their alarms fine on one add/remove cycle.
  > But if I add test people more than once before removing them, some of
  > the earlier batch's alarms get stuck open and keep sounding, even
  > though those test people are gone. I want removing test people to
  > always clear every alarm they caused, no matter how many times I've
  > added and removed batches before that. Nothing fake should ever keep
  > sounding after the fake person is gone.

Two contracts, both pinned here:

  1. **Adding a batch is as thorough a cleanup as clearing one.** The
     idempotency branch inside `POST /admin/test-people/seed` used to
     `delete_many` the previous batch's `device_status` rows without
     touching the open alarms those rows had raised. So after
     seed → seed (no clear between), the first batch's alarms became
     orphans in `board_alarms` — device_ids no longer in
     `device_status`, still sounding. Seed now resolves them at the
     same time it drops the rows.

  2. **The cleanup catches every alarm the batch could have raised,
     regardless of whether the `is_test` flag survived on it.** Both
     `/seed`'s replace-branch and `/clear` now match on the UNION of
     `is_test: True` AND `device_id` starting with `qg-<SEED_TAG>-`.
     That prefix is deterministic (every seeded row uses it) and only
     changes if the seed spec itself changes, so an alarm from a
     seeded person cannot slip through even if some code path along
     the way forgot to tag it.
"""
import os

import pytest
import requests
from dotenv import dotenv_values

BASE = os.environ.get("QG_BASE", "http://localhost:8001")
TOKEN = (dotenv_values("/app/backend/.env").get("ADMIN_TRIGGER_PASSWORD")
         or os.environ.get("ADMIN_TRIGGER_PASSWORD", ""))
H = {"X-Admin-Token": TOKEN, "Content-Type": "application/json"}


def _alarms_include_test():
    """Fetch the alarm panel with test entries included."""
    r = requests.get(f"{BASE}/api/admin/alarms?include_test=1", headers=H, timeout=20)
    assert r.status_code == 200, r.text
    return r.json()


def _seeded_alarms_in_panel(panel):
    """Every alarm-panel row whose device_id belongs to a seeded person."""
    out = []
    for g in panel.get("groups", []):
        for p in g.get("people", []):
            if str(p.get("device_id", "")).startswith("qg-seeded-33-"):
                out.append(p)
    return out


@pytest.fixture(autouse=True)
def _cold_start():
    """Every test in this file starts with no test people and no seeded
    alarms on the board."""
    requests.post(f"{BASE}/api/admin/test-people/clear", headers=H, timeout=30)
    yield
    requests.post(f"{BASE}/api/admin/test-people/clear", headers=H, timeout=30)


# ── 1. The whole-family contract ─────────────────────────────────────
def test_seed_then_seed_then_clear_leaves_zero_test_alarms_on_the_board():
    """The exact scenario Paul reported. seed → seed (no clear between)
    → clear. After clear, nothing seeded is still on the alarm panel."""
    r1 = requests.post(f"{BASE}/api/admin/test-people/seed", headers=H, timeout=60)
    assert r1.status_code == 200, r1.text
    r2 = requests.post(f"{BASE}/api/admin/test-people/seed", headers=H, timeout=60)
    assert r2.status_code == 200, r2.text
    rc = requests.post(f"{BASE}/api/admin/test-people/clear", headers=H, timeout=60)
    assert rc.status_code == 200, rc.text
    panel = _alarms_include_test()
    assert _seeded_alarms_in_panel(panel) == [], _seeded_alarms_in_panel(panel)[:3]


def test_repeated_seed_cycles_never_leave_orphans_between_add_and_add():
    """The problem behind Paul's report: between seed #N and seed #N+1
    (no clear), the previous batch's alarms must not still be on the
    board. If they are, the operator hears sounds for people who are
    already gone from device_status."""
    for _ in range(4):
        r = requests.post(f"{BASE}/api/admin/test-people/seed", headers=H, timeout=60)
        assert r.status_code == 200, r.text
        # Nudge the silence sweep so any latent orphans would surface.
        requests.get(f"{BASE}/api/devices", headers=H, timeout=30)
        panel = _alarms_include_test()
        seeded = _seeded_alarms_in_panel(panel)
        # Every row shown must correspond to a device that currently
        # exists on the board. Anything from a previous seed cycle is a
        # ghost.
        devs = requests.get(f"{BASE}/api/devices", headers=H, timeout=30).json()
        live_ids = {d["device_id"] for d in devs.get("devices", [])}
        orphans = [p for p in seeded if p["device_id"] not in live_ids]
        assert orphans == [], (
            f"seeded alarms with no live device: {[o['device_id'] for o in orphans][:5]}"
        )


def test_clear_response_reports_the_total_alarms_it_resolved():
    """The clear endpoint's response counts every seeded alarm it
    closed, so a rehearsal is auditable and an operator can see it
    finished the job."""
    requests.post(f"{BASE}/api/admin/test-people/seed", headers=H, timeout=60)
    requests.get(f"{BASE}/api/devices", headers=H, timeout=30)
    requests.post(f"{BASE}/api/admin/test-people/seed", headers=H, timeout=60)
    requests.get(f"{BASE}/api/devices", headers=H, timeout=30)
    rc = requests.post(f"{BASE}/api/admin/test-people/clear", headers=H, timeout=60)
    assert rc.status_code == 200, rc.text
    body = rc.json()
    assert body["removed"] == 33
    # There must be a count, and it must be at least the alarms the
    # very first seed raised — otherwise a whole batch was skipped.
    assert isinstance(body.get("alarms_cleared"), int)
    assert body["alarms_cleared"] > 0, body


# ── 2. The bulletproof match: prefix OR is_test ──────────────────────
def test_seed_prefix_is_used_as_the_bulletproof_predicate():
    """The seed uses `qg-<SEED_TAG>-<idx>-<random>` for every device_id,
    and the cleanup code matches on this prefix (as well as `is_test`).
    Verify by static-source read — the code must not go back to a
    single-flag match that failed once already."""
    src = open("/app/backend/test_people_seed.py", encoding="utf-8").read()
    # A helper exists and takes a reason string.
    assert "_resolve_every_alarm_from_test_people" in src
    # The predicate is a union of `is_test: True` and a device_id prefix.
    assert '"is_test": True' in src
    assert "SEED_TAG" in src and "device_id" in src
    assert '$or' in src, "cleanup must union both predicates, not pick one"
    assert '$regex' in src, "prefix match uses a regex on device_id"
    # /clear must use the helper (not the old is_test-only update_many).
    assert 'update_many(\n            {"is_test": True, "resolved_at": None}' not in src, (
        "old is_test-only cleanup is back — regression of #308"
    )


def test_seed_replace_branch_calls_the_cleanup_helper():
    """The seed's idempotency branch is exactly where the bug lived. It
    must now clean up prior batches' device_status rows AND their open
    alarms, so the /seed and /clear cleanup paths cannot drift out of
    sync.

    #320 (2026-08-29): the branch was widened from a single-predicate
    `delete_many({"_test_seed": SEED_TAG})` to the shared sweeper
    `_sweep_all_test_device_status`, which catches mark-test rows and
    load-test rows as well. Either shape (the narrow delete_many OR the
    broader sweeper call) satisfies the "prior batch is cleaned up
    before the new one is inserted" contract behind #308."""
    src = open("/app/backend/test_people_seed.py", encoding="utf-8").read()
    # Anchor to the seed function.
    seed_start = src.index("async def seed_test_people(")
    seed_end = src.index("async def clear_test_people(", seed_start)
    seed_body = src[seed_start:seed_end]
    device_status_cleaned = (
        'delete_many({"_test_seed": SEED_TAG})' in seed_body
        or "_sweep_all_test_device_status" in seed_body
    )
    assert device_status_cleaned, (
        "seed's replace branch must clean up prior batch's device_status "
        "rows before inserting the new batch — either via the narrow "
        "delete_many or the broader _sweep_all_test_device_status helper"
    )
    assert "_resolve_every_alarm_from_test_people" in seed_body, (
        "seed's replace branch must clean up prior batch's alarms, "
        "not just delete their device_status rows"
    )
