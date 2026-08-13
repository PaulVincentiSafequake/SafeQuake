"""Backend tests — B1 Operational / B2 Public casualty report PDFs.

Covers:
  - Auth: legacy X-Admin-Token, JWT (admin + operator), unauthenticated 401
  - Content-Type, PDF magic bytes, response headers (X-Report-Kind, X-Row-Count)
  - Window validation (400 on bad dates, until<since, >30 day windows)
  - PRIVACY INVARIANT: B2 must contain zero identifiable data (regex checks
    on extracted PDF text via pypdf)
  - CONSISTENCY INVARIANT: B1 aggregate totals == B2 aggregate totals
  - Seed test: trapped/red person with display_name is in B1 but NOT in B2
  - Seed test: rescued person with display_name is NOT in B2 (rescued are anon in B2)
  - Static grep check: _B2_SAFE_KEYS belt-and-braces block exists in server.py

Uses pypdf.PdfReader to extract text — do NOT scan raw bytes.
"""
from __future__ import annotations

import io
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import requests
from dotenv import dotenv_values
from jose import jwt
from pymongo import MongoClient

# Resolve config from env files (do NOT hardcode)
BACKEND_ENV = dotenv_values("/app/backend/.env")
FRONTEND_ENV = dotenv_values("/app/frontend/.env")

BASE_URL = (
    os.environ.get("EXPO_BACKEND_URL")
    or FRONTEND_ENV.get("EXPO_PUBLIC_BACKEND_URL")
    or FRONTEND_ENV.get("EXPO_BACKEND_URL")
    or ""
).rstrip("/")
ADMIN_TOKEN = BACKEND_ENV.get("ADMIN_TRIGGER_PASSWORD") or os.environ.get("ADMIN_TRIGGER_PASSWORD") or ""
JWT_SECRET = BACKEND_ENV.get("JWT_SECRET") or os.environ.get("JWT_SECRET") or ""
MONGO_URL = BACKEND_ENV.get("MONGO_URL") or os.environ.get("MONGO_URL") or ""
DB_NAME = BACKEND_ENV.get("DB_NAME") or os.environ.get("DB_NAME") or ""

assert BASE_URL, "BASE_URL not configured"
assert ADMIN_TOKEN, "ADMIN_TRIGGER_PASSWORD missing"
assert JWT_SECRET, "JWT_SECRET missing"
assert MONGO_URL, "MONGO_URL missing"

B1_URL = f"{BASE_URL}/api/admin/casualty-report/operational.pdf"
B2_URL = f"{BASE_URL}/api/admin/casualty-report/public.pdf"

TEST_TRAPPED_DEVICE = "qg-test-casualty-pii-1234"
TEST_TRAPPED_NAME = "TEST_CASUALTY_PII_NAME"
TEST_RESCUED_DEVICE = "qg-test-casualty-rescued-5678"
TEST_RESCUED_NAME = "TEST_CASUALTY_RESCUED_NAME"


# ---------- fixtures ----------
@pytest.fixture(scope="session")
def mongo_db():
    client = MongoClient(MONGO_URL)
    return client[DB_NAME]


@pytest.fixture(scope="session")
def admin_headers():
    return {"X-Admin-Token": ADMIN_TOKEN}


@pytest.fixture(scope="session")
def operator_jwt(mongo_db):
    """Insert a temporary operator user and return a JWT for them.
    Teardown removes the user."""
    sub = f"TEST_OPERATOR_SUB_{uuid.uuid4().hex}"
    email = f"test.operator.{uuid.uuid4().hex[:8]}@example.com"
    mongo_db.users.insert_one({
        "email": email,
        "email_normalized": email,
        "display_name": "TEST_Temporary Operator",
        "role": "operator",
        "allowed": True,
        "disabled": False,
        "session_version": 1,
        "google_sub": sub,
        "created_at": datetime.now(timezone.utc),
        "created_by": "TEST_iteration_31",
    })
    now = datetime.now(timezone.utc)
    claims = {
        "sub": sub,
        "email": email,
        "role": "operator",
        "sv": 1,
        "jti": str(uuid.uuid4()),
        "iss": "quake-angel-api",
        "aud": "quake-angel-dashboard",
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=30)).timestamp()),
    }
    token = jwt.encode(claims, JWT_SECRET, algorithm="HS256")
    yield token
    mongo_db.users.delete_one({"google_sub": sub})


