"""#320 (Paul, 2026-08-29) — "Remove all test people" must clear every
test row, no matter which surface created it.

Live symptom Paul hit on 2026-08-29:
  · The "33 test people" count read (5) before he added anything and
    still read (5) after removing.
  · One survivor's device_id was `qg-1785757225898-jy34olbg` — a
    real-device shape (13-digit epoch + 8 lowercase). Tagged TEST on
    the sidebar, showing "trapped for 22 days".

Root cause: the previous /admin/test-people/clear only deleted rows
whose `_test_seed` matched SEED_TAG. Rows flagged as test via any other
surface (mark-test on a real device_id, the B5 load-test seeder, old
diag/e2e/snippet rows) survived indefinitely.

These tests cover every surface a test row can be born from:
  A. /admin/test-people/seed (existing, regression guard)
  B. /admin/devices/{id}/mark-test (Paul's live survivor)
  C. Load-test seeder shape (`qg-loadtest-*` with `synthetic:true`)
  D. Marker-id shapes (diag/e2e/snippet) without any flag set
  E. `is_test:true` on an otherwise-real-shaped id

After a single POST /admin/test-people/clear, all of A–E must be gone
from device_status, and every alarm they raised must be resolved.
"""
import os
import time
import uuid

import pytest
import requests

BASE = os.environ.get("EXTERNAL_URL", "http://localhost:8001").rstrip("/")
ADMIN_TOKEN = os.environ.get("ADMIN_TRIGGER_PASSWORD", "")
H = {"x-admin-token": ADMIN_TOKEN, "Content-Type": "application/json"}


def _skip_if_no_admin():
    if not ADMIN_TOKEN:
        pytest.skip("ADMIN_TRIGGER_PASSWORD not set")


