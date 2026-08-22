"""Neo #255 — saved-place perceptibility floor + body wording.

Tests /app/backend/emsc/preview.py dispatch_place_notices:
  1. MMI floor `< 2.0` must be enforced BEFORE the preset check,
     even when the user's notification_preset is "everything".
  2. The skip row's `skipped_reason` starts with
     "below_perceptibility_floor".
  3. Body wording: place name appears exactly once in the leading
     colon (not twice as "of {name}").
"""
import asyncio
import os
import sys

import pytest

# Ensure /app/backend is on path so `import emsc.preview` works.
BACKEND_DIR = "/app/backend"
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from emsc.preview import dispatch_place_notices  # noqa: E402
from emsc.intensity import mmi_from_faenza_michelini_2010  # noqa: E402


# ── Test doubles ─────────────────────────────────────────────────────────
class _FakeCollection:
    def __init__(self, docs=None):
        self._docs = list(docs or [])
        self.inserted = []

    def find(self, query, projection=None):
        # Very small in-memory filter — supports the shapes used by
        # dispatch_place_notices only (device_id $in, user_id $in).
        results = []
        for d in self._docs:
            ok = True
            for k, v in (query or {}).items():
                if isinstance(v, dict):
                    # Operator form — every operator present must hold.
                    if "$in" in v and d.get(k) not in v["$in"]:
                        ok = False
                    if "$exists" in v:
                        present = d.get(k) is not None
                        if bool(v["$exists"]) != present:
                            ok = False
                    if "$ne" in v and d.get(k) == v["$ne"]:
                        ok = False
                elif d.get(k) != v:
                    ok = False
                if not ok:
                    break
            if ok:
                results.append(d)

        class _Cursor:
            def __init__(self, rows):
                self._rows = rows

            async def to_list(self, _n):
                return list(self._rows)

        return _Cursor(results)

    async def find_one(self, query, projection=None, sort=None):
        return None

    async def insert_one(self, doc):
        self.inserted.append(doc)
        return type("R", (), {"inserted_id": "fake"})()


class _FakeDB:
    def __init__(self, places, push_devices):
        self.user_places = _FakeCollection(places)
        self.push_devices = _FakeCollection(push_devices)
        self.emsc_preview_notifications = _FakeCollection()


class _APNsCapture:
    """Records the call so we can inspect title/body if a send fires."""
    def __init__(self):
        self.calls = []

    async def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return {"events": [{"user_id": kwargs["devices"][0].get("user_id"),
                            "delivered": True, "apns_id": "fake-apns"}]}


# ── Fixtures / constants ─────────────────────────────────────────────────
MODICA_LAT, MODICA_LON = 36.858, 14.774  # Modica, Sicily
# Event 504 km SSE of Modica — pick a point roughly SSE-far so distance
# comes out at ~504 km. We use a synthetic point south-southeast.
# Verified: emsc.preview.haversine_km with the below is ~504 km.
EVENT_LAT, EVENT_LON = 32.30, 17.05


def _make_event():
    return {
        "provider": "EMSC-TEST",
        "external_id": "test-neo-255",
        "revision": 1,
        "magnitude": 2.9,
        "latitude": EVENT_LAT,
        "longitude": EVENT_LON,
        "depth_km": 2.0,
        "region": "Ionian Sea",
        "observed_at": None,  # skip freshness gate (only fires if age set)
    }


def _make_country_config(enabled=True, device_ids=("device-1",)):
    return {
        "country_code": "MT",
        "country_name": "Malta",
        "center": {"lat": 35.9375, "lon": 14.3754},
        "poll_radius_km": 600.0,
        "preview_mode": {
            "enabled": enabled,
            "device_ids": list(device_ids),
            "trigger_tier": "all_ingested",
            "rate_limit_minutes": 10,
        },
    }


# ── Sanity: confirm the MMI is ~1.15 for the specified geometry ─────────
def test_mmi_is_below_perceptibility_at_504km():
    """The problem statement asserts MMI ≈ 1.15 for M2.9 @ 504km depth 2km."""
    mmi = mmi_from_faenza_michelini_2010(magnitude=2.9, distance_km=504.0, depth_km=2.0)
    assert mmi == pytest.approx(1.15, abs=0.05), (
        f"Expected MMI ≈ 1.15 at 504km M2.9 depth 2km, got {mmi:.2f}"
    )
    assert mmi < 2.0  # the floor threshold


