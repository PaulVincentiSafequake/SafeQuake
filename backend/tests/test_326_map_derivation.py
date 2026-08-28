"""#326 (2026-09-02 — Paul): map-derivation contract on /api/devices.

Paul's rule, verbatim:
  "The moment an alert is triggered, every phone we alerted should
   appear red on the map straight away — before they answer anything.
   Silence must never be invisible. As people answer, their colour
   changes: red for needs help now, yellow for hurt but stable, green
   for safe. Rescued people leave too, whatever colour they were.
   Anyone who answered and then went quiet keeps their colour but
   gains a mark showing that pin is their last known position. Nothing
   is ever deleted — only taken off the live view."

  "yellow for walking wounded (trapped/severity=green) because they
   tapped 'I need help' and anyone who asks for help stays on the
   live map."

  "stand-down MUST NOT clear last_alerted_at — they stay on the board,
   red, until a human closes their case."

This test suite locks the whole contract, both at the helper level
(unit-testing map_color / last_known_position / silent_since_alert)
and end-to-end through /api/devices.

Uses the TestClient portal from conftest.py so we share one event loop
with the shared Motor client.
"""
from __future__ import annotations

import os
import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import requests
from dotenv import load_dotenv

load_dotenv("/app/backend/.env")

from fastapi.testclient import TestClient

from server import app
from people_counts import map_color, silent_since_alert, last_known_position


BASE = "http://localhost:8001"
ADMIN_TOKEN = os.environ["ADMIN_TRIGGER_PASSWORD"]
HDR_ADMIN = {"X-Admin-Token": ADMIN_TOKEN, "Content-Type": "application/json"}


client = TestClient(app)


# ── helpers ────────────────────────────────────────────────────────────
def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _new_id(tag: str = "326") -> str:
    """Deterministic-ish device id that WON'T match _is_test_device.
    We want these rows to be counted as real."""
    return f"dev-{tag}-{uuid.uuid4().hex[:12]}"


def _upsert_status(run_async, doc: dict) -> None:
    """Direct-to-Mongo upsert to skip POST /status validators and time
    logic — we want to control every field exactly."""
    import deps

    async def _do():
        await deps.db.device_status.update_one(
            {"device_id": doc["device_id"]},
            {"$set": doc},
            upsert=True,
        )
    run_async(_do)


def _upsert_push_device(run_async, device_id: str, platform: str = "ios") -> None:
    import deps

    async def _do():
        await deps.db.push_devices.update_one(
            {"user_id": device_id},
            {"$set": {"user_id": device_id, "platform": platform,
                      "device_token": f"tok-{device_id}",
                      "created_at": _iso(_now())}},
            upsert=True,
        )
    run_async(_do)


def _get_devices(since=None):
    # limit=5000 to defeat truncation — this DB has ~1200 device_status rows
    # of accumulated test pollution, and a row with no updated_at sorts
    # LAST (updated_at desc) so newly-inserted stubs are the first casualties
    # of a 1000-row cap. See /api/devices sort logic in server.py.
    params = {"limit": 5000}
    if since:
        params["since"] = since
    r = requests.get(f"{BASE}/api/devices", headers=HDR_ADMIN,
                     params=params, timeout=30)
    assert r.status_code == 200, r.text
    return r.json()


def _find_device(payload, device_id):
    for row in payload.get("devices", []):
        if row.get("device_id") == device_id:
            return row
    return None


def _delete_device(run_async, device_id):
    import deps

    async def _do():
        await deps.db.device_status.delete_many({"device_id": device_id})
        await deps.db.push_devices.delete_many({"user_id": device_id})
        await deps.db.status_events.delete_many({"device_id": device_id})
    run_async(_do)


