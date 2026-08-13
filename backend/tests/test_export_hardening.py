"""Tests for the 2026-08-12 export-hardening batch (Paul's verification list):

  2.1 coordinates rounded to 5 dp on export
  2.2 operator pseudonymisation (?pseudonymise=true)
  2.3 credential guard on rescue notes (server-side reject)
  2.4 CONFIDENTIAL filename prefix on sensitive exports
  3.1 no raw HTML in B1 Name/code column
  3.2 CSV UTF-8 BOM
  3.3 CSV structure: CRLF only, uniform column count, at_simple, TRUE/FALSE,
      metadata rows, display_name backfill
  P4  response-over-time plain-language lines; no bare percentage on B2
"""
import csv as _csv
import io as _io
import os
import uuid
from datetime import datetime, timedelta, timezone

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

CSV_URL = f"{BASE_URL}/api/admin/audit-log/export.csv"
PDF_URL = f"{BASE_URL}/api/admin/audit-log/export.pdf"
B1_URL = f"{BASE_URL}/api/admin/casualty-report/operational.pdf"
B2_URL = f"{BASE_URL}/api/admin/casualty-report/public.pdf"
RESCUE_URL = f"{BASE_URL}/api/mark-rescued"

TAG = f"TEST_HARDEN_{uuid.uuid4().hex[:8]}"
HEADERS = {"X-Admin-Token": ADMIN_TOKEN}
N_PREAMBLE_ROWS = 6  # warning row + 5 metadata rows before the header


def _parse(text: str):
    rows = list(_csv.reader(_io.StringIO(text.lstrip("\ufeff"))))
    header = rows[N_PREAMBLE_ROWS]
    return header, [dict(zip(header, r)) for r in rows[N_PREAMBLE_ROWS + 1:]], rows


def _pdf_text(content: bytes) -> str:
    from pypdf import PdfReader
    reader = PdfReader(_io.BytesIO(content))
    return "".join((p.extract_text() or "") for p in reader.pages)


@pytest.fixture(scope="module")
def seeded(request):
    client = MongoClient(MONGO_URL)
    db = client[DB_NAME]
    now = datetime.now(timezone.utc)
    dev_named = f"{TAG}-dev-named"
    dev_anon = f"{TAG}-dev-anon"

    # status_events WITHOUT display_name (historical shape) + device_status
    # WITH a name → export must backfill it. Precise 12-dp coordinates →
    # export must round to 5 dp.
    db.status_events.insert_one({
        "recorded_at": (now - timedelta(hours=2)).isoformat(),
        "device_id": dev_named, "display_name": None,
        "status": "trapped", "severity": "red", "mobility": "trapped",
        "latitude": 35.887200385677, "longitude": 14.512345678901,
        "accuracy_m": 9.0, "battery_pct": 61, "battery_state": "unplugged",
        "platform": "ios", "_test_seed": TAG,
    })
    db.status_events.insert_one({
        "recorded_at": (now - timedelta(hours=1)).isoformat(),
        "device_id": dev_anon, "display_name": None,
        "status": "trapped", "severity": "yellow", "mobility": "mobile",
        "latitude": 35.901234567891, "longitude": 14.498765432109,
        "accuracy_m": 14.0, "battery_pct": 30, "battery_state": "unplugged",
        "platform": "android", "_test_seed": TAG,
    })
    # rescued event attributed to an operator email → pseudonymisation target
    db.status_events.insert_one({
        "recorded_at": (now - timedelta(minutes=30)).isoformat(),
        "device_id": dev_named, "display_name": None,
        "status": "rescued", "severity": None, "mobility": None,
        "latitude": 35.887200385677, "longitude": 14.512345678901,
        "rescued_by": f"{TAG}-op@example.com", "notes": "walked out unaided",
        "prior_status": "trapped", "prior_severity": "red",
        "platform": "ios", "_test_seed": TAG,
    })
    db.device_status.update_one(
        {"device_id": dev_named},
        {"$set": {"device_id": dev_named, "display_name": "Harden Test Person",
                  "status": "rescued", "latitude": 35.887200385677,
                  "longitude": 14.512345678901, "platform": "ios",
                  "updated_at": now.isoformat(), "_test_seed": TAG}},
        upsert=True,
    )
    # trapped then SELF-REPORTED safe — must appear in the narrative as its
    # own separately-worded figure, never merged into "found by a rescue team"
    dev_selfsafe = f"{TAG}-dev-selfsafe"
    db.status_events.insert_one({
        "recorded_at": (now - timedelta(minutes=90)).isoformat(),
        "device_id": dev_selfsafe, "display_name": None,
        "status": "trapped", "severity": "green", "mobility": "mobile",
        "latitude": 35.912345678901, "longitude": 14.487654321098,
        "accuracy_m": 18.634929726061234, "battery_pct": 77,
        "battery_state": "unplugged", "platform": "ios", "_test_seed": TAG,
    })
    db.status_events.insert_one({
        "recorded_at": (now - timedelta(minutes=20)).isoformat(),
        "device_id": dev_selfsafe, "display_name": None,
        "status": "safe", "severity": None, "mobility": None,
        "latitude": 35.912345678901, "longitude": 14.487654321098,
        "accuracy_m": 18.634929726061234, "battery_pct": 75,
        "battery_state": "unplugged", "platform": "ios", "_test_seed": TAG,
    })
    db.device_status.update_one(
        {"device_id": dev_selfsafe},
        {"$set": {"device_id": dev_selfsafe, "status": "safe",
                  "updated_at": now.isoformat(), "_test_seed": TAG}},
        upsert=True,
    )
    # device for the credential-guard live POST
    db.device_status.update_one(
        {"device_id": f"{TAG}-dev-cred"},
        {"$set": {"device_id": f"{TAG}-dev-cred", "status": "trapped",
                  "severity": "green", "updated_at": now.isoformat(),
                  "_test_seed": TAG}},
        upsert=True,
    )

    yield {"dev_named": dev_named, "dev_anon": dev_anon}

    db.status_events.delete_many({"_test_seed": TAG})
    db.device_status.delete_many({"_test_seed": TAG})
    db.status_events.delete_many({"device_id": f"{TAG}-dev-cred"})
    db.operator_pseudonyms.delete_many({"identity": {"$regex": f"^{TAG}"}})
    client.close()


