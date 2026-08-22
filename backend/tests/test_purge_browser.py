"""Tests for the browser-openable /api/admin/purge-test-devices (GET) plus
regression on the POST variant and neighbouring endpoints.

Seeding note (#266): rows are inserted straight into Mongo rather than via
/api/register-push. Since #266 that endpoint only files a device row when
the push provider ACCEPTS the registration, and in this environment
EMERGENT_PUSH_KEY is a placeholder so every registration is refused — by
design. Going through the endpoint would therefore seed nothing.
"""
import os
import re

import pymongo
import pytest
import requests
from dotenv import load_dotenv

load_dotenv("/app/backend/.env")

BASE_URL = os.environ.get("BACKEND_URL_OVERRIDE") or "http://localhost:8001"
ADMIN_PWD = os.environ["ADMIN_TRIGGER_PASSWORD"]

from server import TRIGGER_ALERT_CONFIRMATION  # noqa: E402

TEST_ROWS = [
    ("TEST_a1", "android"),
    ("test-b1", "android"),
    ("diag-c1", "ios"),
    ("dashboard", "android"),
]
LEGIT_ROW = ("qg-legit-1", "android")

# Everything the purge endpoint considers a test row.
PURGE_FILTER = {
    "$or": [
        {"user_id": {"$regex": "^TEST_"}},
        {"user_id": {"$regex": "^test-"}},
        {"user_id": {"$regex": "^diag-"}},
        {"user_id": "dashboard"},
    ]
}


@pytest.fixture(scope="module")
def s():
    sess = requests.Session()
    sess.headers.update({"Content-Type": "application/json"})
    return sess


@pytest.fixture(scope="module")
def devices():
    m = pymongo.MongoClient(os.environ["MONGO_URL"])
    col = m[os.environ.get("DB_NAME", "test_database")].push_devices
    yield col
    m.close()


def _seed(col, rows):
    for uid, plat in rows:
        col.update_one(
            {"user_id": uid},
            {"$set": {"user_id": uid, "platform": plat,
                      "device_token": f"tok-{uid}"}},
            upsert=True,
        )


def _count(sess):
    # No public GET-all endpoint, so read the total off the preview page.
    r = sess.get(f"{BASE_URL}/api/admin/purge-test-devices", params={"token": ADMIN_PWD})
    m = re.search(r"Currently\s+(\d+)\s+total device rows", r.text)
    return int(m.group(1)) if m else -1


def _remaining(text):
    m = re.search(r"Remaining:</b>\s*(\d+)", text)
    assert m, text[:400]
    return int(m.group(1))


# ---------- Setup: seed rows ----------
class TestSeed:
    def test_seed_all_rows(self, devices):
        # Clear only the rows the purge endpoint would match, so the counts
        # below are exact without destroying real rows other tests rely on.
        devices.delete_many(PURGE_FILTER)
        _seed(devices, TEST_ROWS + [LEGIT_ROW])
        assert devices.count_documents(PURGE_FILTER) == len(TEST_ROWS)


# ---------- Auth on GET ----------
class TestGetAuth:
    def test_no_token(self, s):
        r = s.get(f"{BASE_URL}/api/admin/purge-test-devices")
        assert r.status_code == 401
        assert "Wrong password" in r.text
        assert "text/html" in r.headers.get("content-type", "").lower()

    def test_wrong_token(self, s):
        r = s.get(f"{BASE_URL}/api/admin/purge-test-devices", params={"token": "wrong"})
        assert r.status_code == 401
        assert "Wrong password" in r.text


# ---------- Preview page (does NOT delete) ----------
class TestPreview:
    def test_preview_shows_4_rows_and_no_delete(self, s):
        before = _count(s)  # snapshot count via preview call
        r = s.get(f"{BASE_URL}/api/admin/purge-test-devices",
                  params={"token": ADMIN_PWD})
        assert r.status_code == 200
        body = r.text
        assert "Preview: 4 test row(s)" in body, body[:400]
        # Each of the 4 test uids present in <li><code>
        for uid, _ in TEST_ROWS:
            assert f"<li><code>{uid}</code>" in body, f"missing row {uid}"
        # Confirm anchor with correct href — ampersand MUST be HTML-encoded as &amp;
        assert f'href="?token={ADMIN_PWD}&amp;confirm=yes"' in body, (
            "confirm anchor href should HTML-encode the ampersand between query params"
        )
        assert f'href="?token={ADMIN_PWD}&confirm=yes"' not in body, (
            "confirm anchor href must not use raw '&' between query params"
        )
        # Preview page must carry the no-referrer meta tag
        assert '<meta name="referrer" content="no-referrer">' in body, (
            "preview page missing <meta name='referrer' content='no-referrer'>"
        )
        # Legit row NOT listed as a match
        assert f"<li><code>{LEGIT_ROW[0]}</code>" not in body

        after = _count(s)
        assert before == after, "Preview should NOT delete rows"


