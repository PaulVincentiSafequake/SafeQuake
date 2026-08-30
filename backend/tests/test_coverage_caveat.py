"""
Coverage caveat (Paul, 2026-09-03) — single source of truth contract.

Verifies the string
    "These numbers count only people using Quake Angel.
     Others may be trapped who we cannot see."
comes from ONE place in the code (people_counts.COVERAGE_CAVEAT) and is
carried unchanged on every server-side surface a human might read the
numbers on:

  * /api/public/summary       → coverage_caveat field
  * /api/devices              → coverage_caveat field
  * /api/admin/audit-log/export.csv → embedded row
  * (Audit PDF, B1 team PDF, B2 public PDF also carry it, verified by
    inspection — the file bytes are opaque to pure-string tests and
    the module already has PDF snapshot tests we do not want to
    duplicate.)

If two of these ever disagree, the mobile app, the dashboard and the
reports will silently drift apart and start telling different stories
about coverage.
"""
import os

import pytest
import requests

from people_counts import COVERAGE_CAVEAT


BASE_URL = os.environ.get(
    "EXPO_PUBLIC_BACKEND_URL",
    "https://rescue-alert-hub-3.preview.emergentagent.com",
).rstrip("/")
ADMIN_TOKEN = os.environ.get("ADMIN_TRIGGER_PASSWORD", "m11vRwfDoxnHvIMLkKzjUwQy")


def test_caveat_constant_wording_is_locked():
    """The exact string is the contract. If a wording change is needed,
    update it in people_counts.COVERAGE_CAVEAT and this test in lockstep
    — never in one surface at a time."""
    assert COVERAGE_CAVEAT == (
        "These numbers count only people using Quake Angel. "
        "Others may be trapped who we cannot see."
    )


def test_public_summary_carries_caveat():
    r = requests.get(f"{BASE_URL}/api/public/summary", timeout=15)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("coverage_caveat") == COVERAGE_CAVEAT


def test_devices_endpoint_carries_caveat():
    """`/api/devices` is admin-gated. The admin token principal is
    accepted for automated tests just as it is for the trigger endpoint."""
    r = requests.get(
        f"{BASE_URL}/api/devices",
        headers={"X-Admin-Token": ADMIN_TOKEN},
        timeout=15,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("coverage_caveat") == COVERAGE_CAVEAT


def test_audit_csv_export_contains_caveat_row():
    """The CSV is the fallback when the dashboard is down. Same
    single-source string as the dashboard, plain-text row."""
    r = requests.get(
        f"{BASE_URL}/api/admin/audit-log/export.csv",
        headers={"X-Admin-Token": ADMIN_TOKEN},
        timeout=30,
    )
    assert r.status_code == 200, r.text
    text = r.text
    # The row is: coverage_caveat,<the sentence>,...
    assert COVERAGE_CAVEAT in text, (
        "audit CSV export did not include the coverage caveat — the "
        "dashboard and the CSV must never drift apart."
    )
