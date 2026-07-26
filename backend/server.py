from fastapi import FastAPI, APIRouter, HTTPException, Header
from dotenv import load_dotenv
from starlette.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
import os
import logging
import httpx
from pathlib import Path
from pydantic import BaseModel, Field
from typing import List, Optional
import uuid
from datetime import datetime, timezone


ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

# MongoDB connection
mongo_url = os.environ['MONGO_URL']
client = AsyncIOMotorClient(mongo_url)
db = client[os.environ['DB_NAME']]

# Emergent Push relay
PUSH_BASE_URL = "https://integrations.emergentagent.com"
PUSH_KEY = os.environ.get("EMERGENT_PUSH_KEY", "placeholder")
_push_client = httpx.AsyncClient(
    base_url=PUSH_BASE_URL,
    headers={"X-Push-Key": PUSH_KEY},
    timeout=10.0,
)

# Admin password for the "Trigger Earthquake Alert" dashboard button and the
# maintenance purge endpoint. Sent as `X-Admin-Token: <password>`.
ADMIN_TRIGGER_PASSWORD = os.environ.get("ADMIN_TRIGGER_PASSWORD", "")

app = FastAPI()
api_router = APIRouter(prefix="/api")

# ---------- Legacy status-check demo endpoints ----------
class StatusCheck(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    client_name: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

class StatusCheckCreate(BaseModel):
    client_name: str

@api_router.get("/")
async def root():
    return {"message": "Hello World"}

@api_router.post("/status", response_model=StatusCheck)
async def create_status_check(input: StatusCheckCreate):
    status_obj = StatusCheck(**input.dict())
    await db.status_checks.insert_one(status_obj.dict())
    return status_obj

@api_router.get("/status", response_model=List[StatusCheck])
async def get_status_checks():
    status_checks = await db.status_checks.find({}, {"_id": 0}).to_list(1000)
    return [StatusCheck(**s) for s in status_checks]

# ---------- Push registration + fan-out ----------
class RegisterPushBody(BaseModel):
    user_id: str
    platform: str          # "android" | "ios"
    device_token: str

class TriggerAlertBody(BaseModel):
    triggeredBy: Optional[str] = None
    magnitude: Optional[float] = None
    distance_km: Optional[float] = None
    intensity: Optional[str] = None

@api_router.post("/register-push", status_code=201)
async def register_push(body: RegisterPushBody):
    """Register a device's native push token with the Emergent push relay,
    and remember it locally so we can broadcast alerts to every device."""
    await db.push_devices.update_one(
        {"user_id": body.user_id},
        {"$set": {
            "user_id": body.user_id,
            "platform": body.platform,
            "device_token": body.device_token,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }},
        upsert=True,
    )
    try:
        resp = await _push_client.post(
            "/api/v1/push/users/register",
            json=body.model_dump(),
        )
        if resp.status_code == 401:
            raise HTTPException(500, "EMERGENT_PUSH_KEY missing or invalid")
        if resp.status_code >= 500:
            raise HTTPException(502, "Push provider unavailable")
        resp.raise_for_status()
    except HTTPException:
        raise
    except Exception as e:
        logging.warning(f"Push register failed (non-blocking): {e}")
    return {"status": "registered"}

async def send_push(recipients: List[str], data: dict, idempotency_key: Optional[str] = None) -> None:
    if not recipients:
        return
    if "title" not in data or "message" not in data:
        raise ValueError("data must include title and message")
    CHUNK = 100
    for i in range(0, len(recipients), CHUNK):
        chunk = recipients[i:i + CHUNK]
        payload = {"recipients": chunk, "data": data}
        if idempotency_key:
            payload["$idempotency_key"] = f"{idempotency_key}-{i // CHUNK}"
        try:
            resp = await _push_client.post("/api/v1/push/trigger", json=payload)
            if resp.status_code == 401:
                raise HTTPException(500, "EMERGENT_PUSH_KEY missing or invalid")
            if resp.status_code >= 500:
                raise HTTPException(502, "Push provider unavailable")
            resp.raise_for_status()
        except HTTPException:
            raise
        except Exception as e:
            logging.warning(f"Push trigger failed (non-blocking): {e}")

@api_router.post("/trigger-alert")
async def trigger_alert(
    body: TriggerAlertBody,
    x_admin_token: Optional[str] = Header(default=None, alias="X-Admin-Token"),
):
    """Broadcast a QuakeGuard alert to every registered device (except the
    device that triggered it, if provided). Push delivery failure is logged
    but never blocks the response.

    Requires header `X-Admin-Token` matching ADMIN_TRIGGER_PASSWORD.
    """
    if not ADMIN_TRIGGER_PASSWORD:
        raise HTTPException(500, "ADMIN_TRIGGER_PASSWORD not configured on server")
    if x_admin_token != ADMIN_TRIGGER_PASSWORD:
        raise HTTPException(401, "Invalid or missing X-Admin-Token")

    query = {}
    if body.triggeredBy:
        query = {"user_id": {"$ne": body.triggeredBy}}
    devices = await db.push_devices.find(query, {"_id": 0, "user_id": 1}).to_list(10000)
    recipients = [d["user_id"] for d in devices]

    idem = f"quake-{uuid.uuid4()}"
    magnitude = body.magnitude or 6.4
    title = "EARTHQUAKE ALERT"
    message = f"Magnitude {magnitude}. Are you safe? Tap to check in."
    push_delivered = True
    push_error: Optional[str] = None
    try:
        await send_push(
            recipients=recipients,
            data={
                "title": title,
                "message": message,
                "action_url": "/alert",
            },
            idempotency_key=idem,
        )
    except HTTPException as e:
        push_delivered = False
        push_error = e.detail
    except Exception as e:
        push_delivered = False
        push_error = str(e)
    return {
        "status": "broadcast",
        "recipients": len(recipients),
        "push_delivered": push_delivered,
        "push_error": push_error,
    }

# ---------- Maintenance: purge leftover test / diagnostic rows ----------
@api_router.post("/admin/purge-test-devices")
async def purge_test_devices(
    x_admin_token: Optional[str] = Header(default=None, alias="X-Admin-Token"),
):
    """Remove device rows whose user_id looks like a testing artifact
    (TEST_*, test-*, diag-*, dashboard). Password-protected."""
    if not ADMIN_TRIGGER_PASSWORD:
        raise HTTPException(500, "ADMIN_TRIGGER_PASSWORD not configured on server")
    if x_admin_token != ADMIN_TRIGGER_PASSWORD:
        raise HTTPException(401, "Invalid or missing X-Admin-Token")

    result = await db.push_devices.delete_many({
        "$or": [
            {"user_id": {"$regex": "^TEST_"}},
            {"user_id": {"$regex": "^test-"}},
            {"user_id": {"$regex": "^diag-"}},
            {"user_id": "dashboard"},
        ]
    })
    remaining = await db.push_devices.count_documents({})
    return {"deleted": result.deleted_count, "remaining": remaining}

# ---------- Wire up ----------
app.include_router(api_router)

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
)
logger = logging.getLogger(__name__)

@app.on_event("shutdown")
async def shutdown_db_client():
    client.close()
    await _push_client.aclose()