@pytest.fixture(scope="session")
def seeded_events(mongo_db):
    """Seed a trapped/red and a rescued status_event for consistency + PII tests.
    Cleans up in teardown."""
    now_iso = datetime.now(timezone.utc).isoformat()
    trapped_row = {
        "device_id": TEST_TRAPPED_DEVICE,
        "status": "trapped",
        "severity": "red",
        "display_name": TEST_TRAPPED_NAME,
        "recorded_at": now_iso,
        "latitude": 35.9012,
        "longitude": 14.5123,
        "accuracy_m": 8.0,
        "battery_pct": 42,
        "battery_state": "unplugged",
        "platform": "ios",
        "notes": "TEST_notes_pii",
    }
    rescued_row = {
        "device_id": TEST_RESCUED_DEVICE,
        "status": "rescued",
        "severity": "green",
        "display_name": TEST_RESCUED_NAME,
        "recorded_at": now_iso,
        "latitude": 35.8899,
        "longitude": 14.5300,
        "accuracy_m": 12.0,
        "battery_pct": 88,
        "battery_state": "unplugged",
        "platform": "android",
        "notes": "TEST_rescued_note",
    }
    mongo_db.status_events.insert_many([trapped_row, rescued_row])
    yield {"trapped": trapped_row, "rescued": rescued_row}
    mongo_db.status_events.delete_many({"device_id": {"$in": [TEST_TRAPPED_DEVICE, TEST_RESCUED_DEVICE]}})


def _extract_pdf_text(content: bytes) -> str:
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(content))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


# ---------- basic auth + content ----------
class TestAuthAndBasics:
    def test_b1_unauthenticated_returns_401(self):
        r = requests.get(B1_URL, timeout=30)
        assert r.status_code == 401, r.text

    def test_b2_unauthenticated_returns_401(self):
        r = requests.get(B2_URL, timeout=30)
        assert r.status_code == 401, r.text

    def test_b1_returns_pdf_with_admin_token(self, admin_headers):
        r = requests.get(B1_URL, headers=admin_headers, timeout=60)
        assert r.status_code == 200, r.text[:400]
        assert r.headers["Content-Type"].startswith("application/pdf")
        assert r.content.startswith(b"%PDF-"), r.content[:20]
        assert r.headers.get("X-Report-Kind") == "B1-operational"
        # X-Row-Count MUST be an integer string
        assert "X-Row-Count" in r.headers, "B1 must expose X-Row-Count header"
        int(r.headers["X-Row-Count"])

    def test_b2_returns_pdf_with_admin_token(self, admin_headers):
        r = requests.get(B2_URL, headers=admin_headers, timeout=60)
        assert r.status_code == 200, r.text[:400]
        assert r.headers["Content-Type"].startswith("application/pdf")
        assert r.content.startswith(b"%PDF-")
        assert r.headers.get("X-Report-Kind") == "B2-public"
        # X-Row-Count MUST be absent from B2 per privacy lock (see server.py inline comment)
        assert "X-Row-Count" not in r.headers, (
            "B2 must NOT expose X-Row-Count — privacy lock. See server.py"
        )

    def test_b1_accepts_operator_jwt(self, operator_jwt):
        r = requests.get(B1_URL, headers={"Authorization": f"Bearer {operator_jwt}"}, timeout=60)
        assert r.status_code == 200, r.text[:400]
        assert r.content.startswith(b"%PDF-")

    def test_b2_accepts_operator_jwt(self, operator_jwt):
        r = requests.get(B2_URL, headers={"Authorization": f"Bearer {operator_jwt}"}, timeout=60)
        assert r.status_code == 200, r.text[:400]
        assert r.content.startswith(b"%PDF-")


