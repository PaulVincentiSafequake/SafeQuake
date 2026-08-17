"""Batch 4 (2026-08-17) tests.

  A1  no "B1"/"B2" jargon anywhere in the VISIBLE text of team, public
      and audit PDFs; plain-language title; cross-reference points to the
      "safe to share" report by its dashboard-card name.
  A2  exactly one Quake Angel mark: a partner logo that visually
      duplicates the QA mark is hidden from PDF headers (mirrors the
      dashboard's looksLikeBrandMark); no "In partnership with" caption
      when no (real) partner logo exists; caption present for a real one.
  B3  per-person history: reconfirmations (same status re-reported) are
      their own dated entries; battery + location stored per event;
      /api/admin/device-history/{device_id} exposes it all.
"""
import base64
import io as _io
import os
import re
import time
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

TEAM_URL = f"{BASE_URL}/api/admin/casualty-report/operational.pdf"
PUBLIC_URL = f"{BASE_URL}/api/admin/casualty-report/public.pdf"
AUDIT_PDF_URL = f"{BASE_URL}/api/admin/audit-log/export.pdf"
LOGO_URL = f"{BASE_URL}/api/admin/dashboard-settings/logo"
STATUS_URL = f"{BASE_URL}/api/status"

HEADERS = {"X-Admin-Token": ADMIN_TOKEN}

# The embedded Quake Angel mark, straight from the server source — the
# same bytes the PDFs draw, so "visually identical" is trivially true.
_SRC = open("/app/backend/server.py").read()
QA_B64 = re.search(r'_QA_LOGO_B64 = "([A-Za-z0-9+/=]+)"', _SRC).group(1)


def _pdf_text(content: bytes) -> str:
    from pypdf import PdfReader
    reader = PdfReader(_io.BytesIO(content))
    return "".join((p.extract_text() or "") for p in reader.pages)


def _distinct_logo_b64() -> str:
    """A logo that could not possibly be mistaken for the QA mark."""
    from PIL import Image
    im = Image.new("RGBA", (64, 64), (255, 255, 255, 255))
    for x in range(8, 56):
        for y in range(8, 56):
            im.putpixel((x, y), (0, 68, 204, 255))
    buf = _io.BytesIO()
    im.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


def _delete_logo():
    requests.delete(LOGO_URL, headers=HEADERS, timeout=15)


@pytest.fixture(scope="module", autouse=True)
def restore_logo_state():
    """Whatever the tests do to the org logo, leave the system clean."""
    yield
    _delete_logo()


class TestA1JargonFreePdfs:
    def _texts(self):
        team = requests.get(TEAM_URL, headers=HEADERS,
                            params={"detail": "full"}, timeout=60)
        team_sum = requests.get(TEAM_URL, headers=HEADERS, timeout=60)
        public = requests.get(PUBLIC_URL, headers=HEADERS, timeout=60)
        audit = requests.get(AUDIT_PDF_URL, headers=HEADERS, timeout=60)
        for r in (team, team_sum, public, audit):
            assert r.status_code == 200
        return {
            "team-full": _pdf_text(team.content),
            "team-summary": _pdf_text(team_sum.content),
            "public": _pdf_text(public.content),
            "audit": _pdf_text(audit.content),
        }

    def test_no_b1_b2_anywhere(self):
        _delete_logo()   # logo variants tested separately
        for name, text in self._texts().items():
            assert "B1" not in text, f"'B1' still present in {name} PDF"
            assert "B2" not in text, f"'B2' still present in {name} PDF"

    def test_team_title_is_plain_language(self):
        r = requests.get(TEAM_URL, headers=HEADERS, timeout=60)
        text = _pdf_text(r.content)
        assert "Team report" in text
        assert "perational casualty report" in text  # case-tolerant

    def test_cross_reference_uses_dashboard_card_name(self):
        r = requests.get(TEAM_URL, headers=HEADERS, timeout=60)
        text = _pdf_text(r.content)
        assert "safe to share" in text, \
            "closing note / banner must point to the report by its card name"
        assert "END OF TEAM REPORT" in text