def _window():
    now = datetime.now(timezone.utc)
    return {"since": (now - timedelta(hours=6)).isoformat(),
            "until": (now + timedelta(minutes=5)).isoformat()}


class TestCsvStructure:
    def test_bom_and_crlf_only(self, seeded):
        r = requests.get(CSV_URL, headers=HEADERS, params=_window(), timeout=15)
        assert r.status_code == 200
        raw = r.content
        assert raw[:3] == b"\xef\xbb\xbf", "UTF-8 BOM missing (Excel mojibake bug)"
        body = raw[3:]
        assert body.count(b"\n") == body.count(b"\r\n"), "Mixed line endings"

    def test_uniform_column_count_and_metadata(self, seeded):
        r = requests.get(CSV_URL, headers=HEADERS, params=_window(), timeout=15)
        header, _, all_rows = _parse(r.text)
        assert len({len(row) for row in all_rows}) == 1, "Ragged rows"
        keys = [row[0] for row in all_rows[:N_PREAMBLE_ROWS]]
        assert keys[0].startswith("CONFIDENTIAL")
        for expected in ("export_window_start_utc", "export_window_end_utc",
                         "generated_at_utc", "generated_by", "row_count"):
            assert expected in keys, f"metadata row {expected} missing"

    def test_at_simple_and_booleans_and_rounding(self, seeded):
        r = requests.get(CSV_URL, headers=HEADERS, params=_window(), timeout=15)
        header, rows, _ = _parse(r.text)
        assert "at_simple" in header
        ours = [x for x in rows if x.get("device_id", "").startswith(TAG)]
        assert ours, "seeded rows missing from export"
        for row in ours:
            # Plain sortable timestamp: "YYYY-MM-DD HH:MM"
            assert len(row["at_simple"]) == 16 and row["at_simple"][10] == " "
            if row["latitude"]:
                assert len(row["latitude"].split(".")[-1]) <= 5, \
                    f"latitude not rounded: {row['latitude']}"
            if row.get("accuracy_m") and "." in row["accuracy_m"]:
                assert len(row["accuracy_m"].split(".")[-1]) <= 1, \
                    f"accuracy_m not rounded to 1 dp: {row['accuracy_m']}"
            assert row["delivered"] in ("", "TRUE", "FALSE")

    def test_display_name_backfilled(self, seeded):
        r = requests.get(CSV_URL, headers=HEADERS, params=_window(), timeout=15)
        _, rows, _ = _parse(r.text)
        named = [x for x in rows if x.get("device_id") == seeded["dev_named"]]
        assert named and all(x["display_name"] == "Harden Test Person" for x in named), \
            "display_name not backfilled from device record"

    def test_confidential_filename_prefix(self, seeded):
        r = requests.get(CSV_URL, headers=HEADERS, params=_window(), timeout=15)
        assert 'filename="CONFIDENTIAL-' in r.headers.get("Content-Disposition", "")


