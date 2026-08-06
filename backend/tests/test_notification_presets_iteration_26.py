"""Backend tests for the notification-preset endpoints.

Covers:
  - GET default for an unknown device (returns 200 + 'noticeable')
  - POST for each valid preset value & persistence via GET
  - POST rejects invalid presets with 4xx
"""
import os
import time
import pytest
import requests

BASE_URL = os.environ["EXPO_PUBLIC_BACKEND_URL"].rstrip("/") if os.environ.get("EXPO_PUBLIC_BACKEND_URL") else None
if BASE_URL is None:
    # fall back to frontend .env
    with open("/app/frontend/.env") as f:
        for line in f:
            if line.startswith("EXPO_PUBLIC_BACKEND_URL="):
                BASE_URL = line.split("=", 1)[1].strip().strip('"').rstrip("/")
                break


@pytest.fixture(scope="module")
def api():
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    return s


@pytest.fixture
def fresh_device_id():
    return f"TEST_np_{int(time.time()*1000)}"


class TestNotificationPresetGET:
    def test_unknown_device_returns_default_noticeable(self, api, fresh_device_id):
        r = api.get(f"{BASE_URL}/api/devices/{fresh_device_id}/notification-preset")
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["preset"] == "noticeable"
        assert data["default_used"] is True
        assert data["device_id"] == fresh_device_id


class TestNotificationPresetPOST:
    @pytest.mark.parametrize("preset", ["off", "significant", "noticeable", "everything"])
    def test_valid_presets_persist(self, api, fresh_device_id, preset):
        did = f"{fresh_device_id}_{preset}"
        r = api.post(
            f"{BASE_URL}/api/devices/notification-preset",
            json={"device_id": did, "preset": preset},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is True
        assert body["preset"] == preset

        # Verify persistence
        g = api.get(f"{BASE_URL}/api/devices/{did}/notification-preset")
        assert g.status_code == 200
        d = g.json()
        assert d["preset"] == preset
        assert d["default_used"] is False

    def test_invalid_preset_returns_4xx(self, api, fresh_device_id):
        r = api.post(
            f"{BASE_URL}/api/devices/notification-preset",
            json={"device_id": fresh_device_id, "preset": "garbage"},
        )
        assert 400 <= r.status_code < 500, f"Expected 4xx, got {r.status_code}: {r.text}"

    def test_missing_device_id_returns_4xx(self, api):
        r = api.post(
            f"{BASE_URL}/api/devices/notification-preset",
            json={"preset": "off"},
        )
        assert 400 <= r.status_code < 500

    def test_empty_preset_returns_4xx(self, api, fresh_device_id):
        r = api.post(
            f"{BASE_URL}/api/devices/notification-preset",
            json={"device_id": fresh_device_id, "preset": ""},
        )
        assert 400 <= r.status_code < 500

    def test_case_insensitive_or_normalized(self, api, fresh_device_id):
        # Server does .strip().lower() — verify uppercase works
        r = api.post(
            f"{BASE_URL}/api/devices/notification-preset",
            json={"device_id": fresh_device_id, "preset": "OFF"},
        )
        assert r.status_code == 200
        assert r.json()["preset"] == "off"
