"""Tests for the operator-configurable preview radius override.

Locks in three invariants:

  1. When override is set and non-expired, previews fire for events
     BEYOND the country's real poll_radius_km (600 km) but within the
     override — this is the whole point of the feature.

  2. When override has expired (>= override_expires_at), previews revert
     to the real 600 km boundary automatically. No manual clear needed.
     This is the safety guarantee that the feature can never be "left
     on forever by accident when real users arrive."

  3. When an event fires only because of the override (i.e., it's beyond
     600 km but within the override), the notification body is prefixed
     with the "Beyond alert zone" warning so it can never be visually
     confused with a real-boundary alert.

Uses an in-memory mongo-like double (`FakeDb`) so we don't depend on a
running Mongo, and a stub `apns_send_preview` that records what was sent
without hitting APNs.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List


# ── Fake DB (just enough for dispatch_preview_if_needed) ──────────────
class _FakeCollection:
    def __init__(self, rows: List[dict] | None = None):
        self.rows: List[dict] = list(rows or [])
        self.inserted: List[dict] = []

    def find(self, query=None, projection=None):
        return _FakeCursor([r for r in self.rows if _matches(r, query or {})])

    async def find_one(self, query=None, projection=None):
        for r in self.rows:
            if _matches(r, query or {}):
                return dict(r)
        return None

    async def insert_one(self, doc):
        self.inserted.append(dict(doc))
        return None


class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    async def to_list(self, limit):
        return list(self._rows[:limit])


def _matches(row: dict, query: dict) -> bool:
    for k, v in query.items():
        val = row.get(k)
        if isinstance(v, dict):
            if "$in" in v:
                if val not in v["$in"]:
                    return False
            elif "$gte" in v:
                if val is None or val < v["$gte"]:
                    return False
            elif "$exists" in v or "$ne" in v:
                exists = k in row and row[k] is not None
                if v.get("$exists") is True and not exists:
                    return False
                if "$ne" in v and val == v["$ne"]:
                    return False
        else:
            if val != v:
                return False
    return True


class FakeDb:
    def __init__(self, push_devices):
        self.push_devices = _FakeCollection(push_devices)
        self.emsc_preview_notifications = _FakeCollection()


# ── Fixtures ─────────────────────────────────────────────────────────
MALTA_CENTER = {"lat": 35.9375, "lon": 14.3754}
MALTA_CONFIG_BASE = {
    "country_code": "MT",
    "country_name": "Malta",
    "center": MALTA_CENTER,
    "poll_radius_km": 600,
    "preview_mode": {
        "enabled": True,
        "device_ids": ["test-phone-1"],
        "trigger_tier": "all_ingested",
        "rate_limit_minutes": 10,
    },
}
PUSH_DEVICES = [
    {"user_id": "test-phone-1", "platform": "ios",
     "device_token": "aaaa"*16, "notification_preset": "everything"},
]

# Athens ≈ 830 km NE of Malta — beyond the real 600 km boundary,
# well inside a 2000 km override.
ATHENS_EVENT = {
    "provider": "EMSC", "external_id": "test-athens-1", "revision": 0,
    "latitude": 37.9838, "longitude": 23.7275,
    "magnitude": 4.2, "depth_km": 15,
    "observed_at": datetime.now(timezone.utc),
    "evaluations": [],  # all_ingested tier doesn't need per-country eval
    "intensity_estimates": {"at_MT_center": {"mmi": 2.5}},
}

# Sicily ≈ 200 km N of Malta — INSIDE the real 600 km boundary. Must
# fire regardless of override state.
SICILY_EVENT = dict(
    ATHENS_EVENT,
    external_id="test-sicily-1",
    latitude=37.5, longitude=15.1,
)

# Auckland ≈ 18,000 km — beyond even the 5000 km safety cap. Must
# NEVER fire, even with the maximum override.
AUCKLAND_EVENT = dict(
    ATHENS_EVENT,
    external_id="test-auckland-1",
    latitude=-36.85, longitude=174.76,
)


async def _stub_apns_send_preview(**kwargs):
    """Records what would have been sent without touching APNs.
    Marks every send as delivered=True for simplicity."""
    devices = kwargs["devices"]
    return {
        "payload": {"title": kwargs["title"], "body": kwargs["body"]},
        "events": [
            {"user_id": d.get("user_id"), "delivered": True,
             "apns_id": "test-apns-id"} for d in devices
        ],
    }


# ── Tests ────────────────────────────────────────────────────────────
def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def test_no_override_athens_is_skipped():
    """Without the override, Athens (830 km) is beyond the 600 km
    boundary and MUST be skipped. This is the current production
    behaviour we're not breaking."""
    from emsc.preview import dispatch_preview_if_needed
    db = FakeDb(PUSH_DEVICES)
    cfg = dict(MALTA_CONFIG_BASE)
    result = _run(dispatch_preview_if_needed(
        db=db, apns_send_preview=_stub_apns_send_preview,
        emsc_event=ATHENS_EVENT, country_config=cfg,
    ))
    assert result is None, f"Expected skip, got dispatch: {result}"
    skips = [r for r in db.emsc_preview_notifications.inserted
             if not r.get("delivered")]
    assert any("beyond_country_radius" in (r.get("skipped_reason") or "")
               for r in skips), (
        f"Expected 'beyond_country_radius' skip, got: {skips}"
    )