def _get_status_rows():
    """Best-effort read of the whole device_status collection via the
    same admin endpoint the dashboard uses."""
    r = requests.get(
        f"{BASE}/api/admin/test-entries",
        headers=H,
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def _open_alarms():
    """Every open (unresolved) alarm the board would show."""
    r = requests.get(f"{BASE}/api/board/alarms", timeout=30)
    if r.status_code != 200:
        return []
    body = r.json()
    if isinstance(body, dict):
        return body.get("alarms") or body.get("items") or []
    return body or []


def _clear_all() -> dict:
    r = requests.post(
        f"{BASE}/api/admin/test-people/clear", headers=H, timeout=60
    )
    r.raise_for_status()
    return r.json()


def _seed() -> dict:
    r = requests.post(
        f"{BASE}/api/admin/test-people/seed", headers=H, timeout=60
    )
    r.raise_for_status()
    return r.json()


def _upsert_row(row: dict) -> None:
    """Insert a device_status row directly through the status endpoint
    so we don't need mongo credentials in the test process."""
    payload = {
        "deviceId": row["device_id"],
        "status": row.get("status", "trapped"),
        "severity": row.get("severity", "red"),
        "mobility": row.get("mobility", "trapped"),
        "battery_pct": row.get("battery_pct", 55),
        "platform": row.get("platform", "ios"),
        "latitude": row.get("latitude", 35.8989),
        "longitude": row.get("longitude", 14.5145),
        "accuracy_m": row.get("accuracy_m", 12.0),
        "display_name": row.get("display_name", "TEST Fixture 320"),
    }
    r = requests.post(f"{BASE}/api/status", json=payload, timeout=30)
    assert 200 <= r.status_code < 300, (r.status_code, r.text[:400])


def _mark_test(device_id: str) -> None:
    r = requests.post(
        f"{BASE}/api/admin/devices/{device_id}/mark-test",
        headers=H,
        json={"is_test": True},
        timeout=30,
    )
    r.raise_for_status()


# --------------------------------------------------------------------- A
def test_clear_removes_seeded_batch():
    _skip_if_no_admin()
    _clear_all()
    _seed()
    body = _clear_all()
    # Nothing seeded-33-shaped survives.
    entries = _get_status_rows()
    survivors = [
        d for d in entries.get("devices", [])
        if str(d.get("device_id") or "").startswith("qg-seeded-33-")
    ]
    assert survivors == [], survivors
    assert body["removed"] >= 33, body
    assert "matched_by" in body


# --------------------------------------------------------------------- B
def test_clear_removes_mark_test_flagged_real_device():
    """Paul's live survivor case: a real-shaped device_id
    (qg-<13 digit epoch>-<8 lowercase chars>) flagged via mark-test."""
    _skip_if_no_admin()
    _clear_all()

    epoch = int(time.time() * 1000)
    real_shaped = f"qg-{epoch}-jy34olbg"
    _upsert_row({
        "device_id": real_shaped,
        "status": "trapped",
        "severity": "red",
        "display_name": "TEST Paul survivor 320",
    })
    _mark_test(real_shaped)

    body = _clear_all()
    entries = _get_status_rows()
    survivors = [
        d for d in entries.get("devices", [])
        if d.get("device_id") == real_shaped
    ]
    assert survivors == [], (
        "mark-test-flagged real device survived /clear — this is the "
        "exact #320 symptom Paul hit.",
        survivors,
    )
    assert body["matched_by"]["synthetic_flag"] >= 1, body


# --------------------------------------------------------------------- C
def test_clear_removes_loadtest_shape_rows():
    _skip_if_no_admin()
    _clear_all()

    run_id = uuid.uuid4().hex[:8]
    for i in range(3):
        _upsert_row({
            "device_id": f"qg-loadtest-{run_id}-{i:06d}",
            "status": "trapped",
            "severity": "yellow",
            "display_name": "TEST loadtest 320",
        })

    _clear_all()
    entries = _get_status_rows()
    survivors = [
        d for d in entries.get("devices", [])
        if str(d.get("device_id") or "").startswith("qg-loadtest-")
    ]
    assert survivors == [], survivors


# --------------------------------------------------------------------- D
def test_clear_removes_marker_id_shapes():
    """diag/e2e/snippet/demo/playwright-shaped device_ids are recognised
    as test by is_test_device() alone, even if no flag is ever set."""
    _skip_if_no_admin()
    _clear_all()

    markers = [
        f"qg-diag-{uuid.uuid4().hex[:6]}",
        f"qg-rescue-e2e-{uuid.uuid4().hex[:6]}",
        f"qg-snippet-test-{uuid.uuid4().hex[:6]}",
    ]
    for did in markers:
        _upsert_row({
            "device_id": did,
            "status": "trapped",
            "severity": "green",
            "display_name": "TEST marker 320",
        })

    _clear_all()
    entries = _get_status_rows()
    survivors = [
        d for d in entries.get("devices", [])
        if d.get("device_id") in markers
    ]
    assert survivors == [], survivors


# --------------------------------------------------------------------- E
def test_mixed_batch_end_to_end():
    """The composite case: /seed + a mark-test survivor + a marker-shape
    orphan all present at once. A single /clear removes all three
    categories and resolves all their alarms."""
    _skip_if_no_admin()
    _clear_all()

    # A: 33 seeded
    _seed()
    # B: real-shaped, mark-test-flagged
    epoch = int(time.time() * 1000)
    survivor_id = f"qg-{epoch}-jy34olbg"
    _upsert_row({
        "device_id": survivor_id,
        "status": "trapped",
        "severity": "red",
        "display_name": "TEST survivor 320-E",
    })
    _mark_test(survivor_id)
    # D: marker-shape, no flag
    diag_id = f"qg-diag-{uuid.uuid4().hex[:6]}"
    _upsert_row({
        "device_id": diag_id,
        "status": "trapped",
        "severity": "yellow",
        "display_name": "TEST diag 320-E",
    })

    body = _clear_all()

    entries = _get_status_rows()
    ids_after = {d.get("device_id") for d in entries.get("devices", [])}
    assert survivor_id not in ids_after, body
    assert diag_id not in ids_after, body
    assert not any(str(i or "").startswith("qg-seeded-33-") for i in ids_after), body

    # And no open alarm attributable to any of the three categories.
    alarms = _open_alarms()
    for a in alarms:
        did = str(a.get("device_id") or "")
        assert not did.startswith("qg-seeded-33-"), a
        assert did != survivor_id, a
        assert did != diag_id, a
        assert not a.get("is_test"), a
