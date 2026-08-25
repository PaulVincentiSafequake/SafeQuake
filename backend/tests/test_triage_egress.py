"""Alert-triage wording and the green-only egress question (2026-06-18).

Two problems Paul reported, both small, both real:

1. The secondary button said "I'M TRAPPED / NEED HELP" but the very next screen
   offers "I can walk and I'm not badly hurt" as its first option. If you can
   walk you are not trapped, so the two screens contradicted each other. The
   button was doing double duty — worded as "I'm trapped", functioning as "I'm
   not safe". Reworded to "I NEED HELP": no extra screens, no extra taps.

2. Because the mobility follow-up is limited to yellow (#51), someone trapped
   but uninjured picked green and was never asked whether they were stuck. They
   surfaced as "minor, walking wounded" while physically unable to leave, and
   never appeared as an extraction case. Mobility is not egress: mobility
   describes the body, egress describes the building. Green now gets
   "Can you get out on your own?" — and only green: red already implies
   immobility, yellow keeps its mobility question.
"""
import os

import requests

BASE_URL = "http://localhost:8001"
ADMIN_TOKEN = os.environ["ADMIN_TRIGGER_PASSWORD"]
HEADERS = {"X-Admin-Token": ADMIN_TOKEN}
ALERT_TSX = "/app/frontend/app/alert.tsx"
DEVICE = "qg-1700000000099-egressft"


def _alert_src() -> str:
    with open(ALERT_TSX) as fh:
        return fh.read()


def _post(**over):
    body = {
        "deviceId": DEVICE,
        "status": "trapped",
        "severity": "green",
        "mobility": "mobile",
        "location": {"latitude": 35.9, "longitude": 14.5, "accuracy": 8},
        "battery": {"level": 0.9, "state": "unplugged"},
    }
    body.update(over)
    return requests.post(f"{BASE_URL}/api/status", json=body, timeout=30)


def _device_row():
    r = requests.get(f"{BASE_URL}/api/devices", headers=HEADERS, timeout=30)
    r.raise_for_status()
    return next(d for d in r.json()["devices"] if d["device_id"] == DEVICE)


class TestButtonWording:
    def test_button_no_longer_claims_the_user_is_trapped(self):
        src = _alert_src()
        # The surviving mentions are comments recording the change; what must
        # be gone is the rendered button label.
        assert """I'M TRAPPED / NEED HELP"}""" not in src
        # #283 (2026-08-22): sentence case everywhere. The agreed capitals
        # exceptions are the triage category names and DROP. COVER. HOLD ON.
        # — this button is neither, so the rendered label is "I need help".
        assert '"I need help"}' in src

    def test_severity_options_are_unchanged_including_green(self):
        """Someone can genuinely need help while only lightly hurt."""
        src = _alert_src()
        assert "I can walk and I&apos;m not badly hurt" in src
        assert 'testID="triage-green"' in src
        assert 'testID="triage-yellow"' in src
        assert 'testID="triage-red"' in src


class TestEgressIsAskedOfGreenOnly:
    def test_green_opens_the_egress_sheet_not_a_mobility_sheet(self):
        src = _alert_src()
        assert 'if (severity === "green") {' in src
        assert "setEgressOpen(true)" in src
        # Yellow keeps its own question; red goes straight through.
        assert 'if (severity === "yellow") {' in src
        assert "setMobilityOpen(true)" in src

    def test_red_is_not_asked_about_egress(self):
        """Red already implies immobility and gets maximum response anyway.

        #289: the trailing `true` marks it as a follow-up, so escalating to
        IMMEDIATE after a first report has already been sent still gets
        through the "already sending" guard.
        """
        src = _alert_src()
        assert 'submitCheckIn("trapped", severity, "trapped", null, true)' in src

    def test_question_is_about_the_building_not_the_body(self):
        src = _alert_src()
        assert "Can you get out on your own?" in src
        assert 'testID="egress-can-exit"' in src
        assert 'testID="egress-cannot-exit"' in src


class TestEgressEndToEnd:
    def test_cannot_exit_sets_needs_extraction_without_touching_severity(self):
        assert _post(egress="cannot_exit").status_code == 200
        row = _device_row()
        assert row["egress"] == "cannot_exit"
        assert row["needs_extraction"] is True
        # Severity is a medical axis; extraction is a structural one. A minor
        # injury must not be inflated into a red band by the door being jammed.
        assert row["severity"] == "green"
        assert row["mobility"] == "mobile"

    def test_can_exit_does_not_flag_extraction(self):
        assert _post(egress="can_exit").status_code == 200
        row = _device_row()
        assert row["egress"] == "can_exit"
        assert row["needs_extraction"] is False

    def test_omitted_egress_is_null_not_false_positive(self):
        assert _post().status_code == 200
        row = _device_row()
        assert row["egress"] is None
        assert row["needs_extraction"] is False

    def test_invalid_egress_value_is_rejected(self):
        assert _post(egress="maybe").status_code == 422

    def test_egress_is_dropped_for_a_safe_report(self):
        assert _post(status="safe", severity=None, mobility=None,
                     egress="cannot_exit").status_code == 200
        row = _device_row()
        assert row["egress"] is None and row["needs_extraction"] is False

    def test_history_carries_the_flag_per_event(self):
        _post(egress="cannot_exit")
        r = requests.get(f"{BASE_URL}/api/admin/device-history/{DEVICE}",
                         headers=HEADERS, timeout=30)
        assert r.status_code == 200
        stuck = [e for e in r.json()["events"] if e.get("needs_extraction")]
        assert stuck, "the extraction flag must be visible per event, not only latest"

    def test_operational_report_states_the_count_with_its_base(self):
        from io import BytesIO

        from pypdf import PdfReader
        _post(egress="cannot_exit")
        r = requests.get(f"{BASE_URL}/api/admin/casualty-report/operational.pdf",
                         headers=HEADERS, timeout=60)
        assert r.status_code == 200
        text = "".join((p.extract_text() or "")
                       for p in PdfReader(BytesIO(r.content)).pages)
        assert "cannot get out on their own" in text
        # Never a bare percentage — the base is stated on the same line.
        assert "%" not in text.split("cannot get out on their own")[0][-80:]

    def test_public_report_still_names_nobody(self):
        from io import BytesIO

        from pypdf import PdfReader
        _post(egress="cannot_exit", display_name="EgressPiiCanary")
        r = requests.get(f"{BASE_URL}/api/admin/casualty-report/public.pdf",
                         headers=HEADERS, timeout=60)
        assert r.status_code == 200
        text = "".join((p.extract_text() or "")
                       for p in PdfReader(BytesIO(r.content)).pages)
        assert "EgressPiiCanary" not in text
