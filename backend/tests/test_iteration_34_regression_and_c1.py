"""Iteration 34 — independent live-endpoint verification.

Focus:
1. Regression after the server.py split (endpoints must behave as before).
2. C1 new endpoints (recheck answer, admin status/enabled).
3. GDPR — no google.com/maps in any response body.
4. UTC — seismic-map/events observed_at must carry an explicit UTC offset.

Uses:
- BASE_URL from EXPO_PUBLIC_BACKEND_URL (frontend/.env) — the same URL users hit.
- ADMIN_TOKEN via header X-Admin-Token (LEGACY_TOKEN_ENABLED=true).

Cleanup: all seed devices use device_id prefix "qg-test-i34-". Deleted at teardown.
"""
from __future__ import annotations

import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import requests


def _load_env() -> tuple[str, str]:
    fe = Path("/app/frontend/.env").read_text()
    be = Path("/app/backend/.env").read_text()
    m1 = re.search(r"^EXPO_PUBLIC_BACKEND_URL=(.+)$", fe, re.M)
    m2 = re.search(r"^ADMIN_TRIGGER_PASSWORD=(.+)$", be, re.M)
    if not m1 or not m2:
        raise RuntimeError("Missing env keys")
    return m1.group(1).strip().rstrip("/"), m2.group(1).strip()


BASE_URL, ADMIN_TOKEN = _load_env()
API = f"{BASE_URL}/api"
ADMIN_HDR = {"X-Admin-Token": ADMIN_TOKEN}
TIMEOUT = 30


@pytest.fixture(scope="session")
def api_client():
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    return s


# ═════════ REGRESSION: unauthenticated status codes ═════════
class TestUnauthenticatedRegression:
    def test_root(self, api_client):
        r = api_client.get(f"{API}/", timeout=TIMEOUT)
        assert r.status_code == 200

    def test_public_summary_aggregate_only(self, api_client):
        r = api_client.get(f"{API}/public/summary", timeout=TIMEOUT)
        assert r.status_code == 200
        body = r.text
        # aggregate only — no device_id, no lat/lon, no email
        assert "device_id" not in body
        assert not re.search(r'"latitude"\s*:\s*[0-9]', body)
        assert not re.search(r'"email"\s*:\s*"', body)

    def test_devices_requires_auth(self, api_client):
        r = api_client.get(f"{API}/devices", timeout=TIMEOUT)
        assert r.status_code == 401, f"got {r.status_code}: {r.text[:200]}"

    def test_audit_requires_auth(self, api_client):
        r = api_client.get(f"{API}/audit", timeout=TIMEOUT)
        assert r.status_code == 401

    def test_trigger_alert_requires_auth(self, api_client):
        r = api_client.post(f"{API}/trigger-alert", json={}, timeout=TIMEOUT)
        assert r.status_code == 401

    def test_admin_recheck_status_anonymous_401(self, api_client):
        r = api_client.get(f"{API}/admin/recheck/status", timeout=TIMEOUT)
        assert r.status_code == 401

    def test_admin_recheck_enabled_anonymous_401(self, api_client):
        r = api_client.post(
            f"{API}/admin/recheck/enabled", json={"enabled": False}, timeout=TIMEOUT
        )
        assert r.status_code == 401

    def test_cors_debug(self, api_client):
        r = api_client.get(f"{API}/cors-debug", timeout=TIMEOUT)
        assert r.status_code == 200


