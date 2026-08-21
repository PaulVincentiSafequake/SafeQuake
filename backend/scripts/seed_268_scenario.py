"""#268 — build Paul's exact board situation in the PREVIEW database so the
four kinds of silence, the duplicate flag and the counts can be seen end to
end through the real API.

Reproduces:
  NEO268A  live phone, reported safe a minute ago               → answering
  NEO268B  old install, app removed, same name/position as A,
           went quiet 3 minutes before A appeared               → App removed
                                                                 + duplicate flag
  NEO268C  trapped person whose app was then removed            → stays, held
  NEO268D  registered for alerts, never opened the app          → Never used
  NEO268E  phone that simply stopped reporting 3 hours ago      → Phone went dark

Run:  python scripts/seed_268_scenario.py           (insert)
      python scripts/seed_268_scenario.py --clean   (remove)
"""
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from motor.motor_asyncio import AsyncIOMotorClient  # noqa: E402

NOW = datetime.now(timezone.utc)


def ago(minutes):
    return (NOW - timedelta(minutes=minutes)).isoformat()


IDS = {
    "A": "qg-1755700000001-neo268a",
    "B": "qg-1755600000002-neo268b",
    "C": "qg-1755700000003-neo268c",
    "D": "qg-1755700000004-neo268d",
    "E": "qg-1755700000005-neo268e",
}


async def main():
    client = AsyncIOMotorClient(os.environ["MONGO_URL"])
    db = client[os.environ.get("DB_NAME", "test_database")]
    ids = list(IDS.values())

    await db.device_status.delete_many({"device_id": {"$in": ids}})
    await db.push_devices.delete_many({"user_id": {"$in": ids}})
    await db.status_events.delete_many({"device_id": {"$in": ids}})
    await db.record_decisions.delete_many({"device_id": {"$in": ids}})
    if "--clean" in sys.argv:
        print("removed the #268 scenario rows")
        return

    lat, lng = 35.8997, 14.5146
    await db.device_status.insert_many([
        {"device_id": IDS["A"], "display_name": "Neo Tester", "status": "safe",
         "latitude": lat + 0.0001, "longitude": lng + 0.0001, "accuracy_m": 12,
         "battery_pct": 64, "battery_state": "unplugged", "platform": "ios",
         "created_at": ago(57), "updated_at": ago(1)},
        {"device_id": IDS["B"], "display_name": "Neo Tester", "status": "safe",
         "latitude": lat, "longitude": lng, "accuracy_m": 15,
         "battery_pct": 41, "battery_state": "unplugged", "platform": "ios",
         "created_at": ago(600), "updated_at": ago(60)},
        {"device_id": IDS["C"], "display_name": "Neo Trapped", "status": "trapped",
         "severity": "red", "needs_extraction": True, "mobility": "cannot_move",
         "latitude": lat + 0.002, "longitude": lng + 0.002, "accuracy_m": 20,
         "battery_pct": 9, "battery_state": "unplugged", "platform": "ios",
         "trapped_since": ago(240), "created_at": ago(240), "updated_at": ago(35)},
        {"device_id": IDS["E"], "display_name": "Neo Quiet", "status": "safe",
         "latitude": lat + 0.004, "longitude": lng - 0.003, "accuracy_m": 30,
         "battery_pct": 22, "battery_state": "unplugged", "platform": "android",
         "created_at": ago(900), "updated_at": ago(180)},
    ])
    await db.status_events.insert_many([
        {"device_id": IDS["C"], "status": "trapped", "severity": "red",
         "needs_extraction": True, "latitude": lat + 0.002,
         "longitude": lng + 0.002, "recorded_at": ago(240)},
        {"device_id": IDS["A"], "status": "safe", "latitude": lat + 0.0001,
         "longitude": lng + 0.0001, "recorded_at": ago(1)},
        {"device_id": IDS["B"], "status": "safe", "latitude": lat,
         "longitude": lng, "recorded_at": ago(60)},
    ])
    await db.push_devices.insert_many([
        {"user_id": IDS["A"], "platform": "ios", "device_token": "neo268a",
         "created_at": ago(57), "updated_at": ago(1)},
        # The positive fact: Apple told us the app is gone from this phone.
        {"user_id": IDS["B"], "platform": "ios", "device_token": "neo268b",
         "created_at": ago(600), "updated_at": ago(60), "dead_token": True,
         "dead_token_reason": "Unregistered", "dead_token_at": ago(55)},
        {"user_id": IDS["C"], "platform": "ios", "device_token": "neo268c",
         "created_at": ago(240), "updated_at": ago(35), "dead_token": True,
         "dead_token_reason": "Unregistered", "dead_token_at": ago(20)},
        # Registered for alerts, never opened the app: no device_status row.
        {"user_id": IDS["D"], "platform": "ios", "device_token": "neo268d",
         "created_at": ago(4000), "updated_at": ago(4000)},
        {"user_id": IDS["E"], "platform": "android", "device_token": "neo268e",
         "created_at": ago(900), "updated_at": ago(180)},
    ])
    print("inserted the #268 scenario:")
    for k, v in IDS.items():
        print(f"  {k}  {v}")


asyncio.run(main())
