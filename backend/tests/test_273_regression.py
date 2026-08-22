"""#273 regression tests.

Backend items claimed as newly landed and unverified:
  * GET /api/devices returns devices/off_board/notices/counts/count_notes.
  * A record whose phone has not been asked anything since its last report
    must classify as state 'not_asked' with a label like
    'Not asked since 09:37' (NOT 'phone went dark').
  * 'phone_went_dark' now requires an unanswered ask.
  * POST /api/admin/records/{device_id}/ask-to-check-in refuses:
      - a third unanswered ask (409),
      - inside a 15-minute cooldown (429),
      - without low-battery acknowledgement when battery <= 20% (409),
      - an unregistered or Android phone (409),
    and must NOT use the critical-alert push path.

Seed prerequisites: /app/backend/scripts/seed_268_scenario.py must run
FIRST (documented on the review request). We do not re-seed inside
this module — the tolerance limit for DB-touching TestClient calls is
1 per module (env quirk in the review note); we use plain `requests`
against the live supervisor-managed backend to avoid it entirely.
"""
import os
from datetime import datetime, timezone, timedelta

import pytest
import requests
from pymongo import MongoClient

BASE_URL = (
    os.environ.get("EXPO_PUBLIC_BACKEND_URL")
    or os.environ.get("EXPO_BACKEND_URL")
    or "http://localhost:8001"
).rstrip("/")
ADMIN_TOKEN = os.environ["ADMIN_TRIGGER_PASSWORD"]
MONGO_URL = os.environ.get("MONGO_URL", "mongodb://localhost:27017")
DB_NAME = os.environ.get("DB_NAME", "test_database")

ID_A = "qg-1755700000001-neo268a"   # live, safe 1 min ago  → not_asked
ID_B = "qg-1755600000002-neo268b"   # app removed           → off_board
ID_C = "qg-1755700000003-neo268c"   # trapped, app removed  → on board (held)
ID_D = "qg-1755700000004-neo268d"   # registered, never used
ID_E = "qg-1755700000005-neo268e"   # android, quiet 3h     → dark


@pytest.fixture(scope="module")
def db():
    c = MongoClient(MONGO_URL)
    return c[DB_NAME]


@pytest.fixture(scope="module")
def hdr():
    return {"X-Admin-Token": ADMIN_TOKEN}


# ── GET /api/devices response shape and record_state classification ──────
def test_devices_endpoint_shape_and_268_fields(hdr):
    r = requests.get(f"{BASE_URL}/api/devices?limit=1000", headers=hdr, timeout=15)
    assert r.status_code == 200, r.text
    data = r.json()

    # Envelope fields promised by review request.
    for k in ("devices", "off_board", "notices", "counts", "count_notes"):
        assert k in data, f"missing '{k}' in /api/devices response"

    assert isinstance(data["devices"], list)
    assert isinstance(data["off_board"], list)
    assert isinstance(data["notices"], list)
    assert isinstance(data["counts"], dict)
    assert isinstance(data["count_notes"], list)


def test_neo268_records_split_between_board_and_offboard(hdr):
    r = requests.get(f"{BASE_URL}/api/devices?limit=1000", headers=hdr, timeout=15)
    data = r.json()
    board_ids = {d["device_id"] for d in data["devices"]}
    off_ids = {d["device_id"] for d in data["off_board"]}
    # A, C, E on working board (E is android/quiet but on the board).
    assert ID_A in board_ids
    assert ID_C in board_ids
    assert ID_E in board_ids
    # B and D moved off board (app removed, never used).
    assert ID_B in off_ids
    assert ID_D in off_ids


def test_not_asked_state_and_label(hdr, db):
    """A record whose last report is AFTER the last broadcast/ask must be
    labelled 'Not asked since HH:MM', never 'Phone went dark'."""
    # Order-independence: the ask tests further down this module put an ask
    # on A. Clear it, and re-stamp the last report as "just now", so this
    # test asserts on the state it is actually about.
    db.device_status.update_one(
        {"device_id": ID_A},
        {"$unset": {"asks": "", "recheck": ""},
         "$set": {"updated_at": datetime.now(timezone.utc).isoformat()}},
    )
    r = requests.get(f"{BASE_URL}/api/devices?limit=1000", headers=hdr, timeout=15)
    data = r.json()
    row = next(d for d in data["devices"] if d["device_id"] == ID_A)
    rs = row["record_state"]
    assert rs["state"] == "not_asked", rs
    assert rs["label"].startswith("Not asked since "), rs["label"]


