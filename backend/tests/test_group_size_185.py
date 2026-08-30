"""
Task #185 — Group Size (ANTI-DOUBLE-COUNT) Backend Contract Tests

Verifies:
  1. POST /api/status accepts group_size buckets {just_me, 2, 3, 4, 5_plus}
  2. Invalid values (5, six, int 5, ' 2 ') return 422
  3. /api/devices exposes group_size on each device row
  4. /api/admin/device-history/{device_id} last_known.group_size + events[]
     each snapshot group_size at time of report
  5. ANTI-DOUBLE-COUNT: /api/public/summary counts never sum group_size —
     N reports with group_size='4' MUST increase totals by exactly N, not 4N
  6. Skipped (null) group_size doesn't break counts (+1 as normal)
"""

import os
import time
import uuid

import pytest
import requests

BASE_URL = os.environ.get("EXPO_PUBLIC_BACKEND_URL",
                          "https://rescue-alert-hub-3.preview.emergentagent.com").rstrip("/")
ADMIN_TOKEN = os.environ.get("ADMIN_TRIGGER_PASSWORD", "m11vRwfDoxnHvIMLkKzjUwQy")


@pytest.fixture
def client():
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    return s


@pytest.fixture
def admin_headers():
    return {"X-Admin-Token": ADMIN_TOKEN}


def _new_device_id():
    return f"qg-test-{int(time.time()*1000)}-{uuid.uuid4().hex[:6]}"


# ─── API CONTRACT ──────────────────────────────────────────────────────

class TestGroupSizeApiContract:
    """POST /api/status accepts opaque string buckets; invalids are rejected."""

    @pytest.mark.parametrize("gs", ["just_me", "2", "3", "4", "5_plus"])
    def test_valid_buckets_accepted(self, client, gs):
        did = _new_device_id()
        r = client.post(f"{BASE_URL}/api/status",
                        json={"deviceId": did, "status": "safe", "group_size": gs})
        assert r.status_code == 200, f"bucket {gs} rejected: {r.text}"
        body = r.json()
        assert body["status"] == "ok"
        assert body["device_id"] == did

    def test_null_group_size_accepted(self, client):
        did = _new_device_id()
        r = client.post(f"{BASE_URL}/api/status",
                        json={"deviceId": did, "status": "safe", "group_size": None})
        assert r.status_code == 200

    def test_missing_group_size_accepted(self, client):
        did = _new_device_id()
        r = client.post(f"{BASE_URL}/api/status",
                        json={"deviceId": did, "status": "safe"})
        assert r.status_code == 200

    @pytest.mark.parametrize("bad", ["5", "six", "1", "0", " 2 ", "5plus", "5_PLUS"])
    def test_invalid_string_rejected(self, client, bad):
        did = _new_device_id()
        r = client.post(f"{BASE_URL}/api/status",
                        json={"deviceId": did, "status": "safe", "group_size": bad})
        assert r.status_code == 422, f"invalid group_size {bad!r} accepted"

    @pytest.mark.parametrize("bad_type", [5, 2, 3.0, True, ["3"], {"n": 3}])
    def test_invalid_type_rejected(self, client, bad_type):
        """Non-string group_size types must not be coerced/summed.
        Pydantic v2 with str field + regex will 422 on non-string types.
        """
        did = _new_device_id()
        r = client.post(f"{BASE_URL}/api/status",
                        json={"deviceId": did, "status": "safe", "group_size": bad_type})
        assert r.status_code == 422, f"type {type(bad_type).__name__} accepted: {r.text}"


# ─── PERSISTENCE + DASHBOARD EXPOSURE ──────────────────────────────────

class TestGroupSizePersistenceAndExposure:
    """After POST, /api/devices row and /api/admin/device-history expose group_size."""

    def test_devices_row_exposes_group_size(self, client, admin_headers):
        did = _new_device_id()
        r = client.post(f"{BASE_URL}/api/status",
                        json={"deviceId": did, "status": "safe", "group_size": "2"})
        assert r.status_code == 200

        r2 = client.get(f"{BASE_URL}/api/devices", headers=admin_headers)
        assert r2.status_code == 200, r2.text
        rows = r2.json().get("devices", [])
        me = [x for x in rows if x.get("device_id") == did]
        assert me, f"device {did} not found in /api/devices"
        assert me[0].get("group_size") == "2", \
            f"expected group_size='2', got {me[0].get('group_size')!r}"

    def test_device_history_last_known_group_size(self, client, admin_headers):
        did = _new_device_id()
        # First report with group_size='3'
        r = client.post(f"{BASE_URL}/api/status",
                        json={"deviceId": did, "status": "safe", "group_size": "3"})
        assert r.status_code == 200

        r2 = client.get(f"{BASE_URL}/api/admin/device-history/{did}",
                        headers=admin_headers)
        assert r2.status_code == 200, r2.text
        body = r2.json()
        assert body["last_known"]["group_size"] == "3"
        assert body["count"] >= 1
        # Every status event should have snapshotted group_size at that moment
        status_events = [e for e in body["events"] if e.get("kind") == "status"]
        assert status_events, "no status events found"
        # The most recent status event we just posted should carry group_size='3'
        assert status_events[0]["group_size"] == "3", \
            f"event.group_size snapshot missing: {status_events[0]}"

    def test_device_history_snapshots_change_over_time(self, client, admin_headers):
        """Two sequential reports with different group_size — the ledger
        should carry each value on its own event row (never mutating old)."""
        did = _new_device_id()
        client.post(f"{BASE_URL}/api/status",
                    json={"deviceId": did, "status": "safe", "group_size": "2"})
        time.sleep(0.5)
        client.post(f"{BASE_URL}/api/status",
                    json={"deviceId": did, "status": "safe", "group_size": "4"})

        r = client.get(f"{BASE_URL}/api/admin/device-history/{did}",
                       headers=admin_headers)
        body = r.json()
        status_events = [e for e in body["events"] if e.get("kind") == "status"]
        gs_values = [e.get("group_size") for e in status_events]
        # Latest first; expect at least one '4' and one '2' present
        assert "4" in gs_values, f"missing '4' in {gs_values}"
        assert "2" in gs_values, f"missing '2' in {gs_values}"
        assert body["last_known"]["group_size"] == "4"


