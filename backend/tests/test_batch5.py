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
from datetime import datetime, timezone

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
        """A notice about Sicily must be unmistakably about Sicily.

        Also (Batch 7 D, #246): the body explains WHY the notice was
        sent — the user's own saved-places list — and points at the
        Settings › Places screen so they can turn it off from context.
        The two rules together mean nobody is left wondering "why did
        this app just alert me about Sicily?".
        """
        src = open("/app/backend/emsc/preview.py").read()
        fn = src.split("async def dispatch_place_notices")[1]
        assert "Seismic activity near {name}" in fn
        # #246: the body must name the reason (user's saved places)
        # and the switch-off path (Settings › Places).
        assert "saved places" in fn, "body must state the reason for the notice"
        assert "Settings" in fn and "Places" in fn, (
            "body must point at Settings › Places for the opt-out"
        )

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
        # B3 (batch 6): the clause lives in the TITLE, not the subtitle — the
        # title is the only line a scanning reader reads, so the option that
        # generates the most notifications must not look like the quiet one.
        assert 'title: "Everything nearby — including tremors too small to feel"' in s
        assert "Includes tremors you will not feel at all" in s
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
        # Batch 7 has bumped the version many times; this test only
        # guarantees the version has not REGRESSED below 1.0.32
        # (the #199 clear-on-stand-down cut). Each new fix bumps the
        # patch; asserting >= keeps the test durable.
        parts = tuple(int(x) for x in cfg["expo"]["version"].split("."))
        assert parts >= (1, 0, 32), (
            f"version must be at or above 1.0.32 (found {cfg['expo']['version']})"
        )
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
        # Moved to routes_diagnostics.py in the 2026-06-18 module split.
        src = open("/app/backend/routes_diagnostics.py").read()
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
        # #208 R4 (Batch 7): the critical-alert branch broadened to
        # accept ANY earthquake-alert-shaped payload (kind, action_url,
        # or magnitude). The invariant this test locks — siren=1 on a
        # real critical alert — must still hold after that broadening.
        crit = layout.split('if (looksLikeAlert) {')[1].split("return;")[0]
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