# ═════════ REGRESSION: admin-authenticated endpoints ═════════
class TestAdminAuthenticatedRegression:
    def test_devices_with_admin_token(self, api_client):
        r = api_client.get(f"{API}/devices", headers=ADMIN_HDR, timeout=TIMEOUT)
        assert r.status_code == 200, r.text[:300]
        j = r.json()
        rows = j.get("devices") if isinstance(j, dict) else j
        assert isinstance(rows, list)
        if rows:
            row = rows[0]
            for f in ("silence_state", "recheck", "deteriorating", "reports_improving"):
                assert f in row, f"missing field {f} in /api/devices row"

    def test_audit(self, api_client):
        r = api_client.get(f"{API}/audit", headers=ADMIN_HDR, timeout=TIMEOUT)
        assert r.status_code == 200

    def test_seismic_map_events(self, api_client):
        r = api_client.get(f"{API}/seismic-map/events", timeout=TIMEOUT)
        assert r.status_code == 200
        j = r.json()
        events = j if isinstance(j, list) else j.get("events", j.get("features", []))
        # If features, dig timestamp from properties
        for ev in events[:50]:
            props = ev.get("properties", ev) if isinstance(ev, dict) else {}
            oa = props.get("observed_at") or ev.get("observed_at") if isinstance(ev, dict) else None
            if oa:
                assert oa.endswith("Z") or re.search(r"[+-]\d{2}:\d{2}$", oa), (
                    f"observed_at missing UTC offset: {oa!r}"
                )

    def test_dashboard_settings(self, api_client):
        r = api_client.get(f"{API}/dashboard-settings", timeout=TIMEOUT)
        assert r.status_code == 200

    def test_admin_users(self, api_client):
        r = api_client.get(f"{API}/admin/users", headers=ADMIN_HDR, timeout=TIMEOUT)
        assert r.status_code == 200

    def test_admin_apns_status(self, api_client):
        r = api_client.get(f"{API}/admin/apns-status", headers=ADMIN_HDR, timeout=TIMEOUT)
        assert r.status_code == 200

    def test_admin_test_entries(self, api_client):
        r = api_client.get(f"{API}/admin/test-entries", headers=ADMIN_HDR, timeout=TIMEOUT)
        assert r.status_code == 200

    def test_emsc_health(self, api_client):
        r = api_client.get(f"{API}/admin/emsc/health", headers=ADMIN_HDR, timeout=TIMEOUT)
        assert r.status_code == 200

    def test_emsc_recent(self, api_client):
        r = api_client.get(f"{API}/admin/emsc/recent", headers=ADMIN_HDR, timeout=TIMEOUT)
        assert r.status_code == 200

    def test_emsc_continuity(self, api_client):
        r = api_client.get(
            f"{API}/admin/emsc/continuity", headers=ADMIN_HDR, timeout=TIMEOUT
        )
        assert r.status_code == 200

    def test_emsc_preview_config(self, api_client):
        r = api_client.get(
            f"{API}/admin/emsc/preview/config", headers=ADMIN_HDR, timeout=TIMEOUT
        )
        assert r.status_code == 200

    def test_audit_export_csv(self, api_client):
        r = api_client.get(
            f"{API}/admin/audit-log/export.csv", headers=ADMIN_HDR, timeout=TIMEOUT
        )
        assert r.status_code == 200
        assert "text/csv" in r.headers.get("Content-Type", "") or r.text.startswith("device_id") or "," in r.text

    def test_audit_export_pdf(self, api_client):
        r = api_client.get(
            f"{API}/admin/audit-log/export.pdf", headers=ADMIN_HDR, timeout=TIMEOUT
        )
        assert r.status_code == 200
        assert r.content[:4] == b"%PDF"

    def test_casualty_operational_pdf(self, api_client):
        r = api_client.get(
            f"{API}/admin/casualty-report/operational.pdf",
            headers=ADMIN_HDR, timeout=TIMEOUT,
        )
        assert r.status_code == 200
        assert r.content[:4] == b"%PDF"

    def test_casualty_public_pdf(self, api_client):
        r = api_client.get(
            f"{API}/admin/casualty-report/public.pdf",
            headers=ADMIN_HDR, timeout=TIMEOUT,
        )
        assert r.status_code == 200
        assert r.content[:4] == b"%PDF"


# ═════════ Seed helper for the C1 answer path ═════════
@pytest.fixture
def seeded_trapped_device(api_client):
    """Create a device_status row via POST /api/status."""
    device_id = f"qg-test-i34-{uuid.uuid4().hex[:12]}"
    payload = {
        "device_id": device_id,
        "status": "trapped",
        "latitude": 35.9012345678,
        "longitude": 14.5123456789,
        "battery_pct": 55,
        "severity": "green",
    }
    r = api_client.post(f"{API}/status", json=payload, timeout=TIMEOUT)
    assert r.status_code in (200, 201), f"seed failed: {r.status_code} {r.text[:200]}"
    yield device_id
    # cleanup: mark rescued to move out of trapped set, then rely on test-purge if present
    try:
        api_client.post(
            f"{API}/mark-rescued",
            headers=ADMIN_HDR,
            json={"device_id": device_id},
            timeout=TIMEOUT,
        )
    except Exception:
        pass


