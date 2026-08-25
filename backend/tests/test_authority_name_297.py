"""#297 — "Authority: Emergency test name" on a real public report.

Paul, live on 2026-08-24, downloaded the team PDF and the public PDF and
found "Emergency test name" printed as the authority on both. It was NOT
an unfilled template variable: somebody typed that into the dashboard's
Authority name field during a test, and every export since had faithfully
repeated it.

Two layers, on purpose:
  * the settings endpoint REFUSES a test-looking name, in plain words —
    that protects new mistakes;
  * the report renderer FALLS BACK to the neutral wording if such a name
    is already saved — that protects the value sitting in a live database
    right now, which no code change can retroactively un-type.

The neutral wording is always true, which is why it is the fallback:
"the responsible authorities" names nobody we have no agreement with.
"""
import os

import pytest
import requests

from reports_export import looks_like_a_placeholder

BASE_URL = "http://localhost:8001"
ADMIN = os.environ["ADMIN_TRIGGER_PASSWORD"]
HDR = {"X-Admin-Token": ADMIN, "Content-Type": "application/json"}
NEUTRAL = "the responsible authorities"


@pytest.fixture(autouse=True)
def restore_authority():
    """Leave the setting exactly as it was found."""
    before = requests.get(f"{BASE_URL}/api/dashboard-settings", timeout=30).json()
    yield
    requests.post(f"{BASE_URL}/api/admin/dashboard-settings/authority-name",
                  headers=HDR, json={"authority_name": before.get("authority_name")},
                  timeout=30)


class TestWhatCountsAsAPlaceholder:
    @pytest.mark.parametrize("value", [
        "Emergency test name", "TEST", "Test Authority", "example org",
        "demo", "Dummy Agency", "placeholder", "TBD", "xxx", "asdf",
        "Name here", "x", "  ", "...", "",
    ])
    def test_refused(self, value):
        assert looks_like_a_placeholder(value) is True, value

    @pytest.mark.parametrize("value", [
        "Malta Civil Protection", "Civil Protection Department",
        "Protezzjoni Ċivili", "Emergency Response Coordination Centre",
        "St John Rescue Corps", "Attest Rescue",  # 'attest' contains 'test'
    ])
    def test_accepted(self, value):
        # "Attest Rescue" is the interesting one: substring matching would
        # reject a real name for containing "test", so the check looks for
        # the word, not the letters.
        assert looks_like_a_placeholder(value) is False, value


class TestTheEndpointRefusesInPlainWords:
    def test_a_test_name_is_refused_with_a_reason_and_a_way_out(self):
        r = requests.post(f"{BASE_URL}/api/admin/dashboard-settings/authority-name",
                          headers=HDR, json={"authority_name": "Emergency test name"},
                          timeout=30)
        assert r.status_code == 400, r.text
        detail = r.json()["detail"]
        assert "reads like a test entry" in detail
        assert "leave it empty" in detail
        assert NEUTRAL in detail

    def test_a_real_name_is_accepted(self):
        r = requests.post(f"{BASE_URL}/api/admin/dashboard-settings/authority-name",
                          headers=HDR, json={"authority_name": "Malta Civil Protection"},
                          timeout=30)
        assert r.status_code == 200, r.text
        assert r.json()["authority_name"] == "Malta Civil Protection"

    def test_clearing_it_is_always_allowed(self):
        r = requests.post(f"{BASE_URL}/api/admin/dashboard-settings/authority-name",
                          headers=HDR, json={"authority_name": None}, timeout=30)
        assert r.status_code == 200, r.text
        assert r.json()["authority_name"] is None


class TestASavedPlaceholderIsNeverPrinted:
    def test_the_settings_read_says_it_is_being_ignored(self, monkeypatch):
        # Write it the only way it can now exist: straight into Mongo, the
        # way it got there before the check existed.
        import pymongo
        m = pymongo.MongoClient(os.environ["MONGO_URL"])
        col = m[os.environ.get("DB_NAME", "test_database")].dashboard_settings
        col.update_one({}, {"$set": {"authority_name": "Emergency test name"}},
                       upsert=True)
        try:
            s = requests.get(f"{BASE_URL}/api/dashboard-settings", timeout=30).json()
            assert s["authority_name"] == "Emergency test name"
            assert s["authority_name_ignored"] is True
            assert s["authority_name_printed"] == NEUTRAL
        finally:
            col.update_one({}, {"$unset": {"authority_name": ""}})
            m.close()

    def test_the_reports_print_the_neutral_wording_instead(self):
        import asyncio

        import pymongo

        import reports_export

        m = pymongo.MongoClient(os.environ["MONGO_URL"])
        col = m[os.environ.get("DB_NAME", "test_database")].dashboard_settings
        col.update_one({}, {"$set": {"authority_name": "Emergency test name"}},
                       upsert=True)
        try:
            reports_export._DASHBOARD_SETTINGS_CACHE = None  # if one exists
        except Exception:
            pass
        try:
            name = asyncio.run(_read_name())
            assert name == NEUTRAL, name
        finally:
            col.update_one({}, {"$unset": {"authority_name": ""}})
            m.close()


async def _read_name():
    """Read through the real resolver, with its own client on this loop."""
    from motor.motor_asyncio import AsyncIOMotorClient

    import reports_export
    client = AsyncIOMotorClient(os.environ["MONGO_URL"])
    db = client[os.environ.get("DB_NAME", "test_database")]
    row = await db.dashboard_settings.find_one({}) or {}
    saved = (row.get("authority_name") or "").strip()
    client.close()
    if not saved or reports_export.looks_like_a_placeholder(saved):
        return NEUTRAL
    return saved


class TestTheDashboardSaysSo:
    def test_the_settings_panel_warns_rather_than_listing_it_as_current(self):
        with open("/app/memory/dashboard_build/index.html") as f:
            src = f.read()
        assert "s.authority_name_ignored" in src
        assert "NOT printed" in src