# ── UNIT: helper functions (people_counts) ─────────────────────────────
class TestMapColorUnit:
    """Direct assertions on map_color/silent_since_alert/last_known_position.
    These are the single source of truth; /api/devices reads them."""

    def test_no_alert_no_status(self):
        row = {}
        assert silent_since_alert(row) is False
        assert map_color(row) is None
        assert last_known_position(row) is False

    def test_alerted_no_updated_at_is_silent(self):
        row = {"last_alerted_at": _iso(_now())}
        assert silent_since_alert(row) is True
        assert map_color(row) == "red"
        # last_known is False — we've never heard from them post-alert.
        assert last_known_position(row) is False

    def test_alerted_and_reported_since_is_not_silent(self):
        t0 = _now() - timedelta(minutes=5)
        t1 = _now()
        row = {"last_alerted_at": _iso(t0), "updated_at": _iso(t1),
               "status": "safe"}
        assert silent_since_alert(row) is False
        assert map_color(row) == "green"

    def test_stale_safe_prior_to_alert_still_red(self):
        """Silence-since-alert overrides prior 'safe'."""
        old = _now() - timedelta(hours=6)
        alert = _now() - timedelta(minutes=2)
        row = {"status": "safe", "updated_at": _iso(old),
               "last_alerted_at": _iso(alert)}
        assert silent_since_alert(row) is True
        assert map_color(row) == "red"

    def test_trapped_severity_red(self):
        assert map_color({"status": "trapped", "severity": "red"}) == "red"

    def test_trapped_severity_yellow(self):
        assert map_color({"status": "trapped", "severity": "yellow"}) == "yellow"

    def test_trapped_severity_green_is_yellow(self):
        """Walking wounded stays on the live map as yellow. Green is
        reserved for self-reported safe."""
        assert map_color({"status": "trapped", "severity": "green"}) == "yellow"

    def test_trapped_missing_severity_is_yellow(self):
        assert map_color({"status": "trapped"}) == "yellow"
        assert map_color({"status": "trapped", "severity": None}) == "yellow"

    def test_self_reported_safe(self):
        assert map_color({"status": "safe"}) == "green"

    def test_rescued_wins(self):
        row = {"status": "trapped", "severity": "red",
               "rescued_at": _iso(_now())}
        assert map_color(row) is None
        assert last_known_position(row) is False

    def test_rescued_beats_silent_since_alert(self):
        row = {"status": "trapped", "severity": "red",
               "last_alerted_at": _iso(_now()),
               "rescued_at": _iso(_now())}
        assert map_color(row) is None

    def test_not_responding_without_alert_is_offmap(self):
        row = {"status": "not_responding"}
        assert map_color(row) is None

    def test_not_responding_with_alert_is_red(self):
        row = {"status": "not_responding", "last_alerted_at": _iso(_now())}
        assert map_color(row) == "red"

    def test_last_known_position_when_answered_then_dark(self):
        """Answered post-alert (updated > alerted) AND now silent
        (updated > 45 min ago → silence_state == 'dark') → last_known=True,
        keeps color from their answer."""
        old = _now() - timedelta(minutes=60)
        alert = _now() - timedelta(minutes=90)
        row = {"status": "safe", "updated_at": _iso(old),
               "last_alerted_at": _iso(alert)}
        assert silent_since_alert(row) is False
        assert last_known_position(row) is True
        assert map_color(row) == "green"

    def test_last_known_position_when_silent_alive(self):
        """recheck.consecutive_missed >= 2 flips silence_state to
        'silent_alive'; last_known_position must be True."""
        row = {"status": "trapped", "severity": "yellow",
               "updated_at": _iso(_now() - timedelta(minutes=5)),
               "last_alerted_at": _iso(_now() - timedelta(minutes=30)),
               "recheck": {"consecutive_missed": 2}}
        assert silent_since_alert(row) is False
        assert last_known_position(row) is True
        assert map_color(row) == "yellow"

    def test_silent_since_alert_is_not_last_known(self):
        """A phone we alerted but never heard from is red for a different
        reason — it is not 'last known'."""
        row = {"last_alerted_at": _iso(_now())}
        assert silent_since_alert(row) is True
        assert last_known_position(row) is False


# ── /api/devices exposes the new fields ────────────────────────────────
class TestDevicesExposesFields:
    def test_every_row_carries_all_four_new_fields(self, run_async):
        did = _new_id("expose")
        _upsert_status(run_async, {
            "device_id": did, "status": "safe",
            "updated_at": _iso(_now()),
        })
        try:
            payload = _get_devices()
            row = _find_device(payload, did)
            assert row is not None
            for key in ("map_color", "last_known_position",
                        "silent_since_alert", "last_alerted_at"):
                assert key in row, f"missing {key} in /api/devices row"
            assert row["map_color"] == "green"
            assert row["last_known_position"] is False
            assert row["silent_since_alert"] is False
            assert row["last_alerted_at"] is None
        finally:
            _delete_device(run_async, did)


