"""Batch 5 (2026-08-17) tests — backend side.

  B1  operator kill switch for check-in reminders:
        POST /api/admin/reminders/cancel is auth-gated, targets registered
        iOS devices, and sends a SILENT push (content-available only, no
        alert/sound/badge, apns-push-type=background) so stopping a false
        alarm never costs the user another loud notification.
  B8  "places I care about": CRUD + cap + whole-feature switch, and the
        HARD SAFETY CONSTRAINT that the critical-alert path never reads
        user_places (structural, verified by source inspection + by the
        critical payload being unchanged by any place setting).
  B9  notification actions category on INFORMATIONAL notices only — the
        critical payload must never carry a category.
"""
import os
import re
import uuid

import pytest
import requests
from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv("/app/backend/.env")
load_dotenv("/app/frontend/.env")

BASE_URL = (
    os.environ.get("EXPO_PUBLIC_BACKEND_URL")
    or os.environ.get("EXPO_BACKEND_URL")
    or "http://localhost:8001"
).rstrip("/")
ADMIN_TOKEN = os.environ.get("ADMIN_TRIGGER_PASSWORD")
MONGO_URL = os.environ.get("MONGO_URL") or "mongodb://localhost:27017"
DB_NAME = os.environ.get("DB_NAME") or "test_database"
HEADERS = {"X-Admin-Token": ADMIN_TOKEN}

CANCEL_URL = f"{BASE_URL}/api/admin/reminders/cancel"


@pytest.fixture(scope="module")
def db():
    client = MongoClient(MONGO_URL)
    yield client[DB_NAME]
    client.close()


@pytest.fixture
def device_id(db):
    did = f"qg-batch5-{uuid.uuid4().hex[:8]}"
    yield did
    db.user_places.delete_many({"device_id": did})
    db.push_devices.delete_many({"user_id": did})


# ── B1: reminder kill switch ─────────────────────────────────────────────
class TestB1ReminderKillSwitch:
    def test_requires_auth(self):
        r = requests.post(CANCEL_URL, timeout=30)
        assert r.status_code in (401, 403), r.text

    def test_cancel_returns_target_count_and_is_silent(self, db):
        r = requests.post(CANCEL_URL, headers=HEADERS, timeout=60)
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["ok"] is True
        assert data["silent"] is True
        assert isinstance(data["targeted"], int)
        # Audited so a false-alarm recovery is reconstructable afterwards.
        row = db.emsc_audit_log.find_one(
            {"event_type": "reminders_cancelled",
             "context.idempotency_key": data["idempotency_key"]},
        )
        assert row is not None

    def test_payload_carries_no_alert_no_sound(self):
        """The whole point: cancelling noise must not make noise."""
        from apns import _build_critical_payload  # noqa: F401  (import sanity)
        src = open("/app/backend/apns.py").read()
        fn = src.split("async def send_silent_cancel_reminders")[1]
        fn = fn.split("\n\n\n")[0]
        assert '"aps": {"content-available": 1}' in fn
        assert '"alert"' not in fn
        assert '"sound"' not in fn
        assert 'push_type="background"' in fn
        assert 'apns_priority="5"' in fn
        assert '"kind": "cancel_reminders"' in fn


# ── B9: notification actions on informational notices only ───────────────
class TestB9TremorActionsCategory:
    def test_preview_payload_has_tremor_category(self):
        from apns import _build_preview_payload, TREMOR_CATEGORY_ID
        p = _build_preview_payload("t", "b", "/quake/x", magnitude=3.6, distance_km=210)
        assert p["aps"]["category"] == TREMOR_CATEGORY_ID
        assert p["kind"] == "emsc_preview"

    def test_critical_payload_has_no_category(self):
        """Non-negotiable: nothing may compete with I'M SAFE / I'M TRAPPED."""
        from apns import _build_critical_payload
        p = _build_critical_payload("t", "b", "/alert", magnitude=6.4)
        assert "category" not in p["aps"]
        assert p["aps"]["sound"]["critical"] == 1
        assert p["kind"] == "critical_alert"

    def test_preview_body_includes_distance(self):
        """B3: distance belongs in the notification, not only on the screen."""
        from emsc.preview import format_body
        body = format_body(
            magnitude=3.6, distance_km=210.4, depth_km=11,
            bearing_from_country=120.0, country_name="Malta",
        )
        assert "210km" in body
        assert "M3.6" in body