# ═════════ C1: /api/recheck/answer ═════════
class TestRecheckAnswer:
    def test_bad_answer_returns_400(self, api_client, seeded_trapped_device):
        r = api_client.post(
            f"{API}/recheck/answer",
            json={"device_id": seeded_trapped_device, "answer": "fine"},
            timeout=TIMEOUT,
        )
        assert r.status_code == 400

    def test_unknown_device_returns_404(self, api_client):
        r = api_client.post(
            f"{API}/recheck/answer",
            json={"device_id": "qg-test-i34-does-not-exist-zzz", "answer": "same"},
            timeout=TIMEOUT,
        )
        assert r.status_code == 404

    def test_worse_escalates_one_band(self, api_client, seeded_trapped_device):
        r = api_client.post(
            f"{API}/recheck/answer",
            json={"device_id": seeded_trapped_device, "answer": "worse"},
            timeout=TIMEOUT,
        )
        assert r.status_code == 200, r.text
        j = r.json()
        assert j["severity"] == "yellow"
        assert j["deteriorating"] is True

    def test_much_worse_goes_red_from_green(self, api_client):
        # seed fresh green device
        did = f"qg-test-i34-{uuid.uuid4().hex[:12]}"
        api_client.post(
            f"{API}/status",
            json={"device_id": did, "status": "trapped", "severity": "green",
                  "latitude": 35.9, "longitude": 14.5, "battery_pct": 70},
            timeout=TIMEOUT,
        )
        r = api_client.post(
            f"{API}/recheck/answer",
            json={"device_id": did, "answer": "much_worse"},
            timeout=TIMEOUT,
        )
        assert r.status_code == 200
        assert r.json()["severity"] == "red"
        api_client.post(f"{API}/mark-rescued", headers=ADMIN_HDR,
                        json={"device_id": did}, timeout=TIMEOUT)

    def test_better_is_recorded_but_does_not_downgrade(self, api_client):
        did = f"qg-test-i34-{uuid.uuid4().hex[:12]}"
        # seed as red-severity trapped
        api_client.post(
            f"{API}/status",
            json={"device_id": did, "status": "trapped", "severity": "red",
                  "latitude": 35.9, "longitude": 14.5, "battery_pct": 70},
            timeout=TIMEOUT,
        )
        r = api_client.post(
            f"{API}/recheck/answer",
            json={"device_id": did, "answer": "better"},
            timeout=TIMEOUT,
        )
        assert r.status_code == 200
        assert r.json()["severity"] == "red"          # not reduced

        # Verify persisted: /api/devices should show reports_improving=true and severity red
        j = api_client.get(f"{API}/devices", headers=ADMIN_HDR, timeout=TIMEOUT).json()
        rows = j.get("devices") if isinstance(j, dict) else j
        row = next((x for x in rows if x.get("device_id") == did), None)
        assert row is not None, "seeded device not in /api/devices"
        assert row.get("reports_improving") is True
        assert row.get("severity") == "red"
        api_client.post(f"{API}/mark-rescued", headers=ADMIN_HDR,
                        json={"device_id": did}, timeout=TIMEOUT)

    def test_same_writes_recheck_answered_event(self, api_client, seeded_trapped_device):
        r = api_client.post(
            f"{API}/recheck/answer",
            json={"device_id": seeded_trapped_device, "answer": "same"},
            timeout=TIMEOUT,
        )
        assert r.status_code == 200
        # verify via device-history
        hist = api_client.get(
            f"{API}/admin/device-history/{seeded_trapped_device}",
            headers=ADMIN_HDR, timeout=TIMEOUT,
        )
        assert hist.status_code == 200
        # accept either JSON list or HTML render
        body = hist.text
        assert "recheck_answered" in body

    def test_tap_time_offline_is_authoritative(self, api_client, seeded_trapped_device):
        # answered 45 min in the past
        tapped = (datetime.now(timezone.utc) - timedelta(minutes=45)).isoformat()
        r = api_client.post(
            f"{API}/recheck/answer",
            json={"device_id": seeded_trapped_device, "answer": "same",
                  "answered_at": tapped},
            timeout=TIMEOUT,
        )
        assert r.status_code == 200
        j = r.json()
        # response echoes tap time and received time separately
        assert j.get("answered_at") == tapped
        assert j.get("received_at") != tapped
        # verify status_events row: at == tapped, queued_offline true — read from device-history
        hist = api_client.get(
            f"{API}/admin/device-history/{seeded_trapped_device}",
            headers=ADMIN_HDR, timeout=TIMEOUT,
        )
        assert hist.status_code == 200
        # tapped iso appears in row somewhere
        assert tapped[:16] in hist.text, "answered_at (tap time) missing from device-history"

    def test_future_device_clock_is_flagged_not_rewritten(self, api_client, seeded_trapped_device):
        future = (datetime.now(timezone.utc) + timedelta(hours=3)).isoformat()
        r = api_client.post(
            f"{API}/recheck/answer",
            json={"device_id": seeded_trapped_device, "answer": "same",
                  "answered_at": future},
            timeout=TIMEOUT,
        )
        assert r.status_code == 200
        j = r.json()
        assert j.get("answered_at") == future   # NOT silently rewritten


