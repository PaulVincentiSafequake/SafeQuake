"""Tests for GET /api/seismic-map/events (public in-app map).

Covers:
- Response shape & attribution wording (EMSC licence compliance).
- Bbox constants (Mediterranean).
- Silent clamping of window_hours and limit.
- Bbox filter (no events outside Mediterranean leak through).
- Time window filter (no events older than window_hours leak through).
- Newest-first ordering.
- Same-provider revision dedup (keep highest revision).
- Cross-provider dedup (EMSC + USGS at same location/minute → 1 merged row).
- Public/no-auth access.
- Payload minimization (no `raw` field).

Seeded rows are prefixed with `TEST_MAP_` on `external_id` for cleanup.
"""
from __future__ import annotations

import os
import pytest
import requests
from datetime import datetime, timedelta, timezone

from motor.motor_asyncio import AsyncIOMotorClient
from dotenv import load_dotenv
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(BACKEND_ROOT / ".env")
# Public backend URL lives in the frontend .env for Expo.
load_dotenv(BACKEND_ROOT.parent / "frontend" / ".env")

BASE_URL = os.environ.get("EXPO_PUBLIC_BACKEND_URL") or os.environ.get("EXPO_BACKEND_URL")
assert BASE_URL, "EXPO_PUBLIC_BACKEND_URL (or EXPO_BACKEND_URL) must be set"
BASE_URL = BASE_URL.rstrip("/")

MONGO_URL = os.environ["MONGO_URL"]
DB_NAME = os.environ["DB_NAME"]

ENDPOINT = f"{BASE_URL}/api/seismic-map/events"

# ── Fixtures ─────────────────────────────────────────────────────────────
@pytest.fixture(scope="module")
def api():
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    return s


@pytest.fixture
async def mongo():
    client = AsyncIOMotorClient(MONGO_URL)
    db = client[DB_NAME]
    yield db
    # Cleanup any TEST_MAP_ rows we seeded.
    await db.emsc_events.delete_many({"external_id": {"$regex": "^TEST_MAP_"}})
    client.close()


def _seed_row(external_id, provider, observed_at, lat, lon, magnitude,
              revision=1, magnitude_type="Mw", depth_km=10.0, region="Test Region"):
    return {
        "provider": provider,
        "external_id": external_id,
        "revision": revision,
        "observed_at": observed_at,
        "magnitude": magnitude,
        "magnitude_type": magnitude_type,
        "latitude": lat,
        "longitude": lon,
        "depth_km": depth_km,
        "region": region,
        "raw": {"note": "seeded test row"},
    }


# ── Shape / attribution / bbox constants ─────────────────────────────────
class TestShape:
    def test_returns_200_no_auth(self, api):
        r = api.get(ENDPOINT)
        assert r.status_code == 200

    def test_body_has_required_keys(self, api):
        body = api.get(ENDPOINT).json()
        for k in ("generated_at", "window_hours", "bbox", "count", "events", "attribution"):
            assert k in body, f"missing key: {k}"

    def test_default_window_is_168(self, api):
        body = api.get(ENDPOINT).json()
        assert body["window_hours"] == 168

    def test_attribution_exact_string(self, api):
        body = api.get(ENDPOINT).json()
        assert body["attribution"] == "Data: EMSC (emsc-csem.org) & USGS (earthquake.usgs.gov)"

    def test_bbox_constants(self, api):
        body = api.get(ENDPOINT).json()
        assert body["bbox"] == {
            "lat_min": 30.0, "lat_max": 47.0,
            "lon_min": -6.0, "lon_max": 37.0,
        }

    def test_count_matches_events_length(self, api):
        body = api.get(ENDPOINT).json()
        assert body["count"] == len(body["events"])

    def test_no_raw_field_in_events(self, api):
        body = api.get(ENDPOINT).json()
        for e in body["events"]:
            assert "raw" not in e, "raw payload must be stripped for map response"


# ── window_hours parameter behaviour ─────────────────────────────────────
class TestWindowHours:
    def test_window_24_echoed(self, api):
        body = api.get(ENDPOINT, params={"window_hours": 24}).json()
        assert body["window_hours"] == 24

    def test_window_out_of_range_clamped_to_720_not_400(self, api):
        r = api.get(ENDPOINT, params={"window_hours": 99999})
        assert r.status_code == 200, "must silently clamp, not 400"
        assert r.json()["window_hours"] == 720

    def test_window_zero_clamped_to_1(self, api):
        r = api.get(ENDPOINT, params={"window_hours": 0})
        assert r.status_code == 200
        assert r.json()["window_hours"] == 1

    def test_window_negative_clamped_to_1(self, api):
        r = api.get(ENDPOINT, params={"window_hours": -5})
        assert r.status_code == 200
        assert r.json()["window_hours"] == 1


# ── limit parameter behaviour ────────────────────────────────────────────
class TestLimit:
    def test_limit_huge_clamped_to_500(self, api):
        # Endpoint doesn't echo the limit; verify no 4xx and events count <= 500.
        r = api.get(ENDPOINT, params={"limit": 99999})
        assert r.status_code == 200
        assert len(r.json()["events"]) <= 500

    def test_limit_zero_accepted(self, api):
        # Clamps to 1 internally — should still 200.
        r = api.get(ENDPOINT, params={"limit": 0})
        assert r.status_code == 200
        assert len(r.json()["events"]) <= 1