# ── B8: places I care about ──────────────────────────────────────────────
class TestB8Places:
    def _url(self, did):
        return f"{BASE_URL}/api/devices/{did}/places"

    def test_defaults_empty_and_enabled(self, device_id):
        r = requests.get(self._url(device_id), timeout=30)
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["places"] == []
        assert data["enabled"] is True
        assert data["max_places"] >= 1

    def test_add_list_remove(self, device_id):
        r = requests.post(
            self._url(device_id),
            json={"name": "Mum's house", "latitude": 37.5, "longitude": 15.09},
            timeout=30,
        )
        assert r.status_code == 200, r.text
        pid = r.json()["place"]["place_id"]

        listed = requests.get(self._url(device_id), timeout=30).json()["places"]
        assert [p["name"] for p in listed] == ["Mum's house"]

        d = requests.delete(f"{self._url(device_id)}/{pid}", timeout=30)
        assert d.status_code == 200, d.text
        assert requests.get(self._url(device_id), timeout=30).json()["places"] == []

    def test_cap_enforced(self, device_id):
        max_places = requests.get(self._url(device_id), timeout=30).json()["max_places"]
        for i in range(max_places):
            r = requests.post(
                self._url(device_id),
                json={"name": f"P{i}", "latitude": 36.0 + i / 10, "longitude": 14.4},
                timeout=30,
            )
            assert r.status_code == 200, r.text
        over = requests.post(
            self._url(device_id),
            json={"name": "one too many", "latitude": 36.9, "longitude": 14.4},
            timeout=30,
        )
        assert over.status_code == 400
        assert str(max_places) in over.json()["detail"]

    def test_whole_feature_switch(self, device_id):
        r = requests.post(
            f"{self._url(device_id)}/enabled", json={"enabled": False}, timeout=30,
        )
        assert r.status_code == 200, r.text
        assert requests.get(self._url(device_id), timeout=30).json()["enabled"] is False

    def test_place_notices_never_touch_the_critical_path(self):
        """HARD CONSTRAINT, checked structurally.

        `user_places` must be read ONLY by the informational preview
        module. If it ever appears in the critical send path (apns.py's
        critical builder / send_critical_alerts, or the evaluator's
        critical branch), a place setting could influence someone's own
        emergency alert — which is exactly what must be impossible.
        """
        assert "user_places" not in open("/app/backend/apns.py").read()
        assert "user_places" not in open("/app/backend/emsc/evaluator.py").read()
        assert "user_places" in open("/app/backend/emsc/preview.py").read()

        server_src = open("/app/backend/server.py").read()
        trigger = server_src.split('@api_router.post("/trigger-alert")')[1]
        trigger = trigger.split("@api_router.")[0]
        assert "user_places" not in trigger
        assert "places_enabled" not in trigger

    def test_place_notice_body_names_the_place(self):
        """A notice about Sicily must be unmistakably about Sicily."""
        src = open("/app/backend/emsc/preview.py").read()
        fn = src.split("async def dispatch_place_notices")[1]
        assert "Seismic activity near {name}" in fn
        assert "not your own location" in fn

    def test_places_use_intensity_not_raw_radius(self):
        src = open("/app/backend/emsc/preview.py").read()
        fn = src.split("async def dispatch_place_notices")[1]
        assert "mmi_from_faenza_michelini_2010" in fn
        assert "preset_would_fire" in fn