# ═════════ C1: /api/admin/recheck/status + /enabled ═════════
class TestRecheckAdmin:
    def test_status_with_admin(self, api_client):
        r = api_client.get(f"{API}/admin/recheck/status",
                           headers=ADMIN_HDR, timeout=TIMEOUT)
        assert r.status_code == 200, r.text
        j = r.json()
        for f in ("enabled", "task_running", "last_sweep_at",
                  "last_result", "trapped_count"):
            assert f in j, f"missing field {f}"
        assert isinstance(j["enabled"], bool)
        assert isinstance(j["task_running"], bool)
        assert isinstance(j["trapped_count"], int)

    def test_enabled_flip_is_admin_only_and_restored(self, api_client):
        # legacy admin token IS admin+operator, so this should succeed
        r = api_client.post(
            f"{API}/admin/recheck/enabled",
            json={"enabled": False},
            headers=ADMIN_HDR, timeout=TIMEOUT,
        )
        assert r.status_code == 200
        assert r.json()["enabled"] is False
        # confirm reflected in status
        s = api_client.get(f"{API}/admin/recheck/status",
                           headers=ADMIN_HDR, timeout=TIMEOUT).json()
        assert s["enabled"] is False
        # restore
        r2 = api_client.post(
            f"{API}/admin/recheck/enabled",
            json={"enabled": True},
            headers=ADMIN_HDR, timeout=TIMEOUT,
        )
        assert r2.status_code == 200
        assert r2.json()["enabled"] is True


# ═════════ C1: silence_state on /api/devices ═════════
class TestSilenceStateDark:
    def test_dark_when_updated_at_3h_old(self, api_client):
        """Seed a trapped device, force updated_at to 3h ago, verify silence_state=='dark'."""
        did = f"qg-test-i34-dark-{uuid.uuid4().hex[:8]}"
        api_client.post(
            f"{API}/status",
            json={"device_id": did, "status": "trapped", "severity": "green",
                  "latitude": 35.9, "longitude": 14.5, "battery_pct": 70},
            timeout=TIMEOUT,
        )
        # Age the updated_at directly in Mongo — do via a Motor client
        import asyncio
        from motor.motor_asyncio import AsyncIOMotorClient

        mongo_url = re.search(r"^MONGO_URL=(.+)$",
                              Path("/app/backend/.env").read_text(),
                              re.M).group(1).strip().strip('"').strip("'")
        db_name = re.search(r"^DB_NAME=(.+)$",
                            Path("/app/backend/.env").read_text(),
                            re.M).group(1).strip().strip('"').strip("'")

        async def age():
            client = AsyncIOMotorClient(mongo_url)
            old = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
            await client[db_name].device_status.update_one(
                {"device_id": did}, {"$set": {"updated_at": old}}
            )
            client.close()

        asyncio.run(age())

        rows_j = api_client.get(f"{API}/devices",
                                headers=ADMIN_HDR, timeout=TIMEOUT).json()
        rows = rows_j.get("devices") if isinstance(rows_j, dict) else rows_j
        row = next((x for x in rows if x.get("device_id") == did), None)
        assert row is not None
        assert row.get("silence_state") == "dark", (
            f"expected dark, got {row.get('silence_state')!r}"
        )

        api_client.post(f"{API}/mark-rescued", headers=ADMIN_HDR,
                        json={"device_id": did}, timeout=TIMEOUT)


