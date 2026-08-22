"""
Iteration 25 — Rescue-code + optional display_name feature backend tests.

Focus:
  - POST /api/status sanitizes display_name correctly (unicode, control chars,
    length cap, whitespace, null/empty → None).
  - GET /api/devices exposes short_code = last-5-uppercased(device_id) and
    display_name per row without dropping any pre-existing fields.
  - GET /api/audit surfaces short_code + display_name on every
    status / rescued / rescue_reverted event.
  - POST /api/mark-rescued and /api/unmark-rescued carry the display_name
    into the status_events audit row.
  - Regression: /api/cors-debug shape and /api/trigger-alert auth.

All test rows use device_ids prefixed with 'qg-test-' or 'test-' so the
cleanup fixture can remove them at the end.
"""
import os
import uuid
import pytest
import requests

BASE_URL = "http://localhost:8001"
from dotenv import load_dotenv
load_dotenv("/app/backend/.env")
# GET /api/devices is operator/admin gated as of 2026-08-13.
ADMIN_TOKEN = os.environ.get("ADMIN_TRIGGER_PASSWORD")


# ----------------------- fixtures -----------------------

@pytest.fixture(scope="session")
def api():
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    return s


@pytest.fixture(scope="session")
def created_ids():
    """Collect device_ids so we can purge test rows at teardown."""
    ids = set()
    yield ids
    # Cleanup: hit mongo indirectly via a tiny helper — the backend doesn't
    # expose a delete endpoint, so we delete straight through motor.
    try:
        from pymongo import MongoClient
        mc = MongoClient(os.environ.get("MONGO_URL", "mongodb://localhost:27017"))
        dbname = os.environ.get("DB_NAME", "test_database")
        db = mc[dbname]
        if ids:
            db.device_status.delete_many({"device_id": {"$in": list(ids)}})
            db.status_events.delete_many({"device_id": {"$in": list(ids)}})
    except Exception as e:
        print(f"cleanup warning: {e}")


def _post_status(api, device_id, display_name="__OMIT__", status="safe"):
    payload = {"deviceId": device_id, "status": status}
    if display_name != "__OMIT__":
        payload["display_name"] = display_name
    r = api.post(f"{BASE_URL}/api/status", json=payload)
    assert r.status_code == 200, f"POST /api/status failed: {r.status_code} {r.text}"
    return r.json()


def _get_device(api, device_id):
    r = api.get(f"{BASE_URL}/api/devices?limit=5000",
                headers={"X-Admin-Token": ADMIN_TOKEN})
    assert r.status_code == 200
    devices = r.json()["devices"]
    for d in devices:
        if d["device_id"] == device_id:
            return d
    return None


# ----------------------- 1) POST /api/status sanitization matrix -----------------------

SANITIZE_CASES = [
    ("omitted",        "__OMIT__",           None),
    ("null",           None,                 None),
    ("empty",          "",                   None),
    ("whitespace",     "   ",                None),
    ("simple",         "Paul",               "Paul"),
    ("trimmed",        "  Paul  ",           "Paul"),
    ("unicode_latin",  "José",               "José"),
    ("unicode_cjk",    "京子",               "京子"),
    ("control_chars",  "Paul\x00\x01\x07\x08\x1F", "Paul"),
    ("length_cap",     "X" * 50,             "X" * 40),
]


@pytest.mark.parametrize("label,raw,expected", SANITIZE_CASES,
                         ids=[c[0] for c in SANITIZE_CASES])
def test_status_display_name_sanitization(api, created_ids, label, raw, expected):
    """POST /api/status → verify display_name sanitization stored & echoed back."""
    device_id = f"qg-test-san-{label}-{uuid.uuid4().hex[:6]}"
    created_ids.add(device_id)
    _post_status(api, device_id, display_name=raw)
    d = _get_device(api, device_id)
    assert d is not None, f"device {device_id} not returned by /api/devices"
    assert d["display_name"] == expected, (
        f"case={label}: sent {raw!r} → got {d['display_name']!r}, expected {expected!r}"
    )