class TestPseudonymisation:
    def test_operator_email_replaced(self, seeded):
        params = {**_window(), "pseudonymise": "true"}
        r = requests.get(CSV_URL, headers=HEADERS, params=params, timeout=15)
        assert r.status_code == 200
        assert f"{TAG}-op@example.com" not in r.text, "operator email leaked"
        _, rows, _ = _parse(r.text)
        resc = [x for x in rows if x.get("kind") == "rescued"
                and x.get("device_id") == seeded["dev_named"]]
        assert resc and resc[0]["rescued_by"].startswith("operator-")

    def test_alias_is_stable(self, seeded):
        params = {**_window(), "pseudonymise": "true"}
        a = requests.get(CSV_URL, headers=HEADERS, params=params, timeout=15)
        b = requests.get(CSV_URL, headers=HEADERS, params=params, timeout=15)
        _, ra, _ = _parse(a.text)
        _, rb, _ = _parse(b.text)
        f = lambda rows: [x["rescued_by"] for x in rows
                          if x.get("device_id") == seeded["dev_named"] and x.get("kind") == "rescued"]
        assert f(ra) == f(rb) and f(ra), "pseudonym not stable across exports"

    def test_default_export_keeps_real_identity(self, seeded):
        r = requests.get(CSV_URL, headers=HEADERS, params=_window(), timeout=15)
        assert f"{TAG}-op@example.com" in r.text, \
            "real identity must remain in non-pseudonymised export (accountability)"


class TestCredentialGuard:
    def _post(self, notes):
        return requests.post(RESCUE_URL, headers=HEADERS, timeout=15,
                             json={"deviceId": f"{TAG}-dev-cred", "notes": notes})

    def test_rejects_password_pattern(self, seeded):
        r = self._post("admin password: Xk9$mQ2vLp")
        assert r.status_code == 422, r.text
        assert "rotate" in r.json()["detail"].lower()

    def test_rejects_api_key_prefix(self, seeded):
        assert self._post("key is sk_live_abcdef1234567890abcd").status_code == 422

    def test_rejects_jwt(self, seeded):
        assert self._post("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIx").status_code == 422

    def test_allows_plain_language_note(self, seeded):
        r = self._post("Found under staircase, asked for the password to the shelter door")
        assert r.status_code == 200, r.text


class TestAccessGating:
    """2026-08-13: signed-out visitors could see per-device triage detail,
    coordinates and operator emails. /api/devices and /api/audit are now
    operator/admin gated; anonymous callers get aggregate counts only."""

    def test_devices_requires_auth(self, seeded):
        r = requests.get(f"{BASE_URL}/api/devices", timeout=15)
        assert r.status_code == 401

    def test_audit_requires_auth(self, seeded):
        r = requests.get(f"{BASE_URL}/api/audit?limit=10", timeout=15)
        assert r.status_code == 401

    def test_devices_ok_with_token(self, seeded):
        r = requests.get(f"{BASE_URL}/api/devices", headers=HEADERS, timeout=15)
        assert r.status_code == 200 and "devices" in r.json()

    def test_public_summary_is_aggregate_only(self, seeded):
        r = requests.get(f"{BASE_URL}/api/public/summary", timeout=15)
        assert r.status_code == 200
        body = r.json()
        assert set(body.keys()) == {"generated_at", "total", "counts"}
        assert set(body["counts"].keys()) == {"safe", "trapped", "rescued", "not_responding", "unknown"}
        # nothing device-shaped may leak
        text = r.text.lower()
        for banned in ("device_id", "latitude", "longitude", "short_code", "@"):
            assert banned not in text, f"public summary leaks {banned!r}"

    def test_trigger_alert_rejected_server_side(self, seeded):
        r = requests.post(f"{BASE_URL}/api/trigger-alert", timeout=15,
                          json={"triggeredBy": "anon", "magnitude": 6.0})
        assert r.status_code == 401, "trigger must be enforced server-side, not just hidden in the UI"