class TestAftershockDoesNotDestroyAnswers:
    """Paul, 2026-08-17: aftershocks arrive minutes after a main shock. If a
    second alert re-navigates someone who is mid-answer, they lose it — and
    a trapped person must not have to report themselves twice because the
    ground shook again."""

    def test_layout_publishes_instead_of_navigating_when_screen_is_open(self):
        layout = open("/app/frontend/app/_layout.tsx").read()
        assert "isAlertScreenMounted()" in layout
        assert "publishAlert(data);" in layout
        crit = layout.split("addNotificationReceivedListener")[1].split("getLastNotificationResponseAsync")[0]
        # navigation only in the else branch — never while the screen is open
        assert "if (isAlertScreenMounted()) {" in crit
        assert crit.index("publishAlert(data);") < crit.index("handleTap(")

    def test_alert_screen_registers_mounted_state(self):
        alert = open("/app/frontend/app/alert.tsx").read()
        assert "setAlertScreenMounted(true)" in alert
        assert "return () => setAlertScreenMounted(false);" in alert

    def test_aftershock_touches_no_answer_state(self):
        """The subscriber may set the notice and NOTHING else — no setStatus,
        no clearing of severity/mobility, no closing of the sheets.
        (Signature updated in Batch 7 R4 when the stand-down branch was
        added to the same subscriber — the invariant still holds for
        the aftershock branch.)"""
        alert = open("/app/frontend/app/alert.tsx").read()
        # Slice the callback body: everything between `(event: any) => {`
        # and the callback's own closing `    });` (four-space indent —
        # the useEffect's `}, []);` is two-space, so the two are
        # unambiguous).
        body = alert.split("return subscribeToAlerts((event: any) => {", 1)[1] \
                    .split("\n    });\n", 1)[0]
        assert "setAftershock(event);" in body
        # The aftershock branch = everything AFTER the stand-down early
        # return. Locking the invariant on that specific branch, so the
        # stand-down branch is free to call sirenPlayer.stop() etc.
        aftershock_branch = body.split("setAftershock(event);", 1)[1]
        for forbidden in ("setStatus(", "setTriageOpen(", "setMobilityOpen(",
                          "setChosenSeverity(", "setChosenMobility(",
                          "submitCheckIn("):
            assert forbidden not in aftershock_branch, forbidden

    def test_aftershock_never_resurrects_a_silenced_siren(self):
        """#31/#50 shape: a siren that comes back after the user silenced
        it teaches them the button doesn't work. Locked on the AFTERSHOCK
        branch specifically; the stand-down branch is allowed to CALL
        sirenPlayer.stop() (killing the siren, not resurrecting it)."""
        alert = open("/app/frontend/app/alert.tsx").read()
        body = alert.split("return subscribeToAlerts((event: any) => {", 1)[1] \
                    .split("\n    });\n", 1)[0]
        aftershock_branch = body.split("setAftershock(event);", 1)[1]
        assert "sirenPlayer.play()" not in aftershock_branch
        assert "shouldPlayRef.current = true" not in aftershock_branch

    def test_already_answered_users_are_not_silently_reset(self):
        """An explicit Update button, never an automatic reset — the rescue
        list must match what the person actually said."""
        alert = open("/app/frontend/app/alert.tsx").read()
        assert 'testID="aftershock-update-btn"' in alert
        assert 'status === "sent"' in alert.split('testID="aftershock-bar"')[1][:1200]

    def test_rehearsal_uses_the_real_bus(self):
        """1.0.28: Paul asked to see the mid-answer case on his own phone.
        Home's Trigger Test Alert cannot show it (it navigates locally, and
        once you're on the screen the button is behind you), so Diagnostics
        opens the alert screen with `rehearse=aftershock` and the screen
        publishes a second event through the SAME bus a real push uses. If
        this ever became a bespoke code path it would stop being evidence."""
        alert = open("/app/frontend/app/alert.tsx").read()
        diag = open("/app/frontend/app/diag.tsx").read()
        assert 'params.rehearse === "aftershock"' in alert
        assert "publishAlert({" in alert
        assert "rehearse=aftershock" in diag
        assert 'testID="diag-aftershock-rehearsal"' in diag
        # The rehearsal must not fake the notice directly — it goes through
        # publishAlert so the real listener does the work.
        block = alert.split('const isRehearsal = params.rehearse')[1].split("}, [isRehearsal]);")[0]
        assert "setAftershock(" not in block

    def test_rehearsal_states_what_it_cannot_test(self):
        """A rehearsal that quietly skips a step is worse than no rehearsal:
        the screen must say APNs delivery is not exercised."""
        diag = open("/app/frontend/app/diag.tsx").read()
        section = diag.split('<Section title="Aftershock rehearsal">')[1][:1600]
        assert "delivery of the second" in section
        assert "12 seconds" in section


class TestBuildIdentification:
    """After a 3-week-old build went unnoticed, the app must be able to tell
    you what it is. Diagnostics reads version/build from the INSTALLED
    BINARY (expo-application), and carries a hard-coded fixes marker whose
    absence is itself the answer on an older build."""

    def test_diag_reads_version_from_the_binary(self):
        push = open("/app/frontend/src/utils/push.ts").read()
        assert 'import * as Application from "expo-application"' in push
        assert "Application.nativeBuildVersion" in push
        assert "Application.nativeApplicationVersion" in push

    def test_diag_carries_a_hardcoded_fix_marker(self):
        diag = open("/app/frontend/app/diag.tsx").read()
        # The marker DESCRIPTION stays hard-coded so a build shipping
        # without the fix gets caught (Paul, 2026-08-18). The leading
        # version number is now single-source (#251, Batch 7 R4) — read
        # from `info.app_version` — so all three version rows on the
        # Diagnostics card are guaranteed to agree.
        assert "#208 R4 primary alert" in diag
        # Batch 7 D (#252): the label was renamed from "fixes in this
        # build" to "What's fixed in it" when the Diagnostics screen
        # was rewritten human-first — either wording carries the
        # marker's job (a hard-coded string that must be updated per
        # build so a stale IPA is caught).
        assert (
            'label="fixes in this build"' in diag
            or 'label="What\'s fixed in it"' in diag
        ), "diag screen must carry a hard-coded fixes-in-this-build label"