class TestA2SingleQuakeAngelMark:
    def test_duplicate_partner_logo_hidden(self):
        r = requests.post(LOGO_URL, headers=HEADERS,
                          json={"logo_b64": QA_B64, "mime": "image/png"}, timeout=15)
        assert r.status_code == 200, r.text
        for url in (TEAM_URL, PUBLIC_URL, AUDIT_PDF_URL):
            text = _pdf_text(requests.get(url, headers=HEADERS, timeout=60).content)
            assert "In partnership with" not in text, \
                f"duplicate-of-brand logo must be hidden on {url}"

    def test_no_logo_means_no_partnership_caption(self):
        _delete_logo()
        for url in (TEAM_URL, PUBLIC_URL, AUDIT_PDF_URL):
            text = _pdf_text(requests.get(url, headers=HEADERS, timeout=60).content)
            assert "In partnership with" not in text, \
                f"caption must not appear with no partner logo on {url}"

    def test_real_partner_logo_is_labelled(self):
        r = requests.post(LOGO_URL, headers=HEADERS,
                          json={"logo_b64": _distinct_logo_b64(), "mime": "image/png"},
                          timeout=15)
        assert r.status_code == 200, r.text
        for url in (TEAM_URL, PUBLIC_URL, AUDIT_PDF_URL):
            text = _pdf_text(requests.get(url, headers=HEADERS, timeout=60).content)
            assert "In partnership with" in text, \
                f"a real partner logo must carry its label on {url}"
        _delete_logo()


class TestB3PerPersonHistory:
    DEVICE = f"qg-hist-test-{uuid.uuid4().hex[:8]}"

    @pytest.fixture(scope="class", autouse=True)
    def cleanup(self):
        yield
        client = MongoClient(MONGO_URL)
        dbx = client[DB_NAME]
        dbx.device_status.delete_many({"device_id": self.DEVICE})
        dbx.status_events.delete_many({"device_id": self.DEVICE})
        client.close()

    def _post(self, battery, lat):
        payload = {
            "deviceId": self.DEVICE,
            "status": "trapped",
            "severity": "red",
            "mobility": "trapped",
            "display_name": "History Test Person",
            "batteryLevel": battery,
            "latitude": lat,
            "longitude": 14.51,
        }
        r = requests.post(STATUS_URL, json=payload, timeout=15)
        assert r.status_code == 200, r.text

    def test_reconfirmation_is_its_own_entry(self):
        self._post(0.50, 35.90)
        time.sleep(1.1)   # distinct recorded_at values
        self._post(0.40, 35.91)   # same status/severity/mobility → reconfirmation
        r = requests.get(f"{BASE_URL}/api/admin/device-history/{self.DEVICE}",
                         headers=HEADERS, timeout=30)
        assert r.status_code == 200, r.text
        d = r.json()
        status_events = [e for e in d["events"] if e["kind"] == "status"]
        assert len(status_events) == 2, \
            "a reconfirmation with no change MUST produce its own entry"
        newest, oldest = status_events[0], status_events[1]
        assert oldest["reconfirmation"] is False
        assert newest["reconfirmation"] is True, \
            "identical re-report must be flagged as a reconfirmation"

    def test_battery_and_location_stored_per_event(self):
        r = requests.get(f"{BASE_URL}/api/admin/device-history/{self.DEVICE}",
                         headers=HEADERS, timeout=30)
        d = r.json()
        status_events = [e for e in d["events"] if e["kind"] == "status"]
        batteries = {e["battery_pct"] for e in status_events}
        lats = {e["latitude"] for e in status_events}
        assert batteries == {50, 40}, f"per-event battery lost: {batteries}"
        assert lats == {35.90, 35.91}, f"per-event location lost: {lats}"

    def test_last_known_summary_present(self):
        r = requests.get(f"{BASE_URL}/api/admin/device-history/{self.DEVICE}",
                         headers=HEADERS, timeout=30)
        d = r.json()
        lk = d["last_known"]
        assert lk["status"] == "trapped"
        assert lk["battery_pct"] == 40
        assert lk["silent_seconds"] is not None
        assert lk["is_stale"] is False   # just posted seconds ago

    def test_history_requires_auth(self):
        r = requests.get(f"{BASE_URL}/api/admin/device-history/{self.DEVICE}",
                         timeout=15)
        assert r.status_code in (401, 403)