class TestPdfHardening:
    def test_audit_pdf_confidential_filename_and_watermark(self, seeded):
        r = requests.get(PDF_URL, headers=HEADERS, params=_window(), timeout=30)
        assert r.status_code == 200
        assert 'filename="CONFIDENTIAL-' in r.headers.get("Content-Disposition", "")
        assert _pdf_text(r.content).count("CONFIDENTIAL") >= 2  # banner + watermark

    def test_b1_no_raw_html_and_rounded_coords(self, seeded):
        r = requests.get(B1_URL, headers=HEADERS, params=_window(), timeout=30)
        assert r.status_code == 200
        assert 'filename="CONFIDENTIAL-' in r.headers.get("Content-Disposition", "")
        text = _pdf_text(r.content)
        assert "<br/>" not in text and "<font" not in text, "raw HTML leaked into B1"
        assert "Harden Test Person" in text
        assert "35.887200385677" not in text, "unrounded coordinate leaked into B1"
        assert "Response over time" in text
        assert "This only counts people using the app." in text

    def test_b1_has_no_percentage_statistic(self, seeded):
        # The former "Overall … (N%)" line restated the split narrative
        # lines and was dropped at Paul's request (2026-08-13 polish).
        # (Battery cells like "61%" are data, not statistics — allowed.)
        r = requests.get(B1_URL, headers=HEADERS, params=_window(), timeout=30)
        text = _pdf_text(r.content)
        assert "Overall:" not in text
        assert "counting app users who checked in only" not in text

    def test_all_reports_are_portrait(self, seeded):
        # 1a (2026-08-13): landscape PDFs printed on default portrait paper
        # scaled the CONFIDENTIAL band down to near-illegible.
        from pypdf import PdfReader
        for name, url in (("B1", B1_URL), ("B2", B2_URL), ("audit", PDF_URL)):
            r = requests.get(url, headers=HEADERS, params=_window(), timeout=30)
            box = PdfReader(_io.BytesIO(r.content)).pages[0].mediabox
            assert float(box.width) < float(box.height), \
                f"{name} PDF is landscape ({box.width}x{box.height}) — must be portrait"

    def test_b1_summary_variant(self, seeded):
        params = {**_window(), "detail": "summary"}
        r = requests.get(B1_URL, headers=HEADERS, params=params, timeout=30)
        assert r.status_code == 200
        assert "-summary-" in r.headers.get("Content-Disposition", "")
        text = _pdf_text(r.content)
        assert "Per-device detail omitted" in text
        assert "Harden Test Person" not in text, "summary version must not list devices"
        assert "CONFIDENTIAL" in text, "summary version keeps the confidential treatment"

    def test_b2_names_issuer_but_no_operator(self, seeded):
        r = requests.get(B2_URL, headers=HEADERS, params=_window(), timeout=30)
        text = _pdf_text(r.content)
        assert "Issued by the Quake Angel emergency response system" in text
        assert "@" not in text, "no personal email may appear on B2"

    def test_b2_has_timeline_but_no_percentage(self, seeded):
        r = requests.get(B2_URL, headers=HEADERS, params=_window(), timeout=30)
        assert r.status_code == 200
        text = _pdf_text(r.content)
        assert "How the situation has changed over time" in text
        assert "%" not in text, "B2 must never show a percentage"
        assert "This only counts people using the app." in text
        # Privacy invariant: no names/codes on B2.
        assert "Harden Test Person" not in text


class TestNarrativeTableConsistency:
    """Bug 2026-08-13: table said 'People rescued: 0' while the narrative
    said '1 of 1 found' — a self-reported safe check-in was merged into
    'found'. These tests compare the narrative AGAINST the aggregate
    figures it describes, instead of testing each in isolation."""

    def _texts(self):
        b1 = _pdf_text(requests.get(B1_URL, headers=HEADERS, params=_window(), timeout=30).content)
        b2 = _pdf_text(requests.get(B2_URL, headers=HEADERS, params=_window(), timeout=30).content)
        return b1, b2

    @staticmethod
    def _narrative_rescued(text: str):
        import re
        if "No one has been confirmed found by a rescue team yet." in text:
            return 0
        m = re.search(r"(\d+)\s+(?:person has|people have)\s+been confirmed found by a rescue team",
                      text.replace("\n", " "))
        return int(m.group(1)) if m else None

    def test_b2_rescued_narrative_equals_table(self, seeded):
        import re
        _, b2 = self._texts()
        flat = b2.replace("\n", " ")
        m = re.search(r"People rescued\s*(\d+)", flat)
        assert m, "aggregate table row 'People rescued' not found on B2"
        table_rescued = int(m.group(1))
        narrative_rescued = self._narrative_rescued(b2)
        assert narrative_rescued is not None, "rescue-team narrative line missing on B2"
        assert narrative_rescued == table_rescued, (
            f"CONTRADICTION: table says {table_rescued} rescued, "
            f"narrative says {narrative_rescued} found by a rescue team"
        )

    def test_no_merged_found_wording(self, seeded):
        b1, b2 = self._texts()
        for name, text in (("B1", b1), ("B2", b2)):
            assert "have now been found" not in text and "has now been found" not in text, (
                f"{name} still merges rescue-team confirmations and self-reported "
                "safe check-ins into a single 'found' number"
            )

    def test_self_reported_safe_is_its_own_line(self, seeded):
        b1, b2 = self._texts()
        for name, text in (("B1", b1), ("B2", b2)):
            assert "told" in text and "us themselves that they are now safe." in text, (
                f"{name} missing the separately-worded self-reported-safe figure"
            )

    def test_singular_plural_grammar(self, seeded):
        b1, b2 = self._texts()
        for name, text in (("B1", b1), ("B2", b2)):
            flat = text.replace("\n", " ")
            assert "1 people" not in flat, f"{name}: '1 people' grammar error"
            assert "1 person have" not in flat, f"{name}: '1 person have' grammar error"
            assert "1 person are" not in flat, f"{name}: '1 person are' grammar error"
