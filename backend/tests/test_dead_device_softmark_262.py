"""#262 blocker fix (Neo, 2026-08-20) — deployment scan correctly flagged
that the original _prune_dead_devices() hard-deleted push_devices rows
from an always-on background job (the re-check sweeper) with no human
action per deletion. Fixed to soft-mark (dead_token=True) instead of
delete, and re-registration clears the mark. This test locks that in.

The whole scenario runs on the suite-wide event loop (the `run_async`
fixture in conftest.py) — Motor pins itself to one loop for the life of
the process, so a private asyncio.run() here would fail in a full run.
"""
from __future__ import annotations

from dotenv import load_dotenv
load_dotenv("/app/backend/.env")

from apns import _prune_dead_devices, ApnsResult
from deps import db

TEST_USER_ID = "qg-test-262-deadmark"


async def _scenario():
    # Clean slate.
    await db.push_devices.delete_one({"user_id": TEST_USER_ID})
    await db.push_devices.insert_one({
        "user_id": TEST_USER_ID,
        "platform": "ios",
        "device_token": "e" * 64,
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
    })

    fake_result = ApnsResult(
        user_id=TEST_USER_ID, token_fingerprint="e"*8 + "…" + "e"*8,
        environment="production", status_code=410, apns_id="x",
        apns_unique_id=None, reason="Unregistered", delivered=False,
        duration_ms=5,
    )
    marked = await _prune_dead_devices(db, [fake_result])
    assert marked == 1

    row = await db.push_devices.find_one({"user_id": TEST_USER_ID})
    # THE FIX: the row must still exist (not hard-deleted) ...
    assert row is not None, "row was deleted — the blocker fix regressed"
    # ... and must be marked dead.
    assert row.get("dead_token") is True
    assert row.get("dead_token_reason") == "Unregistered"

    # Re-registering (simulated as the same $unset the real endpoint does)
    # must clear the mark — a device that comes back alive must not stay
    # excluded forever.
    await db.push_devices.update_one(
        {"user_id": TEST_USER_ID},
        {"$set": {"device_token": "f" * 64, "updated_at": "2026-01-02T00:00:00+00:00"},
         "$unset": {"dead_token": "", "dead_token_reason": "", "dead_token_at": ""}},
    )
    row2 = await db.push_devices.find_one({"user_id": TEST_USER_ID})
    assert not row2.get("dead_token")

    await db.push_devices.delete_one({"user_id": TEST_USER_ID})


def test_dead_device_is_marked_not_deleted_and_can_recover(run_async):
    run_async(_scenario)