def test_override_active_athens_fires_with_warning():
    """With override=2000 km (non-expired), Athens fires — AND the body
    is prefixed with the beyond-alert-zone warning."""
    from emsc.preview import dispatch_preview_if_needed
    now = datetime.now(timezone.utc)
    cfg = dict(MALTA_CONFIG_BASE)
    cfg["preview_mode"] = dict(cfg["preview_mode"], **{
        "preview_radius_km_override": 2000.0,
        "preview_radius_km_override_expires_at": now + timedelta(days=6),
    })
    db = FakeDb(PUSH_DEVICES)
    result = _run(dispatch_preview_if_needed(
        db=db, apns_send_preview=_stub_apns_send_preview,
        emsc_event=ATHENS_EVENT, country_config=cfg,
    ))
    assert result and result.get("attempted") == 1, (
        f"Expected 1 dispatch, got: {result}"
    )
    assert result["body"].startswith("⚠️ Beyond alert zone —"), (
        f"Expected 'Beyond alert zone' prefix, got: {result['body']!r}"
    )
    # And the audit row records that the override was active.
    sent = [r for r in db.emsc_preview_notifications.inserted
            if r.get("delivered")]
    assert sent and sent[0].get("radius_override_active") is True
    assert sent[0].get("effective_radius_km") == 2000.0


def test_override_expired_falls_back_to_600km():
    """Expiry timestamp in the past → treated as if override is unset,
    and the audit skip reason is annotated so operators can see WHY
    their wide-radius previews stopped."""
    from emsc.preview import dispatch_preview_if_needed
    now = datetime.now(timezone.utc)
    cfg = dict(MALTA_CONFIG_BASE)
    cfg["preview_mode"] = dict(cfg["preview_mode"], **{
        "preview_radius_km_override": 2000.0,
        # Already expired an hour ago.
        "preview_radius_km_override_expires_at": now - timedelta(hours=1),
    })
    db = FakeDb(PUSH_DEVICES)
    result = _run(dispatch_preview_if_needed(
        db=db, apns_send_preview=_stub_apns_send_preview,
        emsc_event=ATHENS_EVENT, country_config=cfg,
    ))
    assert result is None, (
        f"Expected skip when override expired, got dispatch: {result}"
    )
    skips = [r for r in db.emsc_preview_notifications.inserted
             if not r.get("delivered")]
    assert any("override_expired_at" in (r.get("skipped_reason") or "")
               for r in skips), (
        f"Expected 'override_expired_at' annotation, got: {skips}"
    )


def test_override_active_sicily_no_warning():
    """Sicily is inside the real 600 km boundary — override doesn't
    change anything for events inside the real zone, and the body must
    NOT have the beyond-alert-zone prefix (that would be a lie)."""
    from emsc.preview import dispatch_preview_if_needed
    now = datetime.now(timezone.utc)
    cfg = dict(MALTA_CONFIG_BASE)
    cfg["preview_mode"] = dict(cfg["preview_mode"], **{
        "preview_radius_km_override": 2000.0,
        "preview_radius_km_override_expires_at": now + timedelta(days=6),
    })
    db = FakeDb(PUSH_DEVICES)
    result = _run(dispatch_preview_if_needed(
        db=db, apns_send_preview=_stub_apns_send_preview,
        emsc_event=SICILY_EVENT, country_config=cfg,
    ))
    assert result and result.get("attempted") == 1
    assert not result["body"].startswith("⚠️ Beyond alert zone —"), (
        f"Sicily is INSIDE 600 km — no warning prefix expected. "
        f"Got: {result['body']!r}"
    )


def test_override_capped_at_5000km_by_config_shape():
    """Even if a malformed config sneaks a huge override past the API
    validator (e.g., a direct db edit), a New Zealand event (~18,000 km)
    must never fire against a 5000 km override. The `should_send_preview`
    hard radius gate does this — the test just documents the invariant."""
    from emsc.preview import dispatch_preview_if_needed
    now = datetime.now(timezone.utc)
    cfg = dict(MALTA_CONFIG_BASE)
    cfg["preview_mode"] = dict(cfg["preview_mode"], **{
        "preview_radius_km_override": 5000.0,
        "preview_radius_km_override_expires_at": now + timedelta(days=6),
    })
    db = FakeDb(PUSH_DEVICES)
    result = _run(dispatch_preview_if_needed(
        db=db, apns_send_preview=_stub_apns_send_preview,
        emsc_event=AUCKLAND_EVENT, country_config=cfg,
    ))
    assert result is None, (
        f"Auckland must never fire even at max override. Got: {result}"
    )
