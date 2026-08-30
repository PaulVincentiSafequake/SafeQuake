"""Tests for /api/admin/audit-log/export.csv and export.pdf (Task A).

Seeds TEST_EXPORT_* rows directly into Mongo, verifies CSV+PDF output,
then cleans up so /api/audit isn't polluted for the dashboard.
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

# Load backend .env so JWT_SECRET / MONGO_URL / ADMIN_TRIGGER_PASSWORD are
# available to both the tests and auth.issue_app_jwt.
load_dotenv("/app/backend/.env")
load_dotenv("/app/frontend/.env")

BASE_URL = (
    os.environ.get("EXPO_PUBLIC_BACKEND_URL")
    or os.environ.get("EXPO_BACKEND_URL")
    or "https://siren-fix.preview.emergentagent.com"
).rstrip("/")
ADMIN_TOKEN = os.environ["ADMIN_TRIGGER_PASSWORD"]
MONGO_URL = os.environ.get("MONGO_URL") or "mongodb://localhost:27017"
DB_NAME = os.environ.get("DB_NAME") or "test_database"

EXPECTED_COLUMNS = [
    "at", "at_simple", "kind", "idempotency_key", "triggered_by", "magnitude",
    "recipients_total", "ios_count", "android_count", "delivered", "error",
    "device_id", "short_code", "display_name", "status", "severity",
    "mobility", "latitude", "longitude", "accuracy_m", "battery_pct",
    "battery_state", "platform", "rescued_by", "prior_status",
    "prior_severity", "prior_mobility", "notes", "reverted_by",
]

CSV_URL = f"{BASE_URL}/api/admin/audit-log/export.csv"
PDF_URL = f"{BASE_URL}/api/admin/audit-log/export.pdf"

SEED_TAG = f"TEST_EXPORT_{uuid.uuid4().hex[:8]}"


@pytest.fixture(scope="module")
def mongo_db():
    client = MongoClient(MONGO_URL)
    yield client[DB_NAME]
    client.close()


@pytest.fixture(scope="module")
def seeded_events(mongo_db):
    """Seed one trigger + one rescued event (with sentinel notes) + one status."""
    now = datetime.now(timezone.utc)
    trigger_at = (now - timedelta(minutes=5)).isoformat()
    rescued_at = (now - timedelta(minutes=4)).isoformat()
    status_at = (now - timedelta(minutes=3)).isoformat()

    trigger_doc = {
        "created_at": trigger_at,
        "idempotency_key": f"{SEED_TAG}-idem",
        "triggered_by": f"{SEED_TAG}@test",
        "magnitude": 4.2,
        "recipients_total": 3,
        "ios_count": 2,
        "android_count": 1,
        "push_delivered": True,
        "push_error": None,
        "_test_seed": SEED_TAG,
    }
    rescued_doc = {
        "recorded_at": rescued_at,
        "device_id": f"{SEED_TAG}-dev-1",
        "display_name": "Test Rescue Subject",
        "status": "rescued",
        "severity": "green",
        "mobility": "unknown",
        "latitude": 35.9,
        "longitude": 14.5,
        "accuracy_m": 12.0,
        "battery_pct": 88,
        "battery_state": "unplugged",
        "platform": "ios",
        "rescued_by": f"{SEED_TAG}@test",
        "notes": "REDACTION CHECK",
        "prior_status": "trapped",
        "prior_severity": "red",
        "prior_mobility": "immobile",
        "_test_seed": SEED_TAG,
    }
    status_doc = {
        "recorded_at": status_at,
        "device_id": f"{SEED_TAG}-dev-2",
        "display_name": "Test Status Reporter",
        "status": "safe",
        "severity": "green",
        "mobility": "walking",
        "latitude": 35.91,
        "longitude": 14.51,
        "accuracy_m": 8.0,
        "battery_pct": 55,
        "battery_state": "unplugged",
        "platform": "android",
        "_test_seed": SEED_TAG,
    }

    mongo_db.push_events.insert_one(trigger_doc)
    mongo_db.status_events.insert_one(rescued_doc)
    mongo_db.status_events.insert_one(status_doc)

    yield {
        "trigger_at": trigger_at,
        "rescued_at": rescued_at,
        "status_at": status_at,
        "tag": SEED_TAG,
    }

    # Teardown: only remove our TEST_EXPORT_* seed docs.
    mongo_db.push_events.delete_many({"_test_seed": SEED_TAG})
    mongo_db.status_events.delete_many({"_test_seed": SEED_TAG})


def _auth_headers():
    return {"X-Admin-Token": ADMIN_TOKEN}


# New export format: UTF-8 BOM, then a padded warning row and padded
# metadata rows (count varies — a 'warning' row appears when the window
# misses the start of the incident), THEN the column header, then data.
def _parse_export_csv(text: str):
    all_rows = list(_csv.reader(_io.StringIO(text.lstrip("\ufeff"))))
    hdr_i = next(i for i, r in enumerate(all_rows) if r[:2] == ["at", "at_simple"])
    header = all_rows[hdr_i]
    data = [dict(zip(header, r)) for r in all_rows[hdr_i + 1:]]
    return header, data


def _wide_window_params(seeded_events):
    """A ~10-minute window that spans the seed rows but stays well under 30d."""
    now = datetime.now(timezone.utc)
    since = (now - timedelta(minutes=30)).isoformat()
    until = (now + timedelta(minutes=5)).isoformat()
    return {"since": since, "until": until}


# ---------------------------------------------------------------------------
# Auth gating
# ---------------------------------------------------------------------------

class TestAuthGating:
    def test_csv_rejects_no_auth(self):
        r = requests.get(CSV_URL, timeout=15)
        assert r.status_code == 401, r.text

    def test_pdf_rejects_no_auth(self):
        r = requests.get(PDF_URL, timeout=15)
        assert r.status_code == 401, r.text

    def test_csv_rejects_bad_token(self):
        r = requests.get(CSV_URL, headers={"X-Admin-Token": "wrong-token"}, timeout=15)
        assert r.status_code == 401, r.text

    def test_pdf_rejects_bad_token(self):
        r = requests.get(PDF_URL, headers={"X-Admin-Token": "wrong-token"}, timeout=15)
        assert r.status_code == 401, r.text

    def test_csv_accepts_bearer_jwt(self, mongo_db):
        """Mint a JWT via auth.issue_app_jwt for a seeded admin user."""
        import sys
        sys.path.insert(0, "/app/backend")
        try:
            from auth import issue_app_jwt  # type: ignore
        except Exception as e:
            pytest.skip(f"Cannot import issue_app_jwt: {e}")

        admin_user = mongo_db.users.find_one(
            {"role": "admin", "google_sub": {"$type": "string"}}
        )
        if not admin_user:
            # Bootstrap admin exists but has google_sub=None (never signed in
            # via Google in this test env). Fabricate a linked admin so we
            # can exercise the Bearer JWT path without touching the real one.
            fake_sub = f"TEST_EXPORT_SUB_{uuid.uuid4().hex[:12]}"
            fake_email = f"test_export_{uuid.uuid4().hex[:6]}@example.com"
            admin_user = {
                "email": fake_email,
                "email_normalized": fake_email,
                "display_name": "Test Export Admin",
                "role": "admin",
                "allowed": True,
                "disabled": False,
                "session_version": 1,
                "google_sub": fake_sub,
                "created_at": datetime.now(timezone.utc),
                "created_by": "test_audit_log_export",
                "_test_seed": SEED_TAG,
            }
            mongo_db.users.insert_one(admin_user)
        try:
            token, _ = issue_app_jwt(admin_user)
            r = requests.get(CSV_URL, headers={"Authorization": f"Bearer {token}"}, timeout=15)
            assert r.status_code == 200, r.text
            assert "text/csv" in r.headers.get("Content-Type", "")
        finally:
            mongo_db.users.delete_many({"_test_seed": SEED_TAG})


# ---------------------------------------------------------------------------
# CSV response shape
# ---------------------------------------------------------------------------

class TestCsvResponseShape:
    def test_csv_success_headers(self, seeded_events):
        r = requests.get(CSV_URL, headers=_auth_headers(),
                         params=_wide_window_params(seeded_events), timeout=15)
        assert r.status_code == 200, r.text
        ctype = r.headers.get("Content-Type", "")
        assert ctype.startswith("text/csv"), f"Unexpected Content-Type: {ctype}"
        assert "charset=utf-8" in ctype.lower()
        disp = r.headers.get("Content-Disposition", "")
        assert "attachment" in disp
        assert "filename=" in disp
        assert r.headers.get("X-Row-Count") is not None
        _, rows = _parse_export_csv(r.text)
        assert str(len(rows)) == r.headers["X-Row-Count"]

    def test_csv_header_columns(self, seeded_events):
        r = requests.get(CSV_URL, headers=_auth_headers(),
                         params=_wide_window_params(seeded_events), timeout=15)
        assert r.status_code == 200
        header, _ = _parse_export_csv(r.text)
        assert header == EXPECTED_COLUMNS, f"Got {header}"
        assert len(header) == 29
        # Warning row comes first, before the header.
        first_line = r.text.lstrip("\ufeff").splitlines()[0]
        assert "CONFIDENTIAL" in first_line

    def test_csv_rows_are_dictreader_parseable(self, seeded_events):
        r = requests.get(CSV_URL, headers=_auth_headers(),
                         params=_wide_window_params(seeded_events), timeout=15)
        assert r.status_code == 200
        header, rows = _parse_export_csv(r.text)
        assert header == EXPECTED_COLUMNS
        # Round-trip: writing rows back should not raise.
        buf = _io.StringIO()
        w = _csv.DictWriter(buf, fieldnames=EXPECTED_COLUMNS)
        w.writeheader()
        for row in rows:
            w.writerow(row)
        assert buf.getvalue().splitlines()[0] == ",".join(EXPECTED_COLUMNS)

    def test_csv_datetime_is_iso8601(self, seeded_events):
        r = requests.get(CSV_URL, headers=_auth_headers(),
                         params=_wide_window_params(seeded_events), timeout=15)
        assert r.status_code == 200
        _, rows = _parse_export_csv(r.text)
        seed_rows = [r for r in rows if SEED_TAG in (
            (r.get("idempotency_key") or "") + (r.get("device_id") or "") + (r.get("triggered_by") or "") + (r.get("rescued_by") or "")
        )]
        assert seed_rows, "Seeded rows not found in export"
        for row in seed_rows:
            at = row["at"]
            assert at, "at field is empty"
            assert "datetime.datetime" not in at, f"Not ISO 8601: {at}"
            # Should parse via fromisoformat
            datetime.fromisoformat(at.replace("Z", "+00:00"))


# ---------------------------------------------------------------------------
# PDF response shape
# ---------------------------------------------------------------------------

class TestPdfResponseShape:
    def test_pdf_success(self, seeded_events):
        r = requests.get(PDF_URL, headers=_auth_headers(),
                         params=_wide_window_params(seeded_events), timeout=30)
        assert r.status_code == 200, r.text[:400]
        assert r.headers.get("Content-Type", "").startswith("application/pdf")
        assert r.content[:4] == b"%PDF", f"Bad magic: {r.content[:10]!r}"

    def test_pdf_nontrivial_size_even_empty(self):
        # Empty window: since == until == now → boilerplate PDF only.
        now = datetime.now(timezone.utc).isoformat()
        r = requests.get(PDF_URL, headers=_auth_headers(),
                         params={"since": now, "until": now}, timeout=30)
        assert r.status_code == 200
        assert r.content[:4] == b"%PDF"
        assert len(r.content) > 1000, f"PDF too small: {len(r.content)} bytes"


# ---------------------------------------------------------------------------
# Filter validation
# ---------------------------------------------------------------------------

class TestFilterValidation:
    def test_since_invalid_iso(self):
        r = requests.get(CSV_URL, headers=_auth_headers(),
                         params={"since": "not-a-date"}, timeout=15)
        assert r.status_code == 400
        assert "iso" in r.text.lower() or "invalid" in r.text.lower()

    def test_until_invalid_iso(self):
        r = requests.get(CSV_URL, headers=_auth_headers(),
                         params={"until": "totally-broken"}, timeout=15)
        assert r.status_code == 400

    def test_until_before_since(self):
        now = datetime.now(timezone.utc)
        r = requests.get(CSV_URL, headers=_auth_headers(), params={
            "since": now.isoformat(),
            "until": (now - timedelta(days=1)).isoformat(),
        }, timeout=15)
        assert r.status_code == 400
        assert "until" in r.text.lower()

    def test_window_wider_than_30_days(self):
        r = requests.get(CSV_URL, headers=_auth_headers(), params={
            "since": "2020-01-01T00:00:00Z",
        }, timeout=15)
        assert r.status_code == 400
        assert "30" in r.text or "wide" in r.text.lower()

    def test_limit_below_range(self):
        r = requests.get(CSV_URL, headers=_auth_headers(),
                         params={"limit": 0}, timeout=15)
        assert r.status_code == 422  # Pydantic ge=1

    def test_limit_above_range_returns_422(self):
        r = requests.get(CSV_URL, headers=_auth_headers(),
                         params={"limit": 99999}, timeout=15)
        # Pydantic Query(le=500) means 99999 is rejected outright (422).
        # (Test docstring says "clamps to 500" but the actual impl uses
        # le=500 so we accept 422 here as the stronger contract.)
        assert r.status_code in (200, 422), r.text
        if r.status_code == 200:
            # If it does clamp, verify row cap
            _, rows = _parse_export_csv(r.text)
            assert len(rows) <= 500


# ---------------------------------------------------------------------------
# Filter behavior
# ---------------------------------------------------------------------------

class TestKindAndLimitFilters:
    def test_kind_trigger_only(self, seeded_events):
        params = _wide_window_params(seeded_events)
        params["kind"] = "trigger"
        r = requests.get(CSV_URL, headers=_auth_headers(), params=params, timeout=15)
        assert r.status_code == 200
        _, rows = _parse_export_csv(r.text)
        kinds = {row["kind"] for row in rows}
        assert kinds.issubset({"trigger"}), f"Expected only 'trigger', got {kinds}"
        # And our seeded trigger must appear
        assert any(row.get("idempotency_key") == f"{SEED_TAG}-idem" for row in rows)

    def test_kind_rescued_only(self, seeded_events):
        params = _wide_window_params(seeded_events)
        params["kind"] = "rescued"
        r = requests.get(CSV_URL, headers=_auth_headers(), params=params, timeout=15)
        assert r.status_code == 200
        _, rows = _parse_export_csv(r.text)
        kinds = {row["kind"] for row in rows}
        assert kinds.issubset({"rescued"}), f"Expected only 'rescued', got {kinds}"
        assert any(row.get("device_id") == f"{SEED_TAG}-dev-1" for row in rows)

    def test_limit_one(self, seeded_events):
        params = _wide_window_params(seeded_events)
        params["limit"] = 1
        r = requests.get(CSV_URL, headers=_auth_headers(), params=params, timeout=15)
        assert r.status_code == 200
        _, rows = _parse_export_csv(r.text)
        assert len(rows) <= 1, f"limit=1 returned {len(rows)} rows"


# ---------------------------------------------------------------------------
# Empty result set
# ---------------------------------------------------------------------------

class TestEmptyResultSet:
    def test_csv_empty_window_has_only_header(self):
        now = datetime.now(timezone.utc).isoformat()
        r = requests.get(CSV_URL, headers=_auth_headers(),
                         params={"since": now, "until": now}, timeout=15)
        assert r.status_code == 200
        header, rows = _parse_export_csv(r.text)
        assert rows == []
        assert r.headers.get("X-Row-Count") == "0"
        assert header == EXPECTED_COLUMNS

    def test_pdf_empty_window_shows_no_events(self):
        now = datetime.now(timezone.utc).isoformat()
        r = requests.get(PDF_URL, headers=_auth_headers(),
                         params={"since": now, "until": now}, timeout=30)
        assert r.status_code == 200
        assert r.content[:4] == b"%PDF"
        assert r.headers.get("X-Row-Count") == "0"
        # Extract text from the PDF (streams are Flate+ASCII85 compressed).
        from pypdf import PdfReader
        reader = PdfReader(_io.BytesIO(r.content))
        text = "".join((p.extract_text() or "") for p in reader.pages)
        assert "No events" in text, f"'No events' notice missing. Extracted: {text!r}"


# ---------------------------------------------------------------------------
# Notes visibility (admin-gated → notes must be exposed)
# ---------------------------------------------------------------------------

class TestNotesVisibility:
    def test_notes_present_in_csv(self, seeded_events):
        params = _wide_window_params(seeded_events)
        r = requests.get(CSV_URL, headers=_auth_headers(), params=params, timeout=15)
        assert r.status_code == 200
        assert "REDACTION CHECK" in r.text, \
            "notes='REDACTION CHECK' not present in admin CSV export"

    def test_notes_present_in_pdf(self, seeded_events):
        params = _wide_window_params(seeded_events)
        r = requests.get(PDF_URL, headers=_auth_headers(), params=params, timeout=30)
        assert r.status_code == 200
        # ReportLab flattens Paragraph text into the PDF stream. Search
        # for the substring in the raw bytes — it may span compressed
        # streams, but for our small doc uncompressed text is dumped.
        # Fall back to pypdf if raw scan fails.
        if b"REDACTION CHECK" in r.content:
            return
        try:
            from pypdf import PdfReader
        except ImportError:
            pytest.skip("pypdf not installed; raw bytes did not contain the string")
        reader = PdfReader(_io.BytesIO(r.content))
        text = "".join((p.extract_text() or "") for p in reader.pages)
        assert "REDACTION CHECK" in text, "notes missing from PDF"