# ── App-side invariants that are cheap to assert from here ───────────────
class TestAppSideInvariants:
    def test_no_waking_claim_in_user_facing_copy(self):
        """B5: we cannot promise to wake anyone — phone off, no signal, in
        another room, or the alert routed to a Watch as a quiet tap."""
        offenders = []
        for root, _dirs, files in os.walk("/app/frontend/app"):
            for f in files:
                if not f.endswith((".tsx", ".ts")):
                    continue
                p = os.path.join(root, f)
                text = open(p).read()
                for m in re.finditer(r"\b(woken|wake you|wake the user|wake your phone)\b", text):
                    offenders.append(f"{p}: {m.group(0)}")
        for root, _dirs, files in os.walk("/app/frontend/src"):
            for f in files:
                if not f.endswith((".tsx", ".ts")):
                    continue
                p = os.path.join(root, f)
                text = open(p).read()
                for m in re.finditer(r"\b(woken|wake you|wake the user|wake your phone)\b", text):
                    offenders.append(f"{p}: {m.group(0)}")
        assert offenders == [], offenders

    def test_test_trigger_schedules_no_reminders(self):
        """B1: a practice run must not nag 8 times."""
        home = open("/app/frontend/app/index.tsx").read()
        assert "scheduleCheckInReminders" not in home
        alert = open("/app/frontend/app/alert.tsx").read()
        assert "scheduleCheckInReminders" in alert
        # armed only on the real-alert path
        assert "if (!shouldPlaySiren) return;" in alert

    def test_answering_cancels_reminders_immediately(self):
        """B1: cancellation happens synchronously in submitCheckIn, before
        GPS / battery / network — both I'M SAFE and the triage path."""
        alert = open("/app/frontend/app/alert.tsx").read()
        submit = alert.split("const submitCheckIn = async")[1].split("// Gather location")[0]
        assert "cancelCheckInReminders()" in submit
        assert "stopSiren();" in submit
        assert 'const handleImSafe = () => submitCheckIn("safe");' in alert

    def test_dismiss_alert_button_removed(self):
        alert = open("/app/frontend/app/alert.tsx").read()
        # no renderable "Dismiss alert" label left (the words survive only in
        # the comment explaining why the button was removed)
        assert ">\n              Dismiss alert" not in alert
        assert '"Dismiss alert"' not in alert.split("*/")[1]
        assert "alert-dismiss-btn" not in alert
        assert "alert-back-home-btn" in alert

    def test_three_notification_options_and_imperceptible_wording(self):
        s = open("/app/frontend/app/settings/notifications.tsx").read()
        opts = re.findall(r'value: "(off|significant|noticeable|everything)"', s)
        assert opts == ["off", "noticeable", "everything"], opts
        assert "Including tremors too small to feel" in s
        assert "including ones you will not feel at all" in s
        # both protective statements survive
        assert "always on and cannot be switched off" in s
        assert "does not affect emergency alerts" in s

    def test_quake_detail_sentence_never_renders_a_gap(self):
        s = open("/app/frontend/app/quake/[unid].tsx").read()
        assert "The epicentre is where the earthquake started underground." in s
        assert "from ${distance.from}" in s
        assert "MALTA_CENTER" in s

    def test_app_version_bumped(self):
        import json
        cfg = json.load(open("/app/frontend/app.json"))
        assert cfg["expo"]["version"] == "1.0.25"
        info = cfg["expo"]["ios"]["infoPlist"]
        # export-compliance answer baked in so App Store Connect stops asking
        assert info["ITSAppUsesNonExemptEncryption"] is False
        # required for the silent reminder-kill push to reach a backgrounded app
        assert "remote-notification" in info["UIBackgroundModes"]
        assert info["CFBundleDisplayName"] == "Quake Angel"


