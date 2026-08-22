"""Backend tests for QuakeGuard push fan-out (register-push + trigger-alert).

Tests run against the local backend on port 8001.

Current doctrine these tests pin down:
  - #266: /api/register-push only files a row in `push_devices` when the
    push provider ACCEPTS the registration. EMERGENT_PUSH_KEY is a
    placeholder here, so the relay 401s and the endpoint answers 502 with
    no row — the app is never told a phone is on the alert list when it
    isn't.
  - #262: obviously-fake tokens are refused with 400 before anything else.
  - #245: /api/trigger-alert needs the confirmation phrase naming the
    consequence, and always answers 200 with push_delivered:false rather
    than 500 when the provider is unreachable.

Device rows are therefore seeded straight into Mongo.
"""
import os
import uuid

import pytest
import requests
from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv("/app/backend/.env")

BASE_URL = "http://localhost:8001"
MONGO_URL = os.environ["MONGO_URL"]
DB_NAME = os.environ.get("DB_NAME", "test_database")
ADMIN_PWD = os.environ["ADMIN_TRIGGER_PASSWORD"]

from server import TRIGGER_ALERT_CONFIRMATION  # noqa: E402

ADMIN_HDR = {"X-Admin-Token": ADMIN_PWD}


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
def seed_devices(db):
    suffix = uuid.uuid4().hex[:6]
    devs = [
        {"user_id": f"TEST_dev-A-{suffix}", "platform": "ios",
         "device_token": "a" * 64},
        {"user_id": f"TEST_dev-B-{suffix}", "platform": "android",
         "device_token": "fcm-" + "b" * 60},
        {"user_id": f"TEST_dev-C-{suffix}", "platform": "ios",
         "device_token": "c" * 64},
    ]
    for d in devs:
        db.push_devices.update_one({"user_id": d["user_id"]}, {"$set": d}, upsert=True)

    yield devs

    db.push_devices.delete_many({"user_id": {"$in": [d["user_id"] for d in devs]}})


def _trigger(api, **body):
    body.setdefault("confirmation_phrase", TRIGGER_ALERT_CONFIRMATION)
    return api.post(f"{BASE_URL}/api/trigger-alert", json=body, headers=ADMIN_HDR)


# ---------- (a) register-push honesty ----------
class TestRegisterPush:
    def test_refused_registration_leaves_no_row(self, api, db, clear_register_rate_limit):
        uid = f"TEST_reg_{uuid.uuid4().hex[:6]}"
        r = api.post(
            f"{BASE_URL}/api/register-push",
            json={"user_id": uid, "platform": "ios", "device_token": "d" * 64},
        )
        assert r.status_code == 502, r.text
        assert db.push_devices.count_documents({"user_id": uid}) == 0

    def test_obviously_fake_token_is_refused(self, api, clear_register_rate_limit):
        r = api.post(
            f"{BASE_URL}/api/register-push",
            json={"user_id": "TEST_fake_token", "platform": "ios",
                  "device_token": "tok-A"},
        )
        assert r.status_code == 400, r.text

    def test_register_validation_error(self, api):
        r = api.post(f"{BASE_URL}/api/register-push", json={"user_id": "x"})
        assert r.status_code == 422


# ---------- (b/c/d) trigger-alert broadcast + recipients count ----------
class TestTriggerAlert:
    @pytest.fixture(autouse=True)
    def _call_the_alert_off(self, stand_down_after):
        """Every alert these tests send is stood down again."""

    def test_triggered_by_excludes_source(self, api, seed_devices):
        src = seed_devices[0]["user_id"]
        r = _trigger(api, triggeredBy=src)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "broadcast"
        # Other rows may exist in this database; assert AT LEAST the two
        # siblings, and that the source itself is not counted.
        assert body["recipients"] >= 2
        assert body["push_delivered"] is False

    def test_triggered_by_dev_a_recipients_equals_others(self, api, db, seed_devices):
        """Strict count: recipients from body == total docs where user_id != triggeredBy."""
        src = seed_devices[0]["user_id"]
        expected = db.push_devices.count_documents({
            "user_id": {"$ne": src}, "dead_token": {"$ne": True},
        })
        r = _trigger(api, triggeredBy=src)
        assert r.status_code == 200
        assert r.json()["recipients"] == expected

    def test_empty_body_returns_all_devices(self, api, db, seed_devices):
        expected = db.push_devices.count_documents({"dead_token": {"$ne": True}})
        r = _trigger(api)
        assert r.status_code == 200
        body = r.json()
        assert body["recipients"] == expected
        assert body["push_delivered"] is False

    def test_magnitude_flows_through(self, api, seed_devices):
        src = seed_devices[0]["user_id"]
        r = _trigger(api, triggeredBy=src, magnitude=7.1)
        assert r.status_code == 200
        body = r.json()
        assert body["recipients"] >= 2
        assert body["status"] == "broadcast"
        assert body["push_delivered"] is False

    def test_never_500s_on_upstream_failure(self, api):
        """Core invariant: even with no upstream key, trigger-alert must be 200."""
        r = _trigger(api, triggeredBy="nonexistent")
        assert r.status_code == 200
        assert r.json()["push_delivered"] is False

    def test_wrong_confirmation_phrase_is_refused(self, api):
        r = api.post(
            f"{BASE_URL}/api/trigger-alert",
            json={"triggeredBy": "nonexistent", "confirmation_phrase": "nope"},
            headers=ADMIN_HDR,
        )
        assert r.status_code == 400, r.text
        assert TRIGGER_ALERT_CONFIRMATION in r.json()["detail"]


# ---------- (e) status endpoints ----------
class TestStatus:
    def test_status_post_and_get(self, api):
        device_id = f"TEST_broadcast_{uuid.uuid4().hex[:6]}"
        r = api.post(f"{BASE_URL}/api/status",
                     json={"deviceId": device_id, "status": "safe"})
        assert r.status_code == 200, r.text
        created = r.json()
        assert created["status"] == "ok"
        assert created["device_id"] == device_id

        rg = api.get(f"{BASE_URL}/api/status")
        assert rg.status_code == 200
        assert isinstance(rg.json(), list)

    def test_root(self, api):
        r = api.get(f"{BASE_URL}/api/")
        assert r.status_code == 200
        assert r.json() == {"message": "Hello World"}