# ── A1 (batch 6): the chart counted events while the table counted people ─
class TestReportChartCountsPeople:
    """One device toggling its status three times drew a red bar of 3 beside
    a table saying "Total devices reporting: 1". Every figure was correct;
    the document still contradicted itself in plain language. On the public
    report that reads as three people trapped to a journalist."""

    def _rows(self):
        # ONE device, many events inside a single hour, plus a second device
        # that only ever checked in safe.
        base = "2026-08-18T09:%02d:00+00:00"
        return [
            {"device_id": "dev-A", "status": "trapped", "recorded_at": base % 1},
            {"device_id": "dev-A", "status": "safe", "recorded_at": base % 5},
            {"device_id": "dev-A", "status": "trapped", "recorded_at": base % 9},
            {"device_id": "dev-A", "status": "safe", "recorded_at": base % 20},
            {"device_id": "dev-A", "status": "trapped", "recorded_at": base % 30},
            {"device_id": "dev-B", "status": "safe", "recorded_at": base % 40},
        ]

    def test_one_person_many_toggles_counts_as_one(self):
        """A1 reopened 2026-06-18: de-duplicating within a bucket was not
        enough, because someone still trapped an hour later reports again and
        the C1 ladder makes that routine. Each person now counts ONCE PER
        STATUS for the whole window, in the period they first reported it — so
        the red bars add up to the narrative's trapped figure by construction."""
        from reports_export import _bucket_timeline
        since = datetime(2026, 8, 18, 8, 0, tzinfo=timezone.utc)
        until = datetime(2026, 8, 18, 11, 0, tzinfo=timezone.utc)
        buckets, hourly = _bucket_timeline(self._rows(), since, until)
        assert hourly is True
        assert sum(b["trapped"] for b in buckets) == 1, "chart still counting events"
        # dev-A checked in safe as well, which is a real event the chart must
        # show — but only once, alongside dev-B.
        assert sum(b["safe"] for b in buckets) == 2

    def test_bucket_total_never_exceeds_distinct_people(self):
        """The invariant that makes chart/table/narrative agreement
        structural rather than a coincidence: no SERIES can exceed the number
        of distinct people."""
        from reports_export import _bucket_timeline
        rows = self._rows()
        since = datetime(2026, 8, 18, 8, 0, tzinfo=timezone.utc)
        until = datetime(2026, 8, 18, 11, 0, tzinfo=timezone.utc)
        buckets, _ = _bucket_timeline(rows, since, until)
        distinct_people = len({r["device_id"] for r in rows})
        for series in ("trapped", "safe", "rescued"):
            assert sum(b[series] for b in buckets) <= distinct_people

    def test_reverted_rescues_are_still_excluded(self):
        from reports_export import _bucket_timeline
        rows = [{"device_id": "dev-C", "status": "rescued", "rescue_reverted": True,
                 "recorded_at": "2026-08-18T09:10:00+00:00"}]
        buckets, _ = _bucket_timeline(
            rows,
            datetime(2026, 8, 18, 8, 0, tzinfo=timezone.utc),
            datetime(2026, 8, 18, 11, 0, tzinfo=timezone.utc),
        )
        assert max(b["rescued"] for b in buckets) == 0

    def test_y_axis_is_labelled_in_words(self):
        src = open("/app/backend/reports_export.py").read()
        fn = src.split("def _timeline_chart(")[1].split("\ndef ")[0]
        assert 'String(9, height_pt / 2 - 22, "People")' in fn

    def test_both_reports_use_the_same_bucketing_function(self):
        """The public report must never get an event-based chart even if the
        team report were ever changed."""
        src = open("/app/backend/reports_export.py").read()
        assert src.count("_bucket_timeline(") >= 3   # 1 def + >=2 call sites