# ── #146: old test entries in the trapped list ───────────────────────────
class TestIssue146TestEntries:
    """Stale test check-ins used to sit in the live trapped list looking
    exactly like real casualties. Two-pronged fix: server-computed
    `is_test` on every row (hidden by default in the dashboard, never
    deleted behind anyone's back) + an operator tag for test check-ins
    made from a real phone, which no id pattern can catch."""

    def test_devices_returns_is_test_and_test_count(self):
        r = requests.get(f"{BASE_URL}/api/devices?limit=200", headers=HEADERS, timeout=60)
        assert r.status_code == 200, r.text
        data = r.json()
        assert "test_count" in data
        for d in data["devices"]:
            assert "is_test" in d

    def test_harness_ids_flagged_real_app_ids_not(self):
        from server import _is_test_device
        # real app ids: qg-<epoch>-<random>. Must NEVER be auto-hidden,
        # whatever the random suffix happens to spell.
        assert _is_test_device({"device_id": "qg-1786974119317-ycwe97uk"}) is False
        assert _is_test_device({"device_id": "qg-1785487967868-testabcd"}) is False
        # harness / synthetic ids
        for did in (
            "qg-snippet-test-1785838976", "qg-rescue-e2e-1785838809",
            "qg-mob-safe-1785479931", "qg-loadtest-abc123-000001",
            "TEST_dashboard_1", "diag-1234", "dashboard",
        ):
            assert _is_test_device({"device_id": did}) is True, did
        # explicit operator flag beats everything
        assert _is_test_device({"device_id": "qg-1786974119317-ycwe97uk", "synthetic": True}) is True

    def test_mark_test_round_trip_is_audited(self, db):
        did = f"qg-{1786974119317}-marktest"
        db.device_status.insert_one({
            "device_id": did, "status": "trapped", "severity": "red",
            "updated_at": "2026-08-17T10:00:00+00:00",
        })
        try:
            url = f"{BASE_URL}/api/admin/devices/{did}/mark-test"
            r = requests.post(url, headers=HEADERS, json={"is_test": True}, timeout=30)
            assert r.status_code == 200, r.text
            assert db.device_status.find_one({"device_id": did})["synthetic"] is True
            assert db.emsc_audit_log.find_one(
                {"event_type": "device_marked_test", "device_id": did},
            ) is not None

            # reversible
            r2 = requests.post(url, headers=HEADERS, json={"is_test": False}, timeout=30)
            assert r2.status_code == 200, r2.text
            assert db.device_status.find_one({"device_id": did})["synthetic"] is False
            assert db.emsc_audit_log.find_one(
                {"event_type": "device_unmarked_test", "device_id": did},
            ) is not None
        finally:
            db.device_status.delete_many({"device_id": did})
            db.emsc_audit_log.delete_many({"device_id": did})

    def test_mark_test_requires_auth_and_404s_unknown(self):
        r = requests.post(f"{BASE_URL}/api/admin/devices/nope/mark-test",
                          json={"is_test": True}, timeout=30)
        assert r.status_code in (401, 403)
        r2 = requests.post(f"{BASE_URL}/api/admin/devices/no-such-device/mark-test",
                           headers=HEADERS, json={"is_test": True}, timeout=30)
        assert r2.status_code == 404

    def test_test_entries_preview_counts_all_three_collections(self):
        r = requests.get(f"{BASE_URL}/api/admin/test-entries", headers=HEADERS, timeout=60)
        assert r.status_code == 200, r.text
        data = r.json()
        for key in ("count", "device_status", "status_events", "push_devices", "devices"):
            assert key in data

    def test_purge_is_admin_only(self):
        """Operators may HIDE a row; only an admin may destroy one — these
        are legal records for real people."""
        r = requests.post(f"{BASE_URL}/api/admin/purge-test-entries", timeout=30)
        assert r.status_code in (401, 403)
        src = open("/app/backend/server.py").read()
        fn = src.split('@api_router.post("/admin/purge-test-entries")')[1].split("@api_router.")[0]
        assert 'require_role(principal, "admin")' in fn
        assert '"event_type": "test_entries_purged"' in fn