# ---------- window validation ----------
class TestWindowValidation:
    def test_b1_invalid_since_date(self, admin_headers):
        r = requests.get(B1_URL, headers=admin_headers, params={"since": "not-a-date"}, timeout=30)
        assert r.status_code == 400, r.text

    def test_b2_invalid_since_date(self, admin_headers):
        r = requests.get(B2_URL, headers=admin_headers, params={"since": "not-a-date"}, timeout=30)
        assert r.status_code == 400, r.text

    def test_b1_until_before_since(self, admin_headers):
        now = datetime.now(timezone.utc)
        params = {"since": now.isoformat(), "until": (now - timedelta(hours=1)).isoformat()}
        r = requests.get(B1_URL, headers=admin_headers, params=params, timeout=30)
        assert r.status_code == 400, r.text

    def test_b2_until_before_since(self, admin_headers):
        now = datetime.now(timezone.utc)
        params = {"since": now.isoformat(), "until": (now - timedelta(hours=1)).isoformat()}
        r = requests.get(B2_URL, headers=admin_headers, params=params, timeout=30)
        assert r.status_code == 400, r.text

    def test_b1_window_over_30_days(self, admin_headers):
        now = datetime.now(timezone.utc)
        params = {"since": (now - timedelta(days=31)).isoformat(), "until": now.isoformat()}
        r = requests.get(B1_URL, headers=admin_headers, params=params, timeout=30)
        assert r.status_code == 400, r.text

    def test_b2_window_over_30_days(self, admin_headers):
        now = datetime.now(timezone.utc)
        params = {"since": (now - timedelta(days=31)).isoformat(), "until": now.isoformat()}
        r = requests.get(B2_URL, headers=admin_headers, params=params, timeout=30)
        assert r.status_code == 400, r.text

    def test_default_window_last_24h_ok(self, admin_headers):
        r1 = requests.get(B1_URL, headers=admin_headers, timeout=60)
        r2 = requests.get(B2_URL, headers=admin_headers, timeout=60)
        assert r1.status_code == 200
        assert r2.status_code == 200


# ---------- privacy invariant on B2 ----------
class TestB2PrivacyInvariant:
    @pytest.fixture(scope="class")
    def b2_text(self, admin_headers, seeded_events):
        # Wide window to capture the seed
        now = datetime.now(timezone.utc)
        params = {"since": (now - timedelta(hours=1)).isoformat(), "until": (now + timedelta(minutes=5)).isoformat()}
        r = requests.get(B2_URL, headers=admin_headers, params=params, timeout=60)
        assert r.status_code == 200, r.text[:400]
        return _extract_pdf_text(r.content)

    def test_b2_contains_aggregate_labels(self, b2_text):
        required = [
            "People checked in as safe",
            "People rescued",
            "People awaiting rescue",
            "critical injury",
            "moderate injury",
            "minor injury",
            "Total people accounted for",
            "Individual identities are not disclosed",
        ]
        missing = [s for s in required if s not in b2_text]
        assert not missing, f"B2 missing aggregate labels: {missing}\nText: {b2_text[:800]}"

    def test_b2_does_not_contain_device_id_pattern(self, b2_text):
        # qg- device_id pattern
        m = re.findall(r"qg-[a-z0-9-]{6,}", b2_text)
        assert not m, f"B2 leaked device_id pattern: {m}"

    def test_b2_does_not_contain_short_code_pattern(self, b2_text):
        # 5 uppercase alnum chars (short-code shape). Column headers use lower/mixed case, so
        # we search for standalone 5-uppercase tokens.
        tokens = re.findall(r"\b[A-Z0-9]{5}\b", b2_text)
        # Whitelist any 5-cap header-ish substrings that legitimately appear.
        allowed = {"UTC"}  # UTC is 3 chars so not matched anyway; empty guardrail
        leaks = [t for t in tokens if t not in allowed]
        assert not leaks, f"B2 leaked short-code-shape tokens: {leaks}"

    def test_b2_does_not_contain_gps_coords(self, b2_text):
        # Strip window/timestamp block: window is formatted 'YYYY-MM-DD HH:MM' — no dd.dddd shape.
        # Any '\d\d\.\d{4}' pattern in B2 is a GPS leak.
        m = re.findall(r"\d\d\.\d{4}", b2_text)
        assert not m, f"B2 leaked GPS-shape numbers: {m}"

    def test_b2_does_not_contain_seed_names(self, b2_text):
        for name in (TEST_TRAPPED_NAME, TEST_RESCUED_NAME):
            assert name not in b2_text, f"B2 leaked display_name {name!r}"

    def test_b2_does_not_contain_confidential_marker(self, b2_text):
        assert "CONFIDENTIAL" not in b2_text, "B2 must not contain B1-only CONFIDENTIAL banner"


