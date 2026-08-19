"""#247 — Saved place must not point at the wrong city.

Batch 7, Part C, Item C1. App version 1.0.33 / build 33.

Root cause (frontend): the places-add screen kept `resolved` (the
coordinates from geocodeAsync) alive across edits to the search field.
A user could type "Catania", tap Find (resolved=Catania coords), then
change the search to "Athens" and — without tapping Find again —
overwrite the name to "Athens" and hit Save. The saved place would
have the label "Athens" but Catania's coordinates. Notification paths
read those coords, so people got tremor notices for the wrong region.

Two guarantees are asserted here:

1. Frontend source (`places.tsx`) invalidates `resolved` when the
   search text changes so Save cannot be reached with stale coords.
   Also, the resolved coordinates are shown reverse-geocoded as a
   plain address so a mismatch is visible before Save.

2. Backend `/api/devices/{id}/places` stores the exact coordinates it
   receives — no rounding, no reordering — and returns them intact
   on GET. This is what the notification path reads.

Cleanup: seed devices use the prefix ``qg-test-c1-247-`` and are
purged at teardown so the shared DB stays clean.
"""
from __future__ import annotations

import os
import re
import uuid
from pathlib import Path

import pytest
import requests


def _load_backend_url() -> str:
    fe = Path("/app/frontend/.env").read_text()
    m = re.search(r"^EXPO_PUBLIC_BACKEND_URL=(.+)$", fe, re.M)
    assert m, "EXPO_PUBLIC_BACKEND_URL not found in frontend/.env"
    return m.group(1).strip().rstrip("/")


BASE_URL = _load_backend_url()
PLACES_SRC = Path("/app/frontend/app/settings/places.tsx").read_text()


# ── Frontend source guarantees (#247) ──────────────────────────────

class TestPlacesScreenSourceGuards:
    """The bug lived in the client, so the fix is asserted in source."""

    def test_resolved_state_tracks_the_query_used(self):
        # The resolved object must carry the exact search text that
        # produced it, so onChangeText can detect a mismatch.
        assert "searchedAs" in PLACES_SRC, (
            "resolved state must remember which search produced it (#247)"
        )

    def test_search_edit_invalidates_stale_resolved(self):
        # The onChangeText handler must clear resolved when the user
        # edits the search text away from the value that was Find-ed.
        # We look for the specific invalidation call in the search
        # input's onChangeText.
        assert "resolved.searchedAs" in PLACES_SRC, (
            "search onChangeText must compare against the Find-ed query (#247)"
        )
        assert re.search(
            r"onChangeText=\{[^}]*setResolved\(null\)",
            PLACES_SRC,
            re.S,
        ), "search onChangeText must clear resolved on mismatch (#247)"

    def test_reverse_geocode_shows_actual_address(self):
        # After Find, the app must show what the OS geocoder actually
        # picked — in words, not just lat/lng — so a wrong hit is
        # obvious before Save.
        assert "reverseGeocodeAsync" in PLACES_SRC, (
            "resolved coords must be reverse-geocoded so mismatch is visible (#247)"
        )
        assert "place-resolved-card" in PLACES_SRC, (
            "resolved details must be shown in a dedicated card (#247)"
        )

    def test_no_bare_reset_of_resolved_on_every_keystroke(self):
        # Guard against a lazy fix that clears resolved on ANY keystroke
        # — that would erase the "Found this place" card while the user
        # is just typing a longer name. The invalidation must be
        # conditional on the search text differing from `searchedAs`.
        # We look for the guarded pattern explicitly.
        assert "t.trim() !== resolved.searchedAs" in PLACES_SRC, (
            "invalidation must be conditional on a real change (#247)"
        )


# ── Backend coordinate-integrity guarantees ────────────────────────

@pytest.fixture
def seed_device_id():
    did = f"qg-test-c1-247-{uuid.uuid4().hex[:8]}"
    yield did
    # Cleanup: remove any places we added
    try:
        r = requests.get(
            f"{BASE_URL}/api/devices/{did}/places", timeout=15,
        )
        if r.ok:
            for p in r.json().get("places", []):
                requests.delete(
                    f"{BASE_URL}/api/devices/{did}/places/{p['place_id']}",
                    timeout=15,
                )
    except requests.RequestException:
        pass


class TestBackendStoresExactCoords:
    """Whatever the client sends, the backend stores byte-for-byte."""

    def test_saved_place_returns_exact_coordinates(self, seed_device_id):
        # Athens, Greece — the exact scenario from the bug report.
        payload = {
            "name": "Athens",
            "latitude": 37.9838,
            "longitude": 23.7275,
        }
        r = requests.post(
            f"{BASE_URL}/api/devices/{seed_device_id}/places",
            json=payload, timeout=15,
        )
        assert r.status_code == 200, r.text
        listed = requests.get(
            f"{BASE_URL}/api/devices/{seed_device_id}/places", timeout=15,
        ).json()["places"]
        assert len(listed) == 1
        saved = listed[0]
        assert saved["name"] == "Athens"
        # No drift, no rounding — the exact same floats.
        assert saved["latitude"] == pytest.approx(37.9838, abs=1e-9)
        assert saved["longitude"] == pytest.approx(23.7275, abs=1e-9)

    def test_two_places_do_not_get_swapped(self, seed_device_id):
        # Regression: if the backend ever cross-wired lat/lng or
        # name→coords across two entries, we'd catch it here.
        catania = {"name": "Catania", "latitude": 37.5079, "longitude": 15.0830}
        athens = {"name": "Athens",  "latitude": 37.9838, "longitude": 23.7275}
        for p in (catania, athens):
            r = requests.post(
                f"{BASE_URL}/api/devices/{seed_device_id}/places",
                json=p, timeout=15,
            )
            assert r.status_code == 200, r.text
        by_name = {
            p["name"]: p
            for p in requests.get(
                f"{BASE_URL}/api/devices/{seed_device_id}/places", timeout=15,
            ).json()["places"]
        }
        assert by_name["Catania"]["latitude"] == pytest.approx(37.5079)
        assert by_name["Catania"]["longitude"] == pytest.approx(15.0830)
        assert by_name["Athens"]["latitude"] == pytest.approx(37.9838)
        assert by_name["Athens"]["longitude"] == pytest.approx(23.7275)


# ── Version guard (Batch 7 rule: every change bumps the version) ───

class TestVersionBumpedForThisFix:
    def test_app_json_at_1_0_33(self):
        aj = Path("/app/frontend/app.json").read_text()
        assert '"version": "1.0.33"' in aj, "version must be bumped for #247"
        assert '"buildNumber": "33"' in aj, "iOS build number must match"
        assert '"versionCode": 33' in aj, "Android versionCode must match"