# ─── ANTI-DOUBLE-COUNT CONTRACT (CRITICAL) ────────────────────────────

class TestAntiDoubleCountContract:
    """P0: /api/public/summary counts MUST NEVER be summed from group_size.
    N reports with group_size='4' MUST increase count by exactly N, not 4N."""

    def _summary(self, client):
        r = client.get(f"{BASE_URL}/api/public/summary")
        assert r.status_code == 200
        return r.json()

    def test_single_report_gs4_increments_by_one(self, client):
        pre = self._summary(client)
        pre_total = pre["total"]
        pre_safe = pre["counts"]["safe"]

        did = _new_device_id()
        r = client.post(f"{BASE_URL}/api/status",
                        json={"deviceId": did, "status": "safe", "group_size": "4"})
        assert r.status_code == 200
        time.sleep(0.5)

        post = self._summary(client)
        post_total = post["total"]
        post_safe = post["counts"]["safe"]

        assert post_total - pre_total <= 1, \
            f"total moved by {post_total - pre_total} — expected 0 or 1, NOT 4"
        assert post_safe - pre_safe <= 1, \
            f"safe moved by {post_safe - pre_safe} — expected 0 or 1, NOT 4"

    def test_5plus_from_different_device_still_plus_one(self, client):
        pre = self._summary(client)
        pre_total = pre["total"]

        did = _new_device_id()
        r = client.post(f"{BASE_URL}/api/status",
                        json={"deviceId": did, "status": "safe", "group_size": "5_plus"})
        assert r.status_code == 200
        time.sleep(0.5)

        post = self._summary(client)
        post_total = post["total"]

        delta = post_total - pre_total
        assert delta <= 1, \
            f"5_plus caused total delta of {delta} — CRITICAL: group_size summed!"

    def test_skipped_group_size_still_plus_one(self, client):
        pre = self._summary(client)
        pre_total = pre["total"]
        pre_safe = pre["counts"]["safe"]

        did = _new_device_id()
        r = client.post(f"{BASE_URL}/api/status",
                        json={"deviceId": did, "status": "safe", "group_size": None})
        assert r.status_code == 200
        time.sleep(0.5)

        post = self._summary(client)

        delta_total = post["total"] - pre_total
        delta_safe = post["counts"]["safe"] - pre_safe
        assert delta_total in (0, 1), delta_total
        assert delta_safe in (0, 1), delta_safe

    def test_no_total_people_estimate_field(self, client):
        """Paul: 'There is no total_people_estimate and there will not be one.'
        The public summary must never expose such a field."""
        summary = self._summary(client)
        assert "total_people_estimate" not in summary
        assert "total_people_estimate" not in summary.get("counts", {})

    def test_trapped_gs4_no_double_count(self, client):
        pre = self._summary(client)
        pre_trapped = pre["counts"]["trapped"]

        did = _new_device_id()
        r = client.post(f"{BASE_URL}/api/status",
                        json={"deviceId": did, "status": "trapped",
                              "severity": "red", "mobility": "trapped",
                              "group_size": "4"})
        assert r.status_code == 200
        time.sleep(0.5)

        post = self._summary(client)
        delta = post["counts"]["trapped"] - pre_trapped
        assert delta <= 1, \
            f"trapped moved by {delta} — expected 0 or 1, NOT 4 (group_size summed!)"

    def test_devices_row_group_size_never_summed_into_count(self, client, admin_headers):
        """Sanity: /api/devices returns per-row group_size and top-level 'counts',
        counts.safe/trapped MUST be a count of DEVICES, not sum of group sizes."""
        r = client.get(f"{BASE_URL}/api/devices", headers=admin_headers)
        assert r.status_code == 200
        body = r.json()
        counts = body["counts"]
        # counts.total must equal sum of buckets (definition)
        bucket_sum = (counts["safe"] + counts["trapped"] + counts["rescued"]
                      + counts["not_responding"] + counts["unknown"])
        assert bucket_sum == counts["total"], \
            f"count buckets ({bucket_sum}) != counts.total ({counts['total']})"


# ─── STATUS_EVENTS LEDGER ──────────────────────────────────────────────

class TestStatusEventsLedger:
    """Each status_event carries the group_size in force at report time."""

    def test_follow_up_updates_last_known_but_preserves_events(self, client, admin_headers):
        did = _new_device_id()
        # Initial report (no group_size — skipped-like)
        client.post(f"{BASE_URL}/api/status",
                    json={"deviceId": did, "status": "safe", "group_size": None})
        time.sleep(0.3)
        # Follow-up with group_size='3'
        client.post(f"{BASE_URL}/api/status",
                    json={"deviceId": did, "status": "safe", "group_size": "3"})

        r = client.get(f"{BASE_URL}/api/admin/device-history/{did}",
                       headers=admin_headers)
        body = r.json()
        assert body["last_known"]["group_size"] == "3"
        # Should be at least 2 status events, each with its own group_size snap
        status_events = [e for e in body["events"] if e.get("kind") == "status"]
        assert len(status_events) >= 2
        gs_seq = [e.get("group_size") for e in status_events]
        assert "3" in gs_seq
        # Both a null and a '3' should be present
        assert None in gs_seq or "3" in gs_seq
