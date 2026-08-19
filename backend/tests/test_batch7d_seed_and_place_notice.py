"""Batch 7 D — backend tests for iteration 36.

Covers:
- #241 backend seed jitter: POST /api/admin/test-people/seed spreads
  people sharing an address so no two rows share exact (lat, lon).
- #246 place-notice reason wording: emsc/preview.py body for
  dispatch_place_notices contains the "You get this because you added
  {name} to your saved places." + "Turn off in Settings..." sentence.
"""
from __future__ import annotations

import os
import re
import inspect

import pytest
import requests


BASE_URL = os.environ.get("EXPO_PUBLIC_BACKEND_URL", "").rstrip("/") or \
           os.environ.get("EXPO_BACKEND_URL", "").rstrip("/")
ADMIN_TOKEN = os.environ.get("ADMIN_TRIGGER_PASSWORD") or "m11vRwfDoxnHvIMLkKzjUwQy"


@pytest.fixture
def api_client():
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    return s


# ── #241 seed-jitter ────────────────────────────────────────────────────
class TestSeedJitter:
    """POST /api/admin/test-people/seed must return 33 with distinct pins."""

    def test_seed_returns_33(self, api_client):
        assert BASE_URL, "EXPO_PUBLIC_BACKEND_URL not set"
        r = api_client.post(
            f"{BASE_URL}/api/admin/test-people/seed",
            headers={"X-Admin-Token": ADMIN_TOKEN},
        )
        assert r.status_code == 200, f"seed failed: {r.status_code} {r.text[:400]}"
        data = r.json()
        assert data.get("seeded") == 33, f"expected 33, got {data.get('seeded')}"
        assert data.get("seed_tag") == "seeded-33"

    def test_all_pins_have_distinct_coords(self, api_client):
        """After seeding, list device status and assert no two seeded rows
        share exact (lat, lon). This is the actual fix for #241 — the
        18m jitter ring makes every dot visible."""
        # Ensure fresh seed
        api_client.post(
            f"{BASE_URL}/api/admin/test-people/seed",
            headers={"X-Admin-Token": ADMIN_TOKEN},
        )
        r = api_client.get(
            f"{BASE_URL}/api/devices",
            headers={"X-Admin-Token": ADMIN_TOKEN},
            params={"limit": 5000},
        )
        assert r.status_code == 200, r.text[:400]
        payload = r.json()
        rows = payload if isinstance(payload, list) else (
            payload.get("devices") or payload.get("statuses") or payload.get("items") or []
        )
        seeded = [row for row in rows
                  if (row.get("_test_seed") == "seeded-33"
                      or (row.get("device_id") or "").startswith("qg-seeded-33-"))]
        assert len(seeded) == 33, f"expected 33 seeded rows in /api/status, got {len(seeded)}"

        coords = [(row.get("latitude"), row.get("longitude")) for row in seeded]
        # Filter out None
        coords = [c for c in coords if c[0] is not None and c[1] is not None]
        assert len(coords) == 33, f"seeded rows missing lat/lon: only {len(coords)} had coords"
        unique = set(coords)
        assert len(unique) == 33, (
            f"expected 33 distinct (lat, lon) pairs after jitter, "
            f"got {len(unique)} unique out of {len(coords)}. Duplicates present — "
            f"jitter fix (#241) is not working."
        )

    def test_cleanup_after(self, api_client):
        """Clear the seeded rows so we leave a clean DB."""
        r = api_client.post(
            f"{BASE_URL}/api/admin/test-people/clear",
            headers={"X-Admin-Token": ADMIN_TOKEN},
        )
        assert r.status_code == 200
        data = r.json()
        assert data.get("removed", 0) >= 33


# ── #246 place-notice reason wording (source inspection) ───────────────
class TestPlaceNoticeReasonWording:
    """dispatch_place_notices must include the 'You get this because…'
    and 'Turn off in Settings › Places…' sentences in the notification body."""

    def test_body_contains_reason_sentences(self):
        import sys
        sys.path.insert(0, "/app/backend")
        from emsc import preview

        src = inspect.getsource(preview.dispatch_place_notices)
        assert "You get this because you added" in src, (
            "#246 wording missing: 'You get this because you added {name}"
            " to your saved places.'"
        )
        assert "Turn off in Settings" in src, (
            "#246 wording missing: 'Turn off in Settings › Places if you no"
            " longer want notices for it.'"
        )
        # Verify the sentence structure references saved places + settings
        assert re.search(r"saved places", src), "expected 'saved places' in body"
        assert re.search(r"Settings\s*›\s*Places", src), "expected 'Settings › Places' path"