# ---------- B1 content ----------
class TestB1Content:
    @pytest.fixture(scope="class")
    def b1_text(self, admin_headers, seeded_events):
        now = datetime.now(timezone.utc)
        params = {"since": (now - timedelta(hours=1)).isoformat(), "until": (now + timedelta(minutes=5)).isoformat()}
        r = requests.get(B1_URL, headers=admin_headers, params=params, timeout=60)
        assert r.status_code == 200
        return _extract_pdf_text(r.content)

    def test_b1_contains_required_markers(self, b1_text):
        required = ["CONFIDENTIAL", "Per-device detail", "END OF B1 OPERATIONAL REPORT"]
        missing = [m for m in required if m not in b1_text]
        assert not missing, f"B1 missing markers {missing}\nText: {b1_text[:800]}"

    def test_b1_contains_trapped_display_name(self, b1_text):
        # Portrait B1 (2026-08-13) wraps this long unbroken synthetic name
        # across lines in the narrow Name column — strip newlines before
        # matching; wrapping is presentation, not data loss.
        assert TEST_TRAPPED_NAME in b1_text.replace("\n", ""), (
            f"B1 must include the seeded trapped person's display_name {TEST_TRAPPED_NAME}"
        )


# ---------- consistency B1 vs B2 ----------
class TestConsistencyB1vsB2:
    def test_totals_match(self, admin_headers, seeded_events):
        now = datetime.now(timezone.utc)
        params = {"since": (now - timedelta(hours=1)).isoformat(), "until": (now + timedelta(minutes=5)).isoformat()}
        r1 = requests.get(B1_URL, headers=admin_headers, params=params, timeout=60)
        r2 = requests.get(B2_URL, headers=admin_headers, params=params, timeout=60)
        assert r1.status_code == 200 and r2.status_code == 200

        # B1 exposes X-Row-Count (total_devices). B2 doesn't — pull total from B2 body.
        b1_total = int(r1.headers["X-Row-Count"])
        b2_text = _extract_pdf_text(r2.content)

        # B2 aggregate row: "Total people accounted for  <N>"
        m = re.search(r"Total people accounted for\s+(\d+)", b2_text)
        assert m, f"Could not find 'Total people accounted for <N>' in B2 text: {b2_text[:800]}"
        b2_total = int(m.group(1))
        assert b1_total == b2_total, f"B1 total {b1_total} != B2 total {b2_total}"

        # Also verify each aggregate line's number in B2 matches text we'd expect.
        # Extract counts from B2 for safe/rescued/awaiting.
        counts_pat = {
            "safe": r"People checked in as safe\s+(\d+)",
            "rescued": r"People rescued\s+(\d+)",
            "awaiting": r"People awaiting rescue\s+(\d+)",
            "red": r"critical injury\s+(\d+)",
            "yellow": r"moderate injury\s+(\d+)",
            "green": r"minor injury\s+(\d+)",
        }
        got = {}
        for k, p in counts_pat.items():
            mm = re.search(p, b2_text)
            assert mm, f"B2 missing count line for {k}"
            got[k] = int(mm.group(1))
        # trapped_red + trapped_yellow + trapped_green must be <= awaiting; safe+rescued+awaiting == total
        assert got["safe"] + got["rescued"] + got["awaiting"] == b2_total, got

        # Since we seeded 1 trapped/red and 1 rescued, verify those are counted.
        assert got["rescued"] >= 1
        assert got["awaiting"] >= 1
        assert got["red"] >= 1


# ---------- static grep for belt-and-braces guard ----------
class TestStaticGuardExists:
    def test_b2_safe_keys_guard_in_server_py(self):
        text = Path("/app/backend/server.py").read_text()
        assert "_B2_SAFE_KEYS" in text
        # A 500 with mention of the legal lock must exist inside B2 endpoint
        # (rough check — presence of both substrings in the file).
        assert "Legal / privacy locks" in text
        assert "HTTPException(\n            500," in text or "HTTPException(500" in text or "500," in text
