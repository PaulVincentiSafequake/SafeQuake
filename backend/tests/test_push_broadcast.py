"""Backend tests for QuakeGuard push fan-out (register-push + trigger-alert).

Tests run against localhost:8001 (per task spec: direct backend curl testing).
EMERGENT_PUSH_KEY is placeholder → upstream Emergent relay will 401.
Expected preview behaviour:
  - /api/register-push returns 500 (upstream 401 raises HTTPException) BUT
    the device row must already be persisted in db.push_devices before the
    upstream call.
  - /api/trigger-alert must ALWAYS return 200 with push_delivered:false and
    push_error='EMERGENT_PUSH_KEY missing or invalid'.
  - Legacy /api/status POST + GET must still work.
"""
import os
import uuid
import pytest
import requests
from pymongo import MongoClient

BASE_URL = "http://localhost:8001"
MONGO_URL = os.environ.get("MONGO_URL", "mongodb://localhost:27017")
DB_NAME = os.environ.get("DB_NAME", "test_database")


@pytest.fixture(scope="module")
def db():
    client = MongoClient(MONGO_URL)
    yield client[DB_NAME]
    client.close()


@pytest.fixture(scope="module")
def api():
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    return s


@pytest.fixture(scope="module")
def seed_devices(api, db):
    """Register the 3 test devices per task spec (a). Persistence must occur
    even though upstream 401s."""
    # Unique per run to avoid cross-run interference
    suffix = uuid.uuid4().hex[:6]
    devs = [
        {"user_id": f"TEST_dev-A-{suffix}", "platform": "ios", "device_token": "tok-A"},
        {"user_id": f"TEST_dev-B-{suffix}", "platform": "android", "device_token": "tok-B"},
        {"user_id": f"TEST_dev-C-{suffix}", "platform": "ios", "device_token": "tok-C"},
    ]
    for d in devs:
        r = api.post(f"{BASE_URL}/api/register-push", json=d)
        # Upstream 401 is expected → 500 surfaced OR 201 if provider passes.
        # Either way the DB row must exist. Assert body-persistence separately.
        assert r.status_code in (201, 500), f"unexpected status {r.status_code}: {r.text}"

    yield devs

    # Teardown
    db.push_devices.delete_many({"user_id": {"$in": [d["user_id"] for d in devs]}})


# ---------- (a) register-push persistence ----------
class TestRegisterPushPersistence:
    def test_register_persists_before_upstream(self, seed_devices, db):
        for d in seed_devices:
            row = db.push_devices.find_one({"user_id": d["user_id"]})
            assert row is not None, f"device {d['user_id']} not persisted"
            assert row["platform"] == d["platform"]
            assert row["device_token"] == d["device_token"]

    def test_register_upsert_idempotent(self, api, db, seed_devices):
        d = seed_devices[0]
        new_token = "tok-A-refreshed"
        r = api.post(
            f"{BASE_URL}/api/register-push",
            json={"user_id": d["user_id"], "platform": d["platform"], "device_token": new_token},
        )
        assert r.status_code in (201, 500)
        row = db.push_devices.find_one({"user_id": d["user_id"]})
        assert row["device_token"] == new_token
        # only one row per user_id (upsert not insert)
        count = db.push_devices.count_documents({"user_id": d["user_id"]})
        assert count == 1

    def test_register_validation_error(self, api):
        r = api.post(f"{BASE_URL}/api/register-push", json={"user_id": "x"})
        assert r.status_code == 422


# ---------- (b/c/d) trigger-alert broadcast + recipients count ----------
class TestTriggerAlert:
    def test_triggered_by_excludes_source(self, api, seed_devices):
        """spec (b): triggeredBy=dev-A → recipients=2 (dev-B + dev-C),
        push_delivered=false, push_error='EMERGENT_PUSH_KEY missing or invalid'."""
        src = seed_devices[0]["user_id"]
        r = api.post(f"{BASE_URL}/api/trigger-alert", json={"triggeredBy": src})
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "broadcast"
        # There may be OTHER pre-existing rows in preview db; assert AT LEAST 2
        # AND that the source is NOT counted.
        assert body["recipients"] >= 2
        assert body["push_delivered"] is False
        assert body["push_error"] == "EMERGENT_PUSH_KEY missing or invalid"

    def test_triggered_by_dev_a_recipients_equals_others(self, api, db, seed_devices):
        """Strict count: recipients from body == total docs where user_id != triggeredBy."""
        src = seed_devices[0]["user_id"]
        expected = db.push_devices.count_documents({"user_id": {"$ne": src}})
        r = api.post(f"{BASE_URL}/api/trigger-alert", json={"triggeredBy": src})
        assert r.status_code == 200
        assert r.json()["recipients"] == expected

    def test_empty_body_returns_all_devices(self, api, db, seed_devices):
        """spec (c): POST /api/trigger-alert body {} → recipients = total devices."""
        expected = db.push_devices.count_documents({})
        r = api.post(f"{BASE_URL}/api/trigger-alert", json={})
        assert r.status_code == 200
        body = r.json()
        assert body["recipients"] == expected
        assert body["push_delivered"] is False

    def test_magnitude_flows_through(self, api, seed_devices):
        """spec (d): with magnitude=7.1 the endpoint still returns 200 and
        recipients count is stable for triggeredBy=dev-A."""
        src = seed_devices[0]["user_id"]
        r = api.post(
            f"{BASE_URL}/api/trigger-alert",
            json={"triggeredBy": src, "magnitude": 7.1},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["recipients"] >= 2
        assert body["status"] == "broadcast"
        assert body["push_delivered"] is False

    def test_never_500s_on_upstream_failure(self, api):
        """Core invariant: even with no upstream key, trigger-alert must be 200."""
        r = api.post(f"{BASE_URL}/api/trigger-alert", json={"triggeredBy": "nonexistent"})
        assert r.status_code == 200
        assert r.json()["push_delivered"] is False


# ---------- (e) Legacy status endpoints ----------
class TestLegacyStatus:
    def test_status_post_and_get(self, api):
        name = f"TEST_client_{uuid.uuid4().hex[:6]}"
        r = api.post(f"{BASE_URL}/api/status", json={"client_name": name})
        assert r.status_code == 200
        created = r.json()
        assert created["client_name"] == name
        assert "id" in created and "timestamp" in created

        # GET must include the created row
        rg = api.get(f"{BASE_URL}/api/status")
        assert rg.status_code == 200
        rows = rg.json()
        assert any(x["client_name"] == name for x in rows)

    def test_root(self, api):
        r = api.get(f"{BASE_URL}/api/")
        assert r.status_code == 200
        assert r.json() == {"message": "Hello World"}
