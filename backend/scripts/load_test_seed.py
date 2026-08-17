#!/usr/bin/env python3
"""B5 load-test seeder — synthetic device_status / status_events rows only.

SAFETY (see /app/memory/load-test-plan.md):
  * Writes ONLY to `device_status` and `status_events` via direct DB insert.
    It never touches `push_devices` / any token collection, and never calls
    an alert-trigger or APNs/Expo send path. Synthetic devices therefore have
    no push token at all — a real notification is structurally impossible.
  * Every row carries synthetic=True, load_test_run_id=<uuid> and a
    device_id prefixed `qg-loadtest-`, so cleanup is exact (also the
    recommended flag for issue #146).

Usage:
    python load_test_seed.py seed  --count 100 [--hours 6] [--run-id ID]
    python load_test_seed.py count [--run-id ID]
    python load_test_seed.py clear [--run-id ID | --all] [--dry-run]
"""
import argparse
import asyncio
import os
import random
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

PREFIX = "qg-loadtest-"
# Malta bounding box — realistic pin spread for the dashboard map.
LAT_MIN, LAT_MAX = 35.79, 36.08
LON_MIN, LON_MAX = 14.18, 14.58

# Mixed realism per the plan: 20% red / 30% yellow / 30% green / 15% safe /
# 5% not-responding.
MIX = (
    [("trapped", "red")] * 20
    + [("trapped", "yellow")] * 30
    + [("trapped", "green")] * 30
    + [("safe", None)] * 15
    + [("not_responding", None)] * 5
)
FIRST_NAMES = [
    "Anna", "Marco", "Karen", "Joseph", "Maria", "Luca", "Sofia", "Paul",
    "Elena", "David", "Nadia", "Omar", "Aiko", "José", "Grace", "Liam",
]


def _db():
    client = AsyncIOMotorClient(os.environ["MONGO_URL"])
    return client, client[os.environ["DB_NAME"]]


def _mobility(severity):
    if severity == "yellow":
        return random.choice(["can_move", "cannot_move"])
    return None


async def seed(count: int, hours: int, run_id: str):
    client, db = _db()
    now = datetime.now(timezone.utc)
    latest_docs, event_docs = [], []

    for i in range(count):
        device_id = f"{PREFIX}{run_id[:8]}-{i:06d}"
        status, severity = MIX[i % len(MIX)]
        lat = random.uniform(LAT_MIN, LAT_MAX)
        lon = random.uniform(LON_MIN, LON_MAX)
        battery = random.randint(35, 100)
        # First check-in somewhere in the window, then 0-3 reconfirmations
        # (same status, fresher timestamp) with battery drain + GPS jitter.
        t0 = now - timedelta(minutes=random.randint(1, max(2, hours * 60)))
        n_events = 1 + random.randint(0, 3)
        for e in range(n_events):
            ts = t0 + timedelta(minutes=e * random.randint(8, 45))
            if ts > now:
                ts = now
            doc = {
                "device_id": device_id,
                "status": status,
                "severity": severity,
                "mobility": _mobility(severity),
                "display_name": random.choice(FIRST_NAMES),
                "latitude": round(lat + random.uniform(-0.0004, 0.0004), 6),
                "longitude": round(lon + random.uniform(-0.0004, 0.0004), 6),
                "accuracy_m": round(random.uniform(5, 45), 1),
                "battery_pct": max(1, battery - e * random.randint(1, 6)),
                "battery_state": "unplugged",
                "location_error": None,
                "platform": random.choice(["ios", "android"]),
                "synthetic": True,
                "load_test_run_id": run_id,
            }
            event_docs.append({**doc, "recorded_at": ts.isoformat()})
            last = doc
            last_ts = ts
        latest_docs.append({
            **last,
            "updated_at": last_ts.isoformat(),
            "created_at": t0.isoformat(),
        })

    for chunk_start in range(0, len(latest_docs), 1000):
        await db.device_status.insert_many(latest_docs[chunk_start:chunk_start + 1000])
    for chunk_start in range(0, len(event_docs), 1000):
        await db.status_events.insert_many(event_docs[chunk_start:chunk_start + 1000])

    print(f"run_id={run_id}")
    print(f"device_status inserted: {len(latest_docs)}")
    print(f"status_events inserted: {len(event_docs)}")
    client.close()


def _filt(run_id, all_runs):
    if all_runs:
        return {"device_id": {"$regex": f"^{PREFIX}"}}
    return {"load_test_run_id": run_id}


async def count(run_id, all_runs):
    client, db = _db()
    f = _filt(run_id, all_runs)
    ds = await db.device_status.count_documents(f)
    se = await db.status_events.count_documents(f)
    total_ds = await db.device_status.count_documents({})
    print(f"synthetic device_status: {ds}   status_events: {se}")
    print(f"device_status TOTAL (incl. real): {total_ds}")
    client.close()


async def clear(run_id, all_runs, dry_run):
    client, db = _db()
    f = _filt(run_id, all_runs)
    ds = await db.device_status.count_documents(f)
    se = await db.status_events.count_documents(f)
    if dry_run:
        print(f"DRY RUN — would delete device_status={ds} status_events={se}")
    else:
        r1 = await db.device_status.delete_many(f)
        r2 = await db.status_events.delete_many(f)
        print(f"deleted device_status={r1.deleted_count} status_events={r2.deleted_count}")
    client.close()


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("seed")
    s.add_argument("--count", type=int, required=True)
    s.add_argument("--hours", type=int, default=6)
    s.add_argument("--run-id", default=None)
    for name in ("count", "clear"):
        p = sub.add_parser(name)
        p.add_argument("--run-id", default=None)
        p.add_argument("--all", action="store_true")
        if name == "clear":
            p.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    if a.cmd == "seed":
        asyncio.run(seed(a.count, a.hours, a.run_id or str(uuid.uuid4())))
    elif a.cmd == "count":
        if not a.run_id and not a.all:
            sys.exit("pass --run-id or --all")
        asyncio.run(count(a.run_id, a.all))
    else:
        if not a.run_id and not a.all:
            sys.exit("pass --run-id or --all")
        asyncio.run(clear(a.run_id, a.all, a.dry_run))


if __name__ == "__main__":
    main()