# ---------- Confirm delete ----------
class TestConfirmDelete:
    def test_confirm_deletes_4(self, s):
        r = s.get(f"{BASE_URL}/api/admin/purge-test-devices",
                  params={"token": ADMIN_PWD, "confirm": "yes"})
        assert r.status_code == 200
        assert "Deleted:</b> 4" in r.text, r.text[:400]
        # The legit row (and any other real row) survives.
        assert _remaining(r.text) >= 1
        # Result HTML must also carry the no-referrer meta tag
        assert '<meta name="referrer" content="no-referrer">' in r.text, (
            "result page missing <meta name='referrer' content='no-referrer'>"
        )

    def test_idempotent_second_confirm(self, s):
        r = s.get(f"{BASE_URL}/api/admin/purge-test-devices",
                  params={"token": ADMIN_PWD, "confirm": "yes"})
        assert r.status_code == 200
        assert "Deleted:</b> 0" in r.text
        assert _remaining(r.text) >= 1

    def test_preview_now_empty(self, s):
        r = s.get(f"{BASE_URL}/api/admin/purge-test-devices",
                  params={"token": ADMIN_PWD})
        assert r.status_code == 200
        assert "Preview: 0 test row(s)" in r.text
        assert "Nothing matching to delete." in r.text


# ---------- POST variant (programmatic) ----------
class TestPostVariant:
    def test_post_no_header_401(self, s):
        r = s.post(f"{BASE_URL}/api/admin/purge-test-devices")
        assert r.status_code == 401
        # JSON body (not HTML)
        assert r.headers.get("content-type", "").startswith("application/json")

    def test_post_with_header_200(self, s, devices):
        # Reseed a test row so this POST has something to delete
        _seed(devices, [("TEST_post_check", "android")])
        r = s.post(
            f"{BASE_URL}/api/admin/purge-test-devices",
            headers={"X-Admin-Token": ADMIN_PWD},
        )
        assert r.status_code == 200
        data = r.json()
        assert "deleted" in data and "remaining" in data
        assert data["deleted"] >= 1
        assert data["remaining"] >= 1  # legit row still there


# ---------- HTML escaping (rogue user_id + platform) ----------
class TestHtmlEscaping:
    def test_script_tag_in_user_id_not_executed(self, s, devices):
        rogue_uid = "TEST_<script>alert(1)</script>"
        rogue_plat = "<img src=x onerror=alert(2)>"
        _seed(devices, [(rogue_uid, rogue_plat)])
        try:
            r = s.get(f"{BASE_URL}/api/admin/purge-test-devices",
                      params={"token": ADMIN_PWD})
            assert r.status_code == 200
            body = r.text

            # (b) Raw dangerous substrings MUST NOT appear
            assert "<script>alert(1)</script>" not in body, (
                "SECURITY: user_id containing <script> was rendered unescaped -> XSS"
            )
            assert "onerror=alert(2)>" not in body, (
                "SECURITY: platform containing raw '<img ... onerror=...>' rendered "
                "unescaped -> XSS. The closing '>' after onerror=alert(2) must be encoded."
            )

            # (b) HTML-encoded equivalents MUST appear
            assert "&lt;script&gt;alert(1)&lt;/script&gt;" in body, (
                "expected HTML-encoded <script> substring in preview HTML"
            )
            # platform is inside (...) — the '<' before img must be &lt; and '>' &gt;
            assert "&lt;img src=x onerror=alert(2)&gt;" in body, (
                "expected HTML-encoded <img ...> substring in preview HTML"
            )

            # (d) preview meta referrer tag still present with rogue data too
            assert '<meta name="referrer" content="no-referrer">' in body
        finally:
            s.post(f"{BASE_URL}/api/admin/purge-test-devices",
                   headers={"X-Admin-Token": ADMIN_PWD})

    def test_confirm_href_escapes_token(self, s):
        """(c) Confirm anchor href must HTML-escape the token substring itself.
        We can't change ADMIN_TRIGGER_PASSWORD at runtime, so instead we verify
        the code path uses html.escape on the token by asserting the exact
        escape() output for the current token appears in the href (identity for
        alphanumeric passwords) AND that the '&amp;' separator is present.
        """
        import html as _h
        r = s.get(f"{BASE_URL}/api/admin/purge-test-devices",
                  params={"token": ADMIN_PWD})
        assert r.status_code == 200
        expected = f'href="?token={_h.escape(ADMIN_PWD)}&amp;confirm=yes"'
        assert expected in r.text, f"expected escaped href not found: {expected}"


# ---------- Regression on neighbouring endpoints ----------
class TestRegression:
    def test_trigger_alert_still_200(self, s, stand_down_after):
        r = s.post(
            f"{BASE_URL}/api/trigger-alert",
            headers={"X-Admin-Token": ADMIN_PWD},
            json={"triggeredBy": "regression-tester", "magnitude": 5.5,
                  "confirmation_phrase": TRIGGER_ALERT_CONFIRMATION},
        )
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["status"] == "broadcast"
        assert "recipients" in data

    def test_status_get_and_post(self, s):
        r = s.post(f"{BASE_URL}/api/status",
                   json={"deviceId": "TEST_purge_status", "status": "safe"})
        assert r.status_code == 200, r.text
        assert r.json()["device_id"] == "TEST_purge_status"
        r2 = s.get(f"{BASE_URL}/api/status")
        assert r2.status_code == 200
        assert isinstance(r2.json(), list)

    @pytest.mark.parametrize("path", [
        "/api/debug/last-push-events",
        "/api/debug/test-push-browser",
        "/api/debug/recipients-sample",
        "/api/debug/probe-push",
        "/api/debug/full-recipient-list",
        "/api/debug/register-push-capture",
    ])
    def test_debug_endpoints_removed(self, s, path):
        r = s.get(f"{BASE_URL}{path}")
        assert r.status_code == 404

    def test_shared_helper_used(self):
        # Helper lives in routes_diagnostics.py and is referenced by both the
        # GET and POST paths → defined once, called twice.
        with open("/app/backend/routes_diagnostics.py") as f:
            src = f.read()
        assert src.count("_run_purge_test_devices") >= 3