# ----------------------- 2) GET /api/devices short_code + shape -----------------------

def test_devices_short_code_uppercase_last5(api, created_ids):
    device_id = f"qg-test-shortcode-{uuid.uuid4().hex[:6]}"  # ends with 6 hex chars
    created_ids.add(device_id)
    _post_status(api, device_id, display_name="Paul")
    d = _get_device(api, device_id)
    assert d is not None
    assert d["short_code"] == device_id[-5:].upper()
    assert d["display_name"] == "Paul"


def test_devices_short_code_none_for_short_id(api, created_ids):
    """device_ids shorter than 3 chars should surface short_code=None.
    We can't create a 2-char id via /api/status (backend accepts it, but let's
    just verify the helper output for such a row exists as None when found)."""
    # Insert directly to test the derivation, then check.
    from pymongo import MongoClient
    mc = MongoClient(os.environ.get("MONGO_URL", "mongodb://localhost:27017"))
    dbname = os.environ.get("DB_NAME", "test_database")
    db = mc[dbname]
    short_id = "qz"  # 2-char id -> short_code should be None per helper
    created_ids.add(short_id)
    db.device_status.update_one(
        {"device_id": short_id},
        {"$set": {"device_id": short_id, "status": "safe", "updated_at": "2099-01-01T00:00:00+00:00", "display_name": None}},
        upsert=True,
    )
    d = _get_device(api, short_id)
    assert d is not None, "short-id row not found"
    assert d["short_code"] is None


def test_devices_backwards_compat_fields(api, created_ids):
    """Ensure no pre-existing fields were removed/renamed."""
    device_id = f"qg-test-compat-{uuid.uuid4().hex[:6]}"
    created_ids.add(device_id)
    api.post(f"{BASE_URL}/api/status", json={
        "deviceId": device_id,
        "status": "trapped",
        "severity": "red",
        "mobility": "trapped",
        "display_name": "Paul",
        "latitude": 35.9,
        "longitude": 14.5,
        "battery": {"level": 0.42, "state": "unplugged"},
    })
    d = _get_device(api, device_id)
    assert d is not None
    required_keys = {
        "device_id", "short_code", "display_name",
        "status", "severity", "mobility",
        "latitude", "longitude", "accuracy_m",
        "battery_pct", "battery_state", "platform",
        "updated_at", "rescued_at", "rescued_by",
        "pre_rescue_status", "pre_rescue_severity", "pre_rescue_mobility",
    }
    missing = required_keys - set(d.keys())
    assert not missing, f"missing keys on /api/devices row: {missing}"
    # Spot-check values
    assert d["status"] == "trapped"
    assert d["severity"] == "red"
    assert d["mobility"] == "trapped"
    assert d["battery_pct"] == 42
    assert d["battery_state"] == "unplugged"


# ----------------------- 3) GET /api/audit surfaces short_code+display_name -----------------------

def test_audit_status_event_has_short_code_and_display_name(api, created_ids):
    device_id = f"qg-test-audit-{uuid.uuid4().hex[:6]}"
    created_ids.add(device_id)
    _post_status(api, device_id, display_name="Aiko", status="trapped")
    r = api.get(headers={"X-Admin-Token": ADMIN_TOKEN}, url=f"{BASE_URL}/api/audit?kind=status&limit=200")
    assert r.status_code == 200
    events = r.json()["events"]
    match = next((e for e in events if e.get("device_id") == device_id), None)
    assert match is not None, f"no audit status event found for {device_id}"
    assert match["short_code"] == device_id[-5:].upper()
    assert match["display_name"] == "Aiko"


def test_audit_all_status_rescued_reverted_have_fields(api):
    """Every status/rescued/rescue_reverted event must include the two keys."""
    r = api.get(headers={"X-Admin-Token": ADMIN_TOKEN}, url=f"{BASE_URL}/api/audit?limit=200")
    assert r.status_code == 200
    events = r.json()["events"]
    for e in events:
        if e.get("kind") in ("status", "rescued", "rescue_reverted"):
            assert "short_code" in e, f"missing short_code on {e.get('kind')}: {e}"
            assert "display_name" in e, f"missing display_name on {e.get('kind')}: {e}"


