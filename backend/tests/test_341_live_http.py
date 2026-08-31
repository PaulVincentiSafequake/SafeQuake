"""Live HTTP smoke test for #341 location-preservation contract.

Complements test_341_no_pin_note_and_location_preserved.py (which is
white-box: it re-implements the guard against the normalizer). This
one drives the ACTUAL /api/status endpoint against the running
backend, then reads /api/devices to prove the preservation survives
in Mongo end-to-end.
"""
from __future__ import annotations

import os
import time
import uuid

import pytest
import pymongo
import requests
from dotenv import load_dotenv

load_dotenv("/app/backend/.env")

BASE_URL = os.environ.get(
    "EXPO_PUBLIC_BACKEND_URL",
    "http://localhost:8001",
).rstrip("/")


@pytest.fixture(scope="module")
def mongo_db():
    client = pymongo.MongoClient(os.environ["MONGO_URL"])
    yield client[os.environ.get("DB_NAME", "test_database")]
    client.close()


def _row_for(mongo_db, device_id):
    return mongo_db.device_status.find_one({"device_id": device_id}, {"_id": 0})


@pytest.fixture()
def device_id(mongo_db):
    did = f"TEST_341_{uuid.uuid4().hex[:10]}"
    yield did
    # Clean up.
    mongo_db.device_status.delete_many({"device_id": did})


def _post_status(payload):
    return requests.post(
        f"{BASE_URL}/api/status", json=payload, timeout=15,
    )


class TestLocationPreservationEndToEnd:
    def test_locationless_recheck_preserves_prior_fix(self, device_id, mongo_db):
        # Step 1: fresh check-in WITH GPS.
        r1 = _post_status({
            "device_id": device_id,
            "status": "trapped",
            "severity": "red",
            "latitude": 35.9,
            "longitude": 14.5,
            "accuracy": 12.0,
        })
        assert r1.status_code in (200, 201), r1.text

        # Step 2: locationless re-check (no lat/lng/accuracy).
        r2 = _post_status({
            "device_id": device_id,
            "status": "trapped",
            "severity": "red",
        })
        assert r2.status_code in (200, 201), r2.text

        # Step 3: confirm coordinates survived in Mongo.
        time.sleep(0.3)
        row = _row_for(mongo_db, device_id)
        assert row is not None, f"device {device_id} missing from device_status"
        assert row.get("latitude") == 35.9, (
            f"latitude was clobbered by locationless re-check: {row}"
        )
        assert row.get("longitude") == 14.5, (
            f"longitude was clobbered by locationless re-check: {row}"
        )
        assert row.get("accuracy_m") == 12.0, (
            f"accuracy_m was cleared by locationless re-check: {row}"
        )
        # And the newer status/severity DID land (safe direction).
        assert row.get("status") == "trapped"
        assert row.get("severity") == "red"

    def test_fresh_fix_overwrites_prior_fix(self, device_id, mongo_db):
        r1 = _post_status({
            "device_id": device_id,
            "status": "trapped",
            "severity": "red",
            "latitude": 10.0,
            "longitude": 20.0,
            "accuracy": 50.0,
        })
        assert r1.status_code in (200, 201), r1.text

        r2 = _post_status({
            "device_id": device_id,
            "status": "trapped",
            "severity": "red",
            "latitude": 11.11,
            "longitude": 22.22,
            "accuracy": 5.0,
        })
        assert r2.status_code in (200, 201), r2.text

        time.sleep(0.3)
        row = _row_for(mongo_db, device_id)
        assert row is not None
        assert row.get("latitude") == 11.11
        assert row.get("longitude") == 22.22
        assert row.get("accuracy_m") == 5.0

    def test_locationless_first_ever_checkin_has_no_coords(self, device_id, mongo_db):
        r = _post_status({
            "device_id": device_id,
            "status": "trapped",
            "severity": "red",
        })
        assert r.status_code in (200, 201), r.text

        time.sleep(0.3)
        row = _row_for(mongo_db, device_id)
        assert row is not None
        # Either the field is missing OR it is explicitly null. Both are
        # acceptable — the dashboard note is gated on `!= null`.
        assert row.get("latitude") is None
        assert row.get("longitude") is None