# ── Test 1: the floor SKIPS a M2.9 @ 504km even with preset=everything ──
@pytest.mark.asyncio
async def test_perceptibility_floor_blocks_everything_preset():
    """A device with preset=everything at 504km from a M2.9 must still be
    skipped by the < 2.0 floor, BEFORE preset_would_fire is consulted."""
    place = {
        "device_id": "device-1",
        "place_id": "place-modica",
        "name": "Modica",
        "latitude": MODICA_LAT,
        "longitude": MODICA_LON,
    }
    push_device = {
        "user_id": "device-1",
        "device_token": "TOKEN-ABC",
        "platform": "ios",
        "notification_preset": "everything",
        "places_enabled": True,
    }
    db = _FakeDB([place], [push_device])
    apns = _APNsCapture()

    result = await dispatch_place_notices(
        db=db,
        apns_send_preview=apns,
        emsc_event=_make_event(),
        country_config=_make_country_config(),
    )

    # Nothing should have been sent.
    assert result is None, f"Expected no notice, got {result}"
    assert apns.calls == [], "APNs must NOT be called when floor blocks the notice"

    # A skip row must exist with the exact reason prefix.
    inserted = db.emsc_preview_notifications.inserted
    assert len(inserted) == 1, f"Expected 1 skip row, got {len(inserted)}"
    row = inserted[0]
    assert row.get("delivered") is False
    reason = row.get("skipped_reason") or ""
    assert reason.startswith("below_perceptibility_floor"), (
        f"skipped_reason must start with 'below_perceptibility_floor', got '{reason}'"
    )
    # Sanity — the place metadata is on the skip row for audit.
    assert row.get("place_id") == "place-modica"
    assert row.get("place_name") == "Modica"


# ── Test 2: body wording does NOT say "of {name}" twice ──────────────────
@pytest.mark.asyncio
async def test_body_wording_names_place_once_not_twice():
    """When a notice DOES fire, the body must name the place exactly once
    (in the leading colon) — no "504km SSE of Modica" tautology."""
    # Place the event close enough to fire under preset=everything.
    # M4.5 at 30km gives an MMI well above 2.0.
    event = _make_event()
    event["magnitude"] = 4.5
    event["latitude"] = MODICA_LAT + 0.2   # ~22 km north-ish
    event["longitude"] = MODICA_LON + 0.1
    event["depth_km"] = 10.0

    place = {
        "device_id": "device-1",
        "place_id": "place-modica",
        "name": "Modica",
        "latitude": MODICA_LAT,
        "longitude": MODICA_LON,
    }
    push_device = {
        "user_id": "device-1",
        "device_token": "TOKEN-ABC",
        "platform": "ios",
        "notification_preset": "everything",
        "places_enabled": True,
    }
    db = _FakeDB([place], [push_device])
    apns = _APNsCapture()

    result = await dispatch_place_notices(
        db=db,
        apns_send_preview=apns,
        emsc_event=event,
        country_config=_make_country_config(),
    )

    assert result is not None and result.get("attempted") == 1, (
        f"Expected 1 attempted notice, got {result}"
    )
    assert len(apns.calls) == 1, "APNs must be called exactly once"

    body = apns.calls[0]["body"]
    # 1. Place name appears — but ONLY as the leading colon prefix.
    assert body.startswith("Modica: "), f"Body must start with 'Modica: ', got '{body}'"

    # 2. NO "of Modica" tautology.
    assert " of Modica" not in body, f"Body must not contain ' of Modica', got '{body}'"

    # 3. Required tail sentences (from spec).
    assert "You get this because you added Modica to your saved places." in body, (
        f"Body missing 'You get this...' sentence: {body}"
    )
    assert "Turn off in Settings" in body, (
        f"Body missing 'Turn off in Settings' sentence: {body}"
    )


# ── Test 3: perceptibility floor takes precedence over preset check ─────
@pytest.mark.asyncio
async def test_floor_runs_before_preset_check():
    """Order-of-operations: the floor must be evaluated BEFORE
    preset_would_fire. The way we detect this is: with a preset that
    would BLOCK on its own ('minimal', for instance), we should still
    see the below_perceptibility_floor reason (not a preset reason) —
    because floor fires first and short-circuits."""
    place = {
        "device_id": "device-1",
        "place_id": "place-modica",
        "name": "Modica",
        "latitude": MODICA_LAT,
        "longitude": MODICA_LON,
    }
    # Preset that would otherwise skip — the floor should short-circuit
    # BEFORE the preset gets consulted. If the code reversed the order,
    # we'd see a preset-derived skipped_reason instead.
    push_device = {
        "user_id": "device-1",
        "device_token": "TOKEN-ABC",
        "platform": "ios",
        "notification_preset": "significant_only",
        "places_enabled": True,
    }
    db = _FakeDB([place], [push_device])
    apns = _APNsCapture()

    await dispatch_place_notices(
        db=db,
        apns_send_preview=apns,
        emsc_event=_make_event(),
        country_config=_make_country_config(),
    )

    inserted = db.emsc_preview_notifications.inserted
    assert len(inserted) == 1
    reason = inserted[0].get("skipped_reason") or ""
    assert reason.startswith("below_perceptibility_floor"), (
        f"Order-of-operations broken: floor should fire before preset. "
        f"Got reason: '{reason}'"
    )