# ----------------------- 4) mark-rescued carries display_name -----------------------

def test_mark_rescued_carries_display_name(api, created_ids):
    device_id = f"qg-test-rescued-{uuid.uuid4().hex[:6]}"
    created_ids.add(device_id)
    # Seed as trapped with a name.
    _post_status(api, device_id, display_name="X", status="trapped")
    # Mark rescued
    r = api.post(
        f"{BASE_URL}/api/mark-rescued",
        json={"deviceId": device_id, "rescued_by": "tester"},
        headers={"X-Admin-Token": ADMIN_TOKEN},
    )
    assert r.status_code == 200, f"mark-rescued failed: {r.status_code} {r.text}"
    # Verify audit
    r = api.get(headers={"X-Admin-Token": ADMIN_TOKEN}, url=f"{BASE_URL}/api/audit?kind=rescued&limit=200")
    assert r.status_code == 200
    events = r.json()["events"]
    match = next((e for e in events if e.get("device_id") == device_id), None)
    assert match is not None, "rescued audit event missing"
    assert match["display_name"] == "X"
    assert match["short_code"] == device_id[-5:].upper()


# ----------------------- 5) unmark-rescued carries display_name -----------------------

def test_unmark_rescued_carries_display_name(api, created_ids):
    device_id = f"qg-test-revert-{uuid.uuid4().hex[:6]}"
    created_ids.add(device_id)
    _post_status(api, device_id, display_name="Bea", status="trapped")
    api.post(f"{BASE_URL}/api/status", json={
        "deviceId": device_id, "status": "trapped",
        "severity": "yellow", "mobility": "mobile", "display_name": "Bea",
    })
    r = api.post(
        f"{BASE_URL}/api/mark-rescued",
        json={"deviceId": device_id},
        headers={"X-Admin-Token": ADMIN_TOKEN},
    )
    assert r.status_code == 200
    r = api.post(
        f"{BASE_URL}/api/unmark-rescued",
        json={"deviceId": device_id, "reverted_by": "tester"},
        headers={"X-Admin-Token": ADMIN_TOKEN},
    )
    assert r.status_code == 200, f"unmark-rescued failed: {r.status_code} {r.text}"
    r = api.get(headers={"X-Admin-Token": ADMIN_TOKEN}, url=f"{BASE_URL}/api/audit?kind=rescue_reverted&limit=200")
    assert r.status_code == 200
    events = r.json()["events"]
    match = next((e for e in events if e.get("device_id") == device_id), None)
    assert match is not None, "rescue_reverted event missing"
    assert match["display_name"] == "Bea"
    assert match["short_code"] == device_id[-5:].upper()


# ----------------------- 6) Regression -----------------------

def test_cors_debug_shape(api):
    r = api.get(f"{BASE_URL}/api/cors-debug")
    assert r.status_code == 200, r.text
    body = r.json()
    for k in ("allowed_origins", "allow_reason", "deploy_fingerprint"):
        assert k in body, f"missing key {k} in /api/cors-debug"


def test_trigger_alert_wrong_token_401(api):
    r = api.post(
        f"{BASE_URL}/api/trigger-alert",
        json={"magnitude": 6.4},
        headers={"X-Admin-Token": "wrong"},
    )
    assert r.status_code == 401


def test_trigger_alert_correct_token_200(api, stand_down_after):
    r = api.post(
        f"{BASE_URL}/api/trigger-alert",
        json={"magnitude": 6.4, "triggeredBy": "qg-test-suite",
              "confirmation_phrase": "SIREN"},
        headers={"X-Admin-Token": ADMIN_TOKEN},
    )
    assert r.status_code == 200, f"got {r.status_code}: {r.text}"
    body = r.json()
    assert body.get("status") == "broadcast"
    assert "recipients" in body
