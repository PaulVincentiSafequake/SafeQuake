"""
#280–#287 verification round (Paul's setup/settings/home review v1.0.43).

Backend regressions this file pins:
  - GET /api/devices still returns devices/off_board/counts/count_notes,
    ask_state and counts.no_answer.
  - GET /api/admin/alert/stand-down/preview returns staying_real_count and
    staying_test_count (and does NOT throw with no live alert either).
  - apns.send_preview_alerts now sends apns_expiration ~20 minutes ahead
    (#287) while keeping apns_priority '5'. Structural check on the module.

The pre-existing suites test_delivery_truth_276.py and
test_stand_down_split_274.py are executed as-is by pytest — this file only
adds the checks the review request explicitly names.
"""
import os
import re
import time
import inspect

import pytest
import requests


BASE_URL = (os.environ.get("EXPO_PUBLIC_BACKEND_URL") or "").rstrip("/")
ADMIN_TOKEN = os.environ.get("ADMIN_TRIGGER_PASSWORD") or "m11vRwfDoxnHvIMLkKzjUwQy"


@pytest.fixture(scope="module")
def api():
    if not BASE_URL:
        pytest.skip("EXPO_PUBLIC_BACKEND_URL is not set")
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    return s


# ─────────────────────────────────────────────────────────────
# GET /api/devices — payload shape (review bullet: still returns
# devices/off_board/counts/count_notes with ask_state and counts.no_answer)
# ─────────────────────────────────────────────────────────────
class TestDevicesPayload:
    def test_devices_ok_and_shape(self, api):
        r = api.get(f"{BASE_URL}/api/devices", headers={"X-Admin-Token": ADMIN_TOKEN})
        assert r.status_code == 200, r.text
        body = r.json()
        # top-level keys the dashboard depends on
        for k in ("devices", "off_board", "counts", "count_notes"):
            assert k in body, f"missing top-level key: {k}"
        assert isinstance(body["devices"], list)
        assert isinstance(body["off_board"], list)
        assert isinstance(body["counts"], dict)

    def test_counts_has_no_answer(self, api):
        r = api.get(f"{BASE_URL}/api/devices", headers={"X-Admin-Token": ADMIN_TOKEN})
        counts = r.json()["counts"]
        assert "no_answer" in counts, f"counts missing no_answer key: {list(counts.keys())}"
        assert isinstance(counts["no_answer"], int)

    def test_devices_have_ask_state(self, api):
        r = api.get(f"{BASE_URL}/api/devices", headers={"X-Admin-Token": ADMIN_TOKEN})
        # ask_state lives on the ON-board rows (working board), which is
        # where the operator card reads it — off_board rows are already
        # removed and no ask is possible.
        working = r.json()["devices"]
        if not working:
            pytest.skip("no working-board devices seeded; ask_state cannot be verified")
        for row in working:
            assert "ask_state" in row, f"device {row.get('device_id')} missing ask_state"


# ─────────────────────────────────────────────────────────────
# GET /api/admin/alert/stand-down/preview — split counts (#274)
# ─────────────────────────────────────────────────────────────
class TestStandDownPreview:
    def test_preview_returns_split_counts(self, api):
        r = api.get(
            f"{BASE_URL}/api/admin/alert/stand-down/preview",
            headers={"X-Admin-Token": ADMIN_TOKEN},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert "staying_real_count" in body
        assert "staying_test_count" in body
        assert isinstance(body["staying_real_count"], int)
        assert isinstance(body["staying_test_count"], int)


# ─────────────────────────────────────────────────────────────
# apns.send_preview_alerts — expiration now ~20 minutes (#287)
# ─────────────────────────────────────────────────────────────
class TestApnsPreviewExpiration:
    def _read_source(self) -> str:
        import backend.apns as apns  # type: ignore
        return inspect.getsource(apns.send_preview_alerts)

    def test_send_preview_alerts_uses_priority_5(self):
        try:
            src = self._read_source()
        except (ImportError, ModuleNotFoundError):
            # Fallback: read the file directly
            with open("/app/backend/apns.py", "r") as f:
                whole = f.read()
            m = re.search(r"async def send_preview_alerts.*?(?=\nasync def |\Z)", whole, re.S)
            assert m, "could not locate send_preview_alerts in apns.py"
            src = m.group(0)
        assert 'apns_priority="5"' in src, "send_preview_alerts must keep apns_priority='5'"

    def test_send_preview_alerts_expiration_is_about_20_minutes(self):
        try:
            src = self._read_source()
        except (ImportError, ModuleNotFoundError):
            with open("/app/backend/apns.py", "r") as f:
                whole = f.read()
            m = re.search(r"async def send_preview_alerts.*?(?=\nasync def |\Z)", whole, re.S)
            assert m
            src = m.group(0)
        # Must NOT be the old '0' literal (which meant one-shot no-store).
        assert 'apns_expiration="0"' not in src, "expiration=0 was the #287 bug — must be gone"
        # 20 minutes = 1200 seconds
        assert "1200" in src, (
            "send_preview_alerts expected to compute expiration = now + 1200 seconds (20 min)"
        )
        # And should be computed off time.time(), not a fixed literal.
        assert "time.time()" in src


# ─────────────────────────────────────────────────────────────
# Smoke — the backend is up on the public preview URL.
# ─────────────────────────────────────────────────────────────
def test_devices_endpoint_is_reachable(api):
    # This app doesn't expose /api/health — the operator dashboard's
    # liveness signal is that /api/devices returns 200.
    r = api.get(
        f"{BASE_URL}/api/devices",
        headers={"X-Admin-Token": ADMIN_TOKEN},
        timeout=15,
    )
    assert r.status_code == 200