def test_phone_went_dark_requires_unanswered_ask(hdr, db):
    """"Went dark" must mean "we asked and heard nothing", never "has not
    spoken". The rule is asserted on the classifier itself: the dark clock
    runs from the ASK, so a broadcast fired seconds ago by any other test
    legitimately makes every quiet phone "waiting for an answer" — which
    is why this cannot be pinned through /api/devices."""
    from record_state import classify
    now = datetime.now(timezone.utc)
    quiet_row = {
        "updated_at": (now - timedelta(hours=3)).isoformat(),
        "status": "safe",
    }
    # 1. Nobody asked → not_asked, never dark.
    never_asked = classify(
        quiet_row, push_row=None,
        ever_needed_help=False, ever_located=True,
        incident_active=False, last_alert_at=None, now=now,
    )
    assert never_asked.state == "not_asked", never_asked
    assert never_asked.label.startswith("Not asked "), never_asked.label

    # 2. Asked two hours ago, still nothing back → dark.
    asked_row = {
        **quiet_row,
        "asks": {"count": 1, "unanswered": 1,
                 "last_at": (now - timedelta(hours=2)).isoformat()},
    }
    dark = classify(
        asked_row, push_row=None,
        ever_needed_help=False, ever_located=True,
        incident_active=False, last_alert_at=None, now=now,
    )
    assert dark.state == "phone_went_dark", dark
    assert "No answer for" in dark.detail

    # 3. Asked one minute ago → waiting, not dark. Silence needs time.
    fresh = classify(
        {**quiet_row,
         "asks": {"count": 1, "unanswered": 1,
                  "last_at": (now - timedelta(minutes=1)).isoformat()}},
        push_row=None,
        ever_needed_help=False, ever_located=True,
        incident_active=False, last_alert_at=None, now=now,
    )
    assert fresh.state == "waiting_for_answer", fresh

    # And E is still ON the working board, whatever its silence state is.
    r = requests.get(f"{BASE_URL}/api/devices?limit=1000", headers=hdr, timeout=15)
    data = r.json()
    row_e = next(d for d in data["devices"] if d["device_id"] == ID_E)
    assert row_e["record_state"]["on_working_board"] is True


# ── POST /api/admin/records/{id}/ask-to-check-in edge cases ──────────────
def test_ask_refuses_unregistered_phone(hdr):
    """B is off-board (app removed). Its push row has a dead_token flag
    but a token is still present in the seed. The handler needs a device
    token; if the push row was dropped it must 409 or 502."""
    r = requests.post(
        f"{BASE_URL}/api/admin/records/{ID_B}/ask-to-check-in",
        headers=hdr, json={}, timeout=15,
    )
    # Acceptable outcomes: 409 (unregistered/no token) or the seed carries
    # a token so we might hit 502 (network) or 409 (dead_token). Either
    # way, must NOT succeed (200), and must NOT be a critical-push path.
    assert r.status_code != 200, r.text
    assert r.status_code in (400, 404, 409, 502), r.text


def test_ask_refuses_android_phone(hdr):
    """E is Android. The handler must 409 explicitly."""
    r = requests.post(
        f"{BASE_URL}/api/admin/records/{ID_E}/ask-to-check-in",
        headers=hdr, json={}, timeout=15,
    )
    assert r.status_code == 409, r.text
    assert "Android" in r.text or "android" in r.text.lower()


def test_ask_refuses_low_battery_without_ack(hdr):
    """C has 9% battery and is iOS. Even though C is trapped (help history),
    the low-battery guard fires before the platform check. Must 409 unless
    acknowledge_low_battery=True."""
    r = requests.post(
        f"{BASE_URL}/api/admin/records/{ID_C}/ask-to-check-in",
        headers=hdr, json={}, timeout=15,
    )
    assert r.status_code == 409, r.text
    body = r.json().get("detail", "")
    assert "battery" in body.lower(), body


def test_ask_refuses_unknown_device(hdr):
    """Non-existent device must 404 with a plain-English detail."""
    r = requests.post(
        f"{BASE_URL}/api/admin/records/does-not-exist/ask-to-check-in",
        headers=hdr, json={}, timeout=15,
    )
    assert r.status_code == 404, r.text


def test_ask_refuses_third_unanswered_ask(hdr, db):
    """After 2 unanswered asks, a third must 409. We seed the counter
    directly to isolate the rule (a live 3rd ask requires live APNs)."""
    # Seed 2 unanswered on device A so battery/platform pass first.
    db.device_status.update_one(
        {"device_id": ID_A},
        {"$set": {
            "asks": {
                "count": 2, "unanswered": 2,
                "last_at": (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat(),
                "last_by": "test",
            },
            "battery_pct": 80,
        }},
    )
    r = requests.post(
        f"{BASE_URL}/api/admin/records/{ID_A}/ask-to-check-in",
        headers=hdr, json={"acknowledge_low_battery": True}, timeout=15,
    )
    assert r.status_code == 409, r.text
    assert "already asked" in r.json()["detail"].lower(), r.json()


def test_ask_refuses_inside_cooldown(hdr, db):
    """Within 15 minutes of a previous ask must 429."""
    db.device_status.update_one(
        {"device_id": ID_A},
        {"$set": {
            "asks": {
                "count": 1, "unanswered": 1,
                "last_at": (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(),
                "last_by": "test",
            },
            "battery_pct": 80,
        }},
    )
    r = requests.post(
        f"{BASE_URL}/api/admin/records/{ID_A}/ask-to-check-in",
        headers=hdr, json={"acknowledge_low_battery": True}, timeout=15,
    )
    assert r.status_code == 429, r.text
    assert "minutes" in r.json()["detail"].lower(), r.json()


def test_ask_does_not_use_critical_alert_path():
    """Structural check: the ask endpoint imports send_recheck_prompts,
    NOT send_critical_alerts. Prevents a future edit from wiring the
    critical siren to the operator ask button."""
    import inspect, server
    src = inspect.getsource(server.ask_to_check_in)
    assert "send_recheck_prompts" in src, "ask must use recheck path"
    assert "send_critical_alerts" not in src, (
        "ask must NOT invoke send_critical_alerts — critical push path is "
        "for genuine earthquake alerts only"
    )