# ── Live-data invariants (bbox, window, sort) ────────────────────────────
class TestLiveDataInvariants:
    def test_all_events_within_bbox(self, api):
        events = api.get(ENDPOINT, params={"window_hours": 720}).json()["events"]
        for e in events:
            assert 30.0 <= e["latitude"] <= 47.0, f"lat out of bbox: {e}"
            assert -6.0 <= e["longitude"] <= 37.0, f"lon out of bbox: {e}"

    def test_all_events_within_window(self, api):
        window_h = 24
        body = api.get(ENDPOINT, params={"window_hours": window_h}).json()
        cutoff = datetime.now(timezone.utc) - timedelta(hours=window_h)
        # Allow 60s clock skew tolerance.
        cutoff -= timedelta(seconds=60)
        for e in body["events"]:
            obs = datetime.fromisoformat(e["observed_at"].replace("Z", "+00:00"))
            # Some rows may be stored as naive (Mongo strips tz on BSON date);
            # treat those as UTC for the comparison.
            if obs.tzinfo is None:
                obs = obs.replace(tzinfo=timezone.utc)
            assert obs >= cutoff, f"event older than window: {e['observed_at']}"

    def test_events_sorted_newest_first(self, api):
        events = api.get(ENDPOINT, params={"window_hours": 720}).json()["events"]
        prev = None
        for e in events:
            obs = datetime.fromisoformat(e["observed_at"].replace("Z", "+00:00"))
            if obs.tzinfo is None:
                obs = obs.replace(tzinfo=timezone.utc)
            if prev is not None:
                assert obs <= prev, "events must be sorted newest-first"
            prev = obs


# ── Dedup tests (require Mongo seeding) ──────────────────────────────────
@pytest.mark.asyncio
class TestDedup:
    async def test_same_provider_revisions_keep_highest(self, mongo, api):
        # Seed 2 revisions of the same EMSC event within the last hour.
        base_time = datetime.now(timezone.utc) - timedelta(minutes=30)
        eid = "TEST_MAP_REV_001"
        await mongo.emsc_events.insert_many([
            _seed_row(eid, "EMSC", base_time, 35.5, 15.5, 4.2, revision=1),
            _seed_row(eid, "EMSC", base_time, 35.5, 15.5, 4.5, revision=3),
            _seed_row(eid, "EMSC", base_time, 35.5, 15.5, 4.3, revision=2),
        ])
        body = api.get(ENDPOINT, params={"window_hours": 1}).json()
        matches = [e for e in body["events"] if e.get("external_id") == eid]
        assert len(matches) == 1, f"expected 1 row after revision dedup, got {len(matches)}"
        assert matches[0]["magnitude"] == 4.5, "should keep the highest-revision row (mag 4.5)"

    async def test_cross_provider_dedup_merges(self, mongo, api):
        # Seed EMSC + USGS at essentially same lat/lon and same minute.
        base_time = (datetime.now(timezone.utc) - timedelta(minutes=15)).replace(
            second=0, microsecond=0,
        )
        emsc_id = "TEST_MAP_XPROV_EMSC_001"
        usgs_id = "TEST_MAP_XPROV_USGS_001"
        # Slightly different lat/lon within 0.05° (bucket rounds to 0.1°).
        await mongo.emsc_events.insert_many([
            _seed_row(emsc_id, "EMSC", base_time, 36.02, 14.02, 5.1, revision=1),
            _seed_row(usgs_id, "USGS", base_time + timedelta(seconds=20),
                      36.04, 14.03, 5.3, revision=1),
        ])
        body = api.get(ENDPOINT, params={"window_hours": 1}).json()
        # Look for a row matching our seed by proximity.
        candidates = [
            e for e in body["events"]
            if 35.9 <= e["latitude"] <= 36.1 and 13.9 <= e["longitude"] <= 14.1
            and e.get("external_id") in (emsc_id, usgs_id)
        ]
        assert len(candidates) == 1, (
            f"cross-provider dedup should merge to 1 row, got {len(candidates)}: {candidates}"
        )
        row = candidates[0]
        providers = row.get("providers") or []
        assert "EMSC" in providers, f"providers missing EMSC: {providers}"
        assert "USGS" in providers, f"providers missing USGS: {providers}"

    async def test_seeded_event_within_bbox(self, mongo, api):
        # Sanity: seed an event well inside the bbox, confirm it appears.
        base_time = datetime.now(timezone.utc) - timedelta(minutes=5)
        eid = "TEST_MAP_INSIDE_001"
        await mongo.emsc_events.insert_one(
            _seed_row(eid, "EMSC", base_time, 35.9, 14.5, 3.8),
        )
        body = api.get(ENDPOINT, params={"window_hours": 1}).json()
        assert any(e.get("external_id") == eid for e in body["events"])

    async def test_seeded_event_outside_bbox_excluded(self, mongo, api):
        # Seed an event OUTSIDE the Mediterranean bbox — must not appear.
        base_time = datetime.now(timezone.utc) - timedelta(minutes=5)
        eid = "TEST_MAP_OUTSIDE_001"
        await mongo.emsc_events.insert_one(
            _seed_row(eid, "EMSC", base_time, 60.0, 100.0, 5.0),
        )
        body = api.get(ENDPOINT, params={"window_hours": 1}).json()
        assert not any(e.get("external_id") == eid for e in body["events"])

    async def test_seeded_event_outside_window_excluded(self, mongo, api):
        # Seed an event 200h ago; querying with window_hours=1 must exclude it.
        base_time = datetime.now(timezone.utc) - timedelta(hours=200)
        eid = "TEST_MAP_STALE_001"
        await mongo.emsc_events.insert_one(
            _seed_row(eid, "EMSC", base_time, 35.5, 15.5, 4.0),
        )
        body = api.get(ENDPOINT, params={"window_hours": 1}).json()
        assert not any(e.get("external_id") == eid for e in body["events"])