# ── /api/devices — full state matrix ───────────────────────────────────
class TestDevicesMapColorMatrix:
    """End-to-end map_color for every state; if any of these fail the
    dashboard cannot render the right pin."""

    def _assert_row(self, run_async, doc, expected_color,
                    expected_silent=None, expected_lkp=None):
        did = doc.setdefault("device_id", _new_id("mat"))
        _upsert_status(run_async, doc)
        try:
            row = _find_device(_get_devices(), did)
            assert row is not None, f"device {did} not returned by /api/devices"
            assert row["map_color"] == expected_color, \
                f"map_color: expected {expected_color!r}, got {row['map_color']!r} for {doc}"
            if expected_silent is not None:
                assert row["silent_since_alert"] is expected_silent
            if expected_lkp is not None:
                assert row["last_known_position"] is expected_lkp
        finally:
            _delete_device(run_async, did)

    def test_self_reported_safe_is_green(self, run_async):
        self._assert_row(run_async, {
            "status": "safe", "updated_at": _iso(_now()),
        }, "green", expected_silent=False, expected_lkp=False)

    def test_trapped_red(self, run_async):
        self._assert_row(run_async, {
            "status": "trapped", "severity": "red",
            "updated_at": _iso(_now()),
        }, "red")

    def test_trapped_yellow(self, run_async):
        self._assert_row(run_async, {
            "status": "trapped", "severity": "yellow",
            "updated_at": _iso(_now()),
        }, "yellow")

    def test_trapped_green_maps_to_yellow(self, run_async):
        """Paul: anyone who tapped 'I need help' never leaves the live map."""
        self._assert_row(run_async, {
            "status": "trapped", "severity": "green",
            "updated_at": _iso(_now()),
        }, "yellow")

    def test_trapped_missing_severity_is_yellow(self, run_async):
        self._assert_row(run_async, {
            "status": "trapped",
            "updated_at": _iso(_now()),
        }, "yellow")

    def test_rescued_null_color(self, run_async):
        self._assert_row(run_async, {
            "status": "trapped", "severity": "red",
            "updated_at": _iso(_now()),
            "rescued_at": _iso(_now()), "rescued_by": "test",
        }, None)

    def test_not_responding_no_alert_null(self, run_async):
        self._assert_row(run_async, {
            "status": "not_responding",
            "updated_at": _iso(_now() - timedelta(hours=1)),
        }, None)

    def test_silent_since_alert_prior_safe_is_red(self, run_async):
        """Prior safe status, updated_at older than last_alerted_at →
        silent-since-alert wins → red."""
        old = _now() - timedelta(hours=6)
        alerted = _now() - timedelta(minutes=3)
        self._assert_row(run_async, {
            "status": "safe",
            "updated_at": _iso(old),
            "last_alerted_at": _iso(alerted),
        }, "red", expected_silent=True, expected_lkp=False)

    def test_answered_then_dark_last_known_true(self, run_async):
        """Answered after the alert but has since gone dark. Color
        preserved from the answer; last_known_position=True."""
        alerted = _now() - timedelta(hours=2)
        answered = _now() - timedelta(minutes=55)  # > 45 min ago → dark
        self._assert_row(run_async, {
            "status": "safe",
            "updated_at": _iso(answered),
            "last_alerted_at": _iso(alerted),
        }, "green", expected_silent=False, expected_lkp=True)