# ── A0 (batch 6): revisions and stale events in tremor notices ───────────
class TestPreviewRevisionsAndFreshness:
    """Two notices minutes apart — "M3.3, 249km" then "M3.7, 251km" — were
    ONE earthquake at two revision stages. A user reads that as two
    earthquakes. And neither had an age check, so a config change could
    announce a 3-hour-old tremor as if it had just happened."""

    def test_revision_without_material_change_is_suppressed(self):
        src = open("/app/backend/emsc/preview.py").read()
        fn = src.split("async def dispatch_preview_if_needed")[1]
        assert '"skipped_reason": "revision_no_material_change"' in fn
        assert "abs(float(new_magnitude) - float(prior_magnitude)) >= 0.3" in fn

    def test_material_revision_is_labelled_as_an_update(self):
        src = open("/app/backend/emsc/preview.py").read()
        fn = src.split("async def dispatch_preview_if_needed")[1]
        assert '"PREVIEW · Updated seismic reading" if is_update' in fn
        assert "Updated: now measured at M" in fn
        assert "Same earthquake, " in fn
        assert "first reported M" in fn

    def test_delivered_rows_record_magnitude_for_comparison(self):
        """Without this the update/suppress decision has nothing to compare
        against and every revision would look new again."""
        src = open("/app/backend/emsc/preview.py").read()
        fn = src.split("async def dispatch_preview_if_needed")[1]
        delivered_block = fn.split('"idempotency_key": idem')[0]
        assert '"magnitude": emsc_event.get("magnitude")' in delivered_block

    def test_stale_events_are_not_announced_as_current(self):
        src = open("/app/backend/emsc/preview.py").read()
        fn = src.split("async def dispatch_preview_if_needed")[1]
        assert "max_event_age_minutes" in fn
        assert "event_too_old" in fn
        # default must be tight enough that a 3-hour-old quake is blocked
        assert 'or 90)' in fn


# ── B2/#173 (batch 6): closing an event detail must return to its origin ──
class TestEventDetailNavigation:
    """`router.replace("/")` tore down the stack and dumped the user on Home,
    so browsing a second event meant re-entering the map, re-picking the
    time window and re-panning. That kills the retention feature (#107)."""

    def test_detail_pops_the_stack_instead_of_resetting_it(self):
        s = open("/app/frontend/app/quake/[unid].tsx").read()
        assert "if (router.canGoBack()) router.back();" in s
        # replace("/") survives ONLY as the cold-start fallback (the other
        # occurrence is the comment explaining why it was removed)
        import re as _re
        code = _re.sub(r"\{/\*.*?\*/\}", "", s, flags=_re.S)
        assert code.count('router.replace("/")') == 1, code.count('router.replace("/")')
        assert "else router.replace(\"/\");" in s

    def test_back_control_is_labelled_with_its_origin(self):
        s = open("/app/frontend/app/quake/[unid].tsx").read()
        assert 'params.from === "map" ? "Map"' in s
        assert 'name={router.canGoBack() ? "chevron-back" : "close"}' in s

    def test_map_tags_its_origin_on_push(self):
        s = open("/app/frontend/app/map.tsx").read()
        assert 'from: "map",' in s

    def test_detail_can_open_the_map_on_that_event(self):
        s = open("/app/frontend/app/quake/[unid].tsx").read()
        assert 'testID="see-on-map-btn"' in s
        assert "focus_lat: String(lat)" in s
        assert "focus_unid" in s
        # pushed (not replaced) so backing out returns to the detail — the
        # notification → detail → map → detail loop must always unwind
        openmap = s.split("const openOnMap = () => {")[1].split("};")[0]
        assert "router.push(" in openmap and "replace" not in openmap

    def test_map_honours_focus_and_highlight(self):
        m = open("/app/frontend/app/map.tsx").read()
        assert "focus={focus}" in m
        assert "highlightExternalId={focusParams.focus_unid ?? null}" in m
        canvas = open("/app/frontend/src/components/MapCanvas.native.tsx").read()
        assert "initialRegion={initialRegion}" in canvas
        assert "styles.markerHighlighted" in canvas

    def test_places_path_has_the_same_freshness_gate(self):
        """Flagged by the test agent: a stale event must not announce a
        hours-old tremor near someone's family as if it just happened."""
        src = open("/app/backend/emsc/preview.py").read()
        fn = src.split("async def dispatch_place_notices")[1]
        assert "max_event_age_minutes" in fn
        assert "max_age_minutes" in fn