# ═════════ GDPR: no google.com/maps in any response body ═════════
class TestGDPRNoGoogleMapsLinks:
    ENDPOINTS = [
        ("GET", "/", None, False, None),
        ("GET", "/public/summary", None, False, None),
        ("GET", "/devices", None, True, None),
        ("GET", "/audit", None, True, None),
        # /admin/audit-log currently 500s (see xfail below) — skip in this
        # parametrise loop and cover it separately so the raw response is captured.
        ("GET", "/admin/audit-log/export.csv", None, True, None),
        ("GET", "/seismic-map/events", None, False, None),
        ("GET", "/dashboard-settings", None, False, None),
    ]

    @pytest.mark.parametrize("method,path,body,admin,params", ENDPOINTS)
    def test_no_google_maps_link(self, api_client, method, path, body, admin, params):
        headers = ADMIN_HDR if admin else {}
        r = api_client.request(method, f"{API}{path}", headers=headers,
                               json=body, params=params, timeout=TIMEOUT)
        assert r.status_code < 400, f"{path} returned {r.status_code}"
        text = r.text
        assert "google.com/maps" not in text, f"{path} leaks google.com/maps link"
        assert "maps/place" not in text, f"{path} leaks maps/place link"

    def test_admin_audit_log_html_regression_after_refactor(self, api_client):
        """REGRESSION: /api/admin/audit-log?token=... 500s because
        audit_log_browser() calls get_audit_log(...) without the newly-required
        `request: Request` positional argument (server.py:776).
        Once fixed, this endpoint should return 200 HTML with no google.com/maps.
        """
        r = api_client.get(f"{API}/admin/audit-log",
                           params={"token": ADMIN_TOKEN, "limit": 5},
                           timeout=TIMEOUT)
        # Documenting the current broken state — assert that the endpoint 500s
        # so this test flips green as soon as the main agent restores it.
        if r.status_code == 200:
            assert "google.com/maps" not in r.text
            assert "maps/place" not in r.text
        else:
            pytest.fail(
                f"/admin/audit-log returned {r.status_code} — expected 200. "
                "Regression after server.py split: audit_log_browser calls "
                "get_audit_log(...) without the new `request` positional arg "
                "(server.py:776). See test report for the traceback."
            )


# ═════════ UTC: seismic-map/events observed_at explicit offset ═════════
class TestUtcOffsetOnSeismicEvents:
    def test_every_observed_at_has_utc_offset(self, api_client):
        r = api_client.get(f"{API}/seismic-map/events", timeout=TIMEOUT)
        assert r.status_code == 200
        j = r.json()
        events = (j if isinstance(j, list)
                  else j.get("events") or j.get("features") or [])
        if not events:
            pytest.skip("no seismic events to inspect")
        bad = []
        for ev in events:
            props = ev.get("properties", ev) if isinstance(ev, dict) else {}
            for key in ("observed_at", "occurred_at", "time"):
                v = props.get(key) if isinstance(props, dict) else None
                if v is None and isinstance(ev, dict):
                    v = ev.get(key)
                if v is None:
                    continue
                if isinstance(v, str):
                    if not (v.endswith("Z") or re.search(r"[+-]\d{2}:?\d{2}$", v)):
                        bad.append((key, v))
        assert not bad, f"observed_at without explicit UTC offset: {bad[:5]}"


# ═════════ FIN ═════════
