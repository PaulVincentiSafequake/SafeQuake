"""End-to-end #322 verification (Step C of the review request):

  1. POST /api/status with each wire bucket via the running backend.
  2. GET /api/devices with admin auth and assert the row for the
     just-posted device carries the SAME bucket string.
  3. Feed that bucket string through the EXACT JS normalizer that
     lives in memory/dashboard_build/index.html and assert the number
     the dashboard renders comes out.

If any link in the chain regressed, this test names it.
"""
from __future__ import annotations

import os
import random
import re
import shutil
import string
import subprocess
import time
from pathlib import Path

import pytest
import requests

BASE_URL = os.environ.get(
    "EXPO_PUBLIC_BACKEND_URL",
    "https://coverage-caveat-ui.preview.emergentagent.com",
).rstrip("/")
ADMIN_TOKEN = os.environ.get(
    "ADMIN_TRIGGER_PASSWORD", "m11vRwfDoxnHvIMLkKzjUwQy"
)  # from /app/memory/test_credentials.md

DASH_PATH = (
    Path(__file__).resolve().parents[2]
    / "memory"
    / "dashboard_build"
    / "index.html"
)


def _rand_suffix(n=8) -> str:
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=n))


def _dashboard_normalizer_body() -> str:
    dash = DASH_PATH.read_text()
    m = re.search(
        r"groupSize:\s*\(function\s*\(raw\)\s*\{([^}]+)\}\)\(d\.group_size\)",
        dash,
    )
    assert m, "Could not locate groupSize normalizer in dashboard_build/index.html"
    return m.group(1)


def _run_normalizer(raw_value):
    """Run the dashboard's JS normalizer against `raw_value` via node -e.
    raw_value can be a string, a number, or None (mapped to null)."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available on runner")
    body = _dashboard_normalizer_body()
    if raw_value is None:
        js_lit = "null"
    elif isinstance(raw_value, (int, float)):
        js_lit = str(raw_value)
    else:
        js_lit = '"' + str(raw_value).replace('"', '\\"') + '"'
    js = (
        "const fn = function (raw) {"
        + body
        + "};"
        + f"const out = fn({js_lit});"
        + "process.stdout.write(out === null || out === undefined "
        + "? 'null' : String(out));"
    )
    r = subprocess.run([node, "-e", js], capture_output=True, text=True, timeout=10)
    assert r.returncode == 0, f"node run failed: {r.stderr}"
    return r.stdout.strip()


@pytest.fixture(scope="module")
def api():
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    return s


def _post_status(api, device_id: str, group_size):
    payload = {
        "deviceId": device_id,
        "status": "trapped",
        "severity": "red",
        "mobility": "trapped",
        "coords": {"lat": 35.8989, "lng": 14.5146},  # Malta
    }
    if group_size is not None:
        payload["group_size"] = group_size
    r = api.post(f"{BASE_URL}/api/status", json=payload, timeout=15)
    assert r.status_code == 200, (
        f"POST /api/status returned {r.status_code}: {r.text}"
    )
    return r.json()


def _get_device_row(api, device_id: str) -> dict:
    """Fetch /api/devices with admin auth and locate the row we just posted."""
    r = api.get(
        f"{BASE_URL}/api/devices",
        headers={"X-Admin-Token": ADMIN_TOKEN},
        timeout=20,
    )
    assert r.status_code == 200, (
        f"GET /api/devices returned {r.status_code}: {r.text[:400]}"
    )
    body = r.json()
    rows = body.get("devices") or body if isinstance(body, dict) else body
    if isinstance(body, dict) and "devices" in body:
        rows = body["devices"]
    for row in rows:
        if row.get("deviceId") == device_id or row.get("device_id") == device_id:
            return row
    pytest.fail(
        f"deviceId {device_id!r} not present in /api/devices response "
        f"({len(rows)} rows scanned)"
    )


@pytest.mark.parametrize(
    "bucket, expected_number",
    [
        ("4", 4),
        ("5_plus", 5),
        ("just_me", 1),
    ],
)
def test_group_size_roundtrips_end_to_end(api, bucket, expected_number):
    device_id = f"qg-e2e-322-{int(time.time() * 1000)}-{_rand_suffix()}"

    # 1) Mobile → backend
    _post_status(api, device_id, bucket)

    # tiny settle so /api/devices sees the write
    time.sleep(0.4)

    # 2) Backend → /api/devices
    row = _get_device_row(api, device_id)
    assert row.get("group_size") == bucket, (
        f"Full-stack contract broke: posted group_size={bucket!r}, "
        f"/api/devices returned {row.get('group_size')!r} for "
        f"device {device_id}"
    )

    # 3) Dashboard normalizer against the very value the operator's
    #    browser would see.
    out = _run_normalizer(row.get("group_size"))
    assert out == str(expected_number), (
        f"Dashboard normalizer mapped {bucket!r} → {out!r}, "
        f"expected {expected_number!r}. Bug #322 would recur."
    )


def test_null_group_size_roundtrips_as_null(api):
    """A user who declines to answer the group-size prompt: the row
    must carry no group_size and the dashboard must honestly say
    'not given' (normalizer returns null), never a phantom number."""
    device_id = f"qg-e2e-322-null-{int(time.time() * 1000)}-{_rand_suffix()}"

    _post_status(api, device_id, None)
    time.sleep(0.4)

    row = _get_device_row(api, device_id)
    assert row.get("group_size") in (None, ""), (
        f"Skipped group_size should be null/absent; got {row.get('group_size')!r}"
    )

    out = _run_normalizer(row.get("group_size"))
    assert out == "null", (
        f"Dashboard normalizer must return null for missing group_size, "
        f"got {out!r}"
    )
