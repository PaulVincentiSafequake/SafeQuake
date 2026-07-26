"""Tests for the browser-openable /api/admin/purge-test-devices (GET) plus
regression on the POST variant and neighbouring endpoints."""
import os
import re
import pytest
import requests

BASE_URL = os.environ.get("BACKEND_URL_OVERRIDE") or "http://localhost:8001"
ADMIN_PWD = os.environ.get("ADMIN_TRIGGER_PASSWORD", "Pt3481pt")

TEST_ROWS = [
    ("TEST_a1", "android"),
    ("test-b1", "android"),
    ("diag-c1", "ios"),
    ("dashboard", "android"),
]
LEGIT_ROW = ("qg-legit-1", "android")


@pytest.fixture(scope="module")
def s():
    sess = requests.Session()
    sess.headers.update({"Content-Type": "application/json"})
    return sess


def _register(sess, user_id, platform):
    """POST /api/register-push. In this env EMERGENT_PUSH_KEY=placeholder,
    so the relay returns 401 → endpoint returns 500 AFTER the DB upsert has
    already succeeded. We therefore accept 201 or 500 as 'row is now in DB'.
    """
    return sess.post(
        f"{BASE_URL}/api/register-push",
        json={"user_id": user_id, "platform": platform, "device_token": f"tok-{user_id}"},
    )


def _assert_registered(r, uid):
    assert r.status_code in (201, 500), f"seed {uid} unexpected: {r.status_code} {r.text}"


def _count(sess):
    # Count via the preview page (which reports total rows) — but simplest is a helper:
    # We don't have a public GET-all endpoint, so parse from the preview page.
    r = sess.get(f"{BASE_URL}/api/admin/purge-test-devices", params={"token": ADMIN_PWD})
    m = re.search(r"Currently\s+(\d+)\s+total device rows", r.text)
    return int(m.group(1)) if m else -1


# ---------- Setup: seed rows ----------
class TestSeed:
    def test_seed_all_rows(self, s):
        # Nuke ALL pre-existing rows (previous test iterations left legit rows behind).
        # We do this via a direct mongo delete since the purge endpoint only removes
        # rows matching the TEST_/test-/diag-/dashboard filter.
        import pymongo
        m = pymongo.MongoClient(os.environ.get("MONGO_URL", "mongodb://localhost:27017"))
        m[os.environ.get("DB_NAME", "test_database")].push_devices.delete_many({})
        m.close()

        for uid, plat in TEST_ROWS + [LEGIT_ROW]:
            r = _register(s, uid, plat)
            _assert_registered(r, uid)


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
        # Confirm anchor with correct href
        assert f'href="?token={ADMIN_PWD}&confirm=yes"' in body
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
        # 1 legit remaining
        assert "Remaining:</b> 1" in r.text

    def test_idempotent_second_confirm(self, s):
        r = s.get(f"{BASE_URL}/api/admin/purge-test-devices",
                  params={"token": ADMIN_PWD, "confirm": "yes"})
        assert r.status_code == 200
        assert "Deleted:</b> 0" in r.text
        assert "Remaining:</b> 1" in r.text

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

    def test_post_with_header_200(self, s):
        # Reseed a test row so this POST has something to delete
        r0 = _register(s, "TEST_post_check", "android")
        _assert_registered(r0, "TEST_post_check")
        r = s.post(
            f"{BASE_URL}/api/admin/purge-test-devices",
            headers={"X-Admin-Token": ADMIN_PWD},
        )
        assert r.status_code == 200
        data = r.json()
        assert "deleted" in data and "remaining" in data
        assert data["deleted"] >= 1
        assert data["remaining"] >= 1  # legit row still there


# ---------- HTML escaping (rogue user_id) ----------
class TestHtmlEscaping:
    def test_script_tag_in_user_id_not_executed(self, s):
        rogue = "TEST_<script>alert(1)</script>"
        r = _register(s, rogue, "android")
        _assert_registered(r, rogue)
        try:
            r = s.get(f"{BASE_URL}/api/admin/purge-test-devices",
                      params={"token": ADMIN_PWD})
            assert r.status_code == 200
            # The raw <script> substring appearing verbatim in HTML is an XSS vuln.
            assert "<script>alert(1)</script>" not in r.text, (
                "SECURITY: user_id containing <script> was rendered unescaped -> XSS risk"
            )
        finally:
            s.post(f"{BASE_URL}/api/admin/purge-test-devices",
                   headers={"X-Admin-Token": ADMIN_PWD})


# ---------- Regression on neighbouring endpoints ----------
class TestRegression:
    def test_trigger_alert_still_200(self, s):
        r = s.post(
            f"{BASE_URL}/api/trigger-alert",
            headers={"X-Admin-Token": ADMIN_PWD},
            json={"triggeredBy": "regression-tester", "magnitude": 5.5},
        )
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["status"] == "broadcast"
        assert "recipients" in data

    def test_register_push_upserts(self, s):
        r = _register(s, "TEST_upsert_1", "android")
        _assert_registered(r, "upsert_1")
        r2 = _register(s, "TEST_upsert_1", "ios")  # same user_id, different platform
        _assert_registered(r2, "upsert_1")
        # Cleanup
        s.post(f"{BASE_URL}/api/admin/purge-test-devices",
               headers={"X-Admin-Token": ADMIN_PWD})

    def test_status_get_and_post(self, s):
        r = s.post(f"{BASE_URL}/api/status", json={"client_name": "TEST_client"})
        assert r.status_code == 200
        assert r.json()["client_name"] == "TEST_client"
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
        with open("/app/backend/server.py") as f:
            src = f.read()
        # Helper defined once, referenced in both GET and POST paths → 3 total occurrences
        assert src.count("_run_purge_test_devices") >= 3