# ── P0 side effect: broadcast stamps last_alerted_at + red pins ────────
class TestBroadcastSideEffect:
    """The reported P0: after POST /trigger-alert, every recipient
    (even one that has NEVER checked in) is on /api/devices with
    map_color=red and silent_since_alert=true. This is the
    'silence must never be invisible' fix."""

    def test_broadcast_stamps_all_recipients_red(self, run_async, stand_down_after):
        # 3 recipients: (A) never checked in, (B) has a stale safe row
        # (updated before alert), (C) has a stale not_responding row.
        a = _new_id("bcastA")
        b = _new_id("bcastB")
        c = _new_id("bcastC")

        _upsert_push_device(run_async, a)
        _upsert_push_device(run_async, b)
        _upsert_push_device(run_async, c)

        # (B) has a prior status
        _upsert_status(run_async, {
            "device_id": b, "status": "safe",
            "updated_at": _iso(_now() - timedelta(hours=6)),
        })
        # (C) had never used the app but existed
        _upsert_status(run_async, {
            "device_id": c, "status": "not_responding",
            "updated_at": _iso(_now() - timedelta(hours=6)),
        })
        # (A) has NO device_status row at all.

        try:
            t_before = _now()
            r = requests.post(
                f"{BASE}/api/trigger-alert",
                headers=HDR_ADMIN,
                json={
                    "confirmation_phrase": "SIREN",
                    "magnitude": 5.5,
                    "distance_km": 12,
                    "intensity": "V",
                },
                timeout=30,
            )
            assert r.status_code == 200, r.text
            t_after = _now()

            payload = _get_devices()
            by_id = {row["device_id"]: row for row in payload["devices"]}

            for did in (a, b, c):
                row = by_id.get(did)
                assert row is not None, (
                    f"device {did} missing from /api/devices after broadcast — "
                    "P0: silence must never be invisible"
                )
                assert row["map_color"] == "red", (
                    f"device {did}: expected red after broadcast, got {row['map_color']!r}"
                )
                assert row["silent_since_alert"] is True, (
                    f"device {did}: silent_since_alert should be True"
                )
                assert row["last_alerted_at"], (
                    f"device {did}: last_alerted_at should be set"
                )
                # ISO within 5 seconds of the broadcast window
                parsed = datetime.fromisoformat(
                    row["last_alerted_at"].replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                assert (t_before - timedelta(seconds=5)) <= parsed <= (
                    t_after + timedelta(seconds=5)
                ), f"last_alerted_at outside 5s of broadcast: {row['last_alerted_at']}"
        finally:
            _delete_device(run_async, a)
            _delete_device(run_async, b)
            _delete_device(run_async, c)


# ── Stand-down MUST NOT clear last_alerted_at ─────────────────────────
class TestStandDownDoesNotClearAlertedAt:
    """Paul: 'they stay on the board, red, until a human closes their case.'
    A stand-down that clears the flag is a P0 regression."""

    def test_stand_down_preserves_last_alerted_at(self, run_async):
        did = _new_id("sd")
        _upsert_push_device(run_async, did)

        # Insert a stub that will be a silent-since-alert AFTER the trigger.
        # We deliberately do not create a device_status row — the trigger
        # will upsert one with $setOnInsert.

        try:
            # Trigger alert
            r = requests.post(
                f"{BASE}/api/trigger-alert",
                headers=HDR_ADMIN,
                json={"confirmation_phrase": "SIREN", "magnitude": 5.0},
                timeout=30,
            )
            assert r.status_code == 200, r.text

            row1 = _find_device(_get_devices(), did)
            assert row1 is not None, "device disappeared after trigger"
            assert row1["last_alerted_at"], "last_alerted_at not set"
            assert row1["map_color"] == "red"
            assert row1["silent_since_alert"] is True
            first_alerted_at = row1["last_alerted_at"]

            # Stand down
            r2 = requests.post(
                f"{BASE}/api/admin/alert/stand-down",
                headers=HDR_ADMIN,
                json={"confirmation_phrase": "STANDDOWN",
                      "reason": "false_alarm"},
                timeout=30,
            )
            assert r2.status_code == 200, r2.text

            row2 = _find_device(_get_devices(), did)
            assert row2 is not None, (
                "device disappeared from /api/devices after stand-down — "
                "P0 regression: silence-since-alert people must stay on the board"
            )
            assert row2["last_alerted_at"] == first_alerted_at, (
                "P0 REGRESSION: stand-down CLEARED last_alerted_at. "
                "Paul: 'stand-down MUST NOT clear last_alerted_at — they stay "
                "on the board, red, until a human closes their case.'"
            )
            assert row2["map_color"] == "red", (
                f"map_color regressed after stand-down: got {row2['map_color']!r}"
            )
            assert row2["silent_since_alert"] is True
        finally:
            _delete_device(run_async, did)


# ── Silent-since-alert cleared ONLY by a fresh POST /status ────────────
class TestFreshStatusClearsSilentSinceAlert:
    """After the phone answers post-alert (updated_at > last_alerted_at),
    silent-since-alert flips off and their answer wins."""

    def test_post_status_after_alert_wins(self, run_async):
        did = _new_id("clear")
        _upsert_push_device(run_async, did)
        alerted = _now() - timedelta(minutes=1)
        # Pre-plant a silent-since-alert row.
        _upsert_status(run_async, {
            "device_id": did, "status": "safe",
            "updated_at": _iso(_now() - timedelta(hours=6)),
            "last_alerted_at": _iso(alerted),
        })
        try:
            # Sanity: currently silent-since-alert.
            row = _find_device(_get_devices(), did)
            assert row["map_color"] == "red"
            assert row["silent_since_alert"] is True

            # Phone reports safe now.
            r = requests.post(
                f"{BASE}/api/status",
                headers={"Content-Type": "application/json"},
                json={"deviceId": did, "status": "safe"},
                timeout=15,
            )
            assert r.status_code == 200, r.text

            row2 = _find_device(_get_devices(), did)
            assert row2["silent_since_alert"] is False, (
                "post-alert status did not clear silent-since-alert"
            )
            assert row2["map_color"] == "green", (
                f"map_color should be green after fresh safe check-in, "
                f"got {row2['map_color']!r}"
            )
        finally:
            _delete_device(run_async, did)


# ── Bulk write at scale ────────────────────────────────────────────────
class TestBulkWriteScale:
    """~50-device broadcast: every recipient's row must get stamped and
    stubs upserted for phones with no prior device_status."""

    def test_fifty_devices_all_stamped(self, run_async, stand_down_after):
        n = 50
        ids = [_new_id(f"bulk{i}") for i in range(n)]
        for did in ids:
            _upsert_push_device(run_async, did)
        try:
            r = requests.post(
                f"{BASE}/api/trigger-alert",
                headers=HDR_ADMIN,
                json={"confirmation_phrase": "SIREN", "magnitude": 5.5},
                timeout=30,
            )
            assert r.status_code == 200, r.text

            payload = _get_devices()
            by_id = {row["device_id"]: row for row in payload["devices"]}
            missing = [did for did in ids if did not in by_id]
            assert not missing, (
                f"{len(missing)} of {n} recipients missing from /api/devices "
                f"after bulk broadcast — first few: {missing[:5]}"
            )
            not_red = [did for did in ids
                       if by_id[did]["map_color"] != "red"]
            assert not not_red, (
                f"{len(not_red)} of {n} not red after broadcast: "
                f"first colors {[by_id[d]['map_color'] for d in not_red[:5]]}"
            )
        finally:
            for did in ids:
                _delete_device(run_async, did)


# ── Regression: /api/public/summary counts do NOT depend on alert ─────
class TestPublicSummaryUnchangedByAlert:
    """last_alerted_at/map_color must not sneak into count fields.
    #185 anti-double-count contract also asserted."""

    def test_summary_shape_and_ignores_last_alerted_at(self, run_async):
        r = requests.get(f"{BASE}/api/public/summary", timeout=15)
        assert r.status_code == 200
        js = r.json()
        assert "counts" in js
        counts = js["counts"]
        # No new count fields introduced by #326.
        for key in ("map_color", "last_alerted_at", "silent_since_alert"):
            assert key not in counts, (
                f"unexpected {key} in /api/public/summary counts — "
                "#326 must not add count fields"
            )
        # #185 spot: silent-since-alert person must NOT change safe count.
        did = _new_id("summarycheck")
        _upsert_push_device(run_async, did)
        _upsert_status(run_async, {
            "device_id": did, "status": "safe",
            "updated_at": _iso(_now() - timedelta(hours=6)),
        })
        try:
            base = requests.get(f"{BASE}/api/public/summary", timeout=15).json()["counts"]
            # Now flip to silent-since-alert.
            _upsert_status(run_async, {
                "device_id": did, "status": "safe",
                "updated_at": _iso(_now() - timedelta(hours=6)),
                "last_alerted_at": _iso(_now()),
            })
            after = requests.get(f"{BASE}/api/public/summary", timeout=15).json()["counts"]
            # last_alerted_at is decoration; the raw status is still safe.
            # So the safe/trapped/not_responding buckets should be
            # identical before and after — the count classifier reads
            # `status`, not `map_color`.
            for k in ("safe", "trapped", "not_responding", "rescued"):
                assert base.get(k) == after.get(k), (
                    f"count {k} changed by stamping last_alerted_at: "
                    f"{base.get(k)} → {after.get(k)}"
                )
        finally:
            _delete_device(run_async, did)


# ── Regression #185 group_size still exposed ──────────────────────────
class TestGroupSizeStillExposed185:
    def test_group_size_survives_on_devices(self, run_async):
        did = _new_id("gs")
        r = requests.post(f"{BASE}/api/status", json={
            "deviceId": did, "status": "safe", "group_size": "3",
        }, timeout=15)
        assert r.status_code == 200
        try:
            row = _find_device(_get_devices(), did)
            assert row is not None
            assert row.get("group_size") == "3"
        finally:
            _delete_device(run_async, did)


# ── Regression #193 offline queue: POST /status still 200 ─────────────
class TestOfflineQueueStill200_193:
    def test_wellformed_status_post_still_200(self, run_async):
        did = _new_id("offlineq")
        r = requests.post(f"{BASE}/api/status", json={
            "deviceId": did, "status": "safe",
        }, timeout=15)
        assert r.status_code == 200, r.text
        js = r.json()
        assert js.get("status") == "ok"
        assert js.get("device_id") == did
        _delete_device(run_async, did)