# ── #169: the siren did not play on a test alert (MOST SERIOUS TO DATE) ───
class TestIssue169SirenPlaysOnTest:
    """Root cause: Home's Trigger Test Alert navigated to a bare "/alert".

    Commit d3e8d81 (2026-08-06) made the siren opt-in via `?siren=1` to
    stop informational preview taps from detonating it
    (BUG-2026-08-06-preview-tap-siren). The test trigger never passed the
    param, so from that day the practice run showed the red screen in
    total silence — and no test covered the ENTRY PARAMS, only the
    playback code, so everything looked green.

    These tests pin the entry params for BOTH paths. That is the layer
    that broke; asserting playback internals again would not have caught
    it.
    """

    def test_home_test_trigger_passes_siren_and_test_params(self):
        home = open("/app/frontend/app/index.tsx").read()
        assert 'router.push("/alert?siren=1&test=1")' in home
        # the bare push is what caused #169 — it must not come back
        assert 'router.push("/alert")' not in home

    def test_real_critical_alert_tap_still_sets_siren(self):
        layout = open("/app/frontend/app/_layout.tsx").read()
        crit = layout.split('if (kind === "critical_alert") {')[1].split("return;")[0]
        assert 'params.set("siren", "1")' in crit

    def test_test_run_plays_siren_but_arms_no_reminders(self):
        alert = open("/app/frontend/app/alert.tsx").read()
        # siren gate depends ONLY on siren=1 …
        assert 'const shouldPlaySiren = params.siren === "1";' in alert
        assert 'const isTestRun = params.test === "1";' in alert
        # … and the reminder gate is the one that checks the test flag,
        # so the two decisions can never be conflated again.
        rem = alert.split("if (!shouldPlaySiren) return;")[1].split("}, [shouldPlaySiren")[0]
        assert "if (isTestRun) return;" in rem
        # playback itself is unchanged and still traced
        assert "sirenPlayer.play();" in alert
        assert "SIREN play() requested" in alert

    def test_silent_mode_override_still_in_place(self):
        """#13 regression guard: the siren must ignore the ringer switch."""
        alert = open("/app/frontend/app/alert.tsx").read()
        layout = open("/app/frontend/app/_layout.tsx").read()
        for src in (alert, layout):
            assert "playsInSilentMode: true" in src

    def test_answering_still_kills_the_siren(self):
        """#31 / #50 regression guard."""
        alert = open("/app/frontend/app/alert.tsx").read()
        assert "shouldPlayRef.current = false" in alert
        assert "SIREN KILL-SWITCH" in alert

    def test_locked_phone_path_uses_bundled_siren_sound(self):
        """If the user never opens the app, the SIREN IS THE PUSH SOUND —
        so the critical payload must name a real bundled file, never
        'default' (inconsistently honoured inside a critical dict)."""
        from apns import _build_critical_payload
        p = _build_critical_payload("t", "b", "/alert", magnitude=6.4)
        assert p["aps"]["sound"]["critical"] == 1
        assert p["aps"]["sound"]["name"] == "siren.caf"
        assert p["aps"]["sound"]["volume"] == 1.0
        assert os.path.exists("/app/frontend/assets/audio/siren.caf")


class TestIssue169FollowUps:
    """Two further defects found while tracing #169 end to end, both in the
    real-alert path rather than the test button."""

    def test_android_real_alert_carries_kind_critical_alert(self):
        """Android alerts had NO `kind`, and the app's fail-safe treats a
        missing kind as informational — so tapping a real earthquake alert
        opened the informational event screen, not the check-in screen, and
        never armed the siren."""
        src = open("/app/backend/server.py").read()
        block = src.split("# ---- Android: SuprSend relay")[1].split("ios_delivered")[0]
        assert '"kind": "critical_alert"' in block
        assert '"action_url": "/alert"' in block
        assert '"magnitude": body.magnitude' in block

    def test_foreground_critical_alert_routes_to_alert_screen(self):
        """If a real alert arrives with the app already open, the user must
        land on the check-in screen with the siren running — not be left on
        whatever screen they were on with a banner they might not tap."""
        layout = open("/app/frontend/app/_layout.tsx").read()
        recv = layout.split("addNotificationReceivedListener")[1].split("getLastNotificationResponseAsync")[0]
        assert 'handleTap({ ...data, kind: "critical_alert" });' in recv

    def test_foreground_notifications_are_audible(self):
        """iOS suppresses the push sound in the foreground unless the handler
        asks for it. shouldPlaySound MUST stay true or a foreground alert is
        silent again."""
        layout = open("/app/frontend/app/_layout.tsx").read()
        handler = layout.split("setNotificationHandler")[1].split("}")[0]
        assert "shouldPlaySound: true" in handler
