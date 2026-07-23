from fastapi import FastAPI, APIRouter, HTTPException, Header, Query
from fastapi.responses import HTMLResponse
from dotenv import load_dotenv
from starlette.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
import os
import logging
import httpx
import html as _html
from collections import deque
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

# Admin password for the "Trigger Earthquake Alert" dashboard button.
# Sent by the dashboard as `X-Admin-Token: <password>` on POST /api/trigger-alert.
ADMIN_TRIGGER_PASSWORD = os.environ.get("ADMIN_TRIGGER_PASSWORD", "")

# Bounded in-memory ring buffer of the last 20 push-provider interactions so
# we can inspect raw APNs / relay error bodies via /api/debug/last-push-events.
_last_push_events: "deque[dict]" = deque(maxlen=20)

app = FastAPI()
api_router = APIRouter(prefix="/api")

# ---------- Legacy status-check demo endpoints (kept as-is) ----------
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
    triggeredBy: Optional[str] = None       # deviceId of the trigger source
    magnitude: Optional[float] = None
    distance_km: Optional[float] = None
    intensity: Optional[str] = None

@api_router.post("/register-push", status_code=201)
async def register_push(body: RegisterPushBody):
    """Register a device native push token with the Emergent push relay,
    and remember it locally so we can broadcast alerts to every device."""
    # Store locally so we know who to fan out to
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

    event = {
        "at": datetime.now(timezone.utc).isoformat(),
        "kind": "register",
        "user_id": body.user_id,
        "platform": body.platform,
        "status_code": None,
        "response_body": None,
        "error": None,
    }
    try:
        resp = await _push_client.post(
            "/api/v1/push/users/register",
            json=body.model_dump(),
        )
        event["status_code"] = resp.status_code
        try:
            event["response_body"] = resp.text[:2000]
        except Exception:
            event["response_body"] = "<unreadable>"
        _last_push_events.appendleft(event)
        if resp.status_code == 401:
            raise HTTPException(500, "EMERGENT_PUSH_KEY missing or invalid")
        if resp.status_code >= 500:
            raise HTTPException(502, "Push provider unavailable")
        resp.raise_for_status()
    except HTTPException:
        raise
    except Exception as e:
        event["error"] = str(e)
        _last_push_events.appendleft(event)
        logging.warning(f"Push register failed (non-blocking): {e}")
    return {"status": "registered"}

async def send_push(recipients: List[str], data: dict, idempotency_key: Optional[str] = None) -> None:
    if not recipients:
        return
    if "title" not in data or "message" not in data:
        raise ValueError("data must include title and message")
    # Chunk to 100 per Emergent relay contract
    CHUNK = 100
    for i in range(0, len(recipients), CHUNK):
        chunk = recipients[i:i + CHUNK]
        payload = {"recipients": chunk, "data": data}
        if idempotency_key:
            payload["$idempotency_key"] = f"{idempotency_key}-{i // CHUNK}"

        event = {
            "at": datetime.now(timezone.utc).isoformat(),
            "kind": "trigger",
            "chunk_size": len(chunk),
            "title": data.get("title"),
            "message": data.get("message"),
            "status_code": None,
            "response_body": None,
            "error": None,
        }
        try:
            resp = await _push_client.post("/api/v1/push/trigger", json=payload)
            event["status_code"] = resp.status_code
            # Capture body (truncated) — this is where APNs error codes surface
            try:
                event["response_body"] = resp.text[:2000]
            except Exception:
                event["response_body"] = "<unreadable>"

            if resp.status_code == 401:
                event["error"] = "EMERGENT_PUSH_KEY missing or invalid"
                _last_push_events.appendleft(event)
                raise HTTPException(500, "EMERGENT_PUSH_KEY missing or invalid")
            if resp.status_code >= 500:
                event["error"] = f"upstream {resp.status_code}"
                _last_push_events.appendleft(event)
                raise HTTPException(502, "Push provider unavailable")
            resp.raise_for_status()
            _last_push_events.appendleft(event)
        except HTTPException:
            raise
        except Exception as e:
            event["error"] = str(e)
            _last_push_events.appendleft(event)
            logging.warning(f"Push trigger failed (non-blocking): {e}")

@api_router.post("/trigger-alert")
async def trigger_alert(
    body: TriggerAlertBody,
    x_admin_token: Optional[str] = Header(default=None, alias="X-Admin-Token"),
):
    """Broadcast a QuakeGuard alert to every registered device (except the
    device that triggered it, if provided). Returns count of recipients.
    Push delivery failure is logged but never blocks the response.

    Requires header `X-Admin-Token` matching the ADMIN_TRIGGER_PASSWORD env
    var. This protects the dashboard button from random visitors.
    """
    if not ADMIN_TRIGGER_PASSWORD:
        # Fail-closed: refuse to broadcast if the operator hasn't set a
        # password. Better than silently allowing anonymous triggers.
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
        logging.warning(f"trigger-alert push non-fatal: {e.detail}")
    except Exception as e:
        push_delivered = False
        push_error = str(e)
        logging.warning(f"trigger-alert push non-fatal: {e}")
    return {
        "status": "broadcast",
        "recipients": len(recipients),
        "push_delivered": push_delivered,
        "push_error": push_error,
    }

@api_router.get("/debug/devices")
async def debug_devices():
    """Diagnostic: list every device that has registered a push token.
    Returned tokens are truncated to 8 chars each so they can't be reused
    but you can still tell whether YOUR device made it into the list.
    Also reports whether EMERGENT_PUSH_KEY is a real value or the
    build-time 'placeholder'."""
    devices = await db.push_devices.find({}, {"_id": 0}).to_list(1000)
    for d in devices:
        tok = d.get("device_token", "") or ""
        d["device_token_preview"] = (tok[:8] + "…" + tok[-4:]) if len(tok) > 12 else tok
        d.pop("device_token", None)
    return {
        "device_count": len(devices),
        "push_key_status": "placeholder" if PUSH_KEY == "placeholder" else "real",
        "admin_password_configured": bool(ADMIN_TRIGGER_PASSWORD),
        "devices": devices,
    }

@api_router.post("/debug/test-push")
async def debug_test_push(
    x_admin_token: Optional[str] = Header(default=None, alias="X-Admin-Token"),
):
    """Send a plain test push to every registered device — bypasses the
    'not_responding' bookkeeping so you can isolate whether the push
    channel itself is delivering. Password-protected same as trigger-alert."""
    if not ADMIN_TRIGGER_PASSWORD:
        raise HTTPException(500, "ADMIN_TRIGGER_PASSWORD not configured on server")
    if x_admin_token != ADMIN_TRIGGER_PASSWORD:
        raise HTTPException(401, "Invalid or missing X-Admin-Token")
    return await _run_test_push()

@api_router.get("/debug/test-push", response_class=HTMLResponse)
async def debug_test_push_browser(
    token: str = Query(default=""),
):
    """Browser-friendly variant: open in a tab with ?token=<password>.
    Renders an HTML page with the same result payload so it's readable
    without curl / Postman. Query-string tokens ARE less secure than headers
    (they leak into browser history and server logs) — rotate the password
    if you use this outside a trusted operator device."""
    if not ADMIN_TRIGGER_PASSWORD:
        return HTMLResponse(
            "<h2>Server error</h2><p>ADMIN_TRIGGER_PASSWORD not configured.</p>",
            status_code=500,
        )
    if token != ADMIN_TRIGGER_PASSWORD:
        return HTMLResponse(
            "<h2 style='color:#c21818'>Wrong password.</h2>"
            "<p>Append <code>?token=&lt;password&gt;</code> to the URL.</p>",
            status_code=401,
        )
    result = await _run_test_push()
    ok = result["push_delivered"]
    color = "#1F8A3A" if ok else "#c21818"
    err_line = (
        f"<p><b>Error:</b> {result['push_error']}</p>" if result["push_error"] else ""
    )
    html = f"""<!doctype html>
<html><head><title>QuakeGuard test push</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
  body {{ font-family:-apple-system,Segoe UI,sans-serif; padding:24px; max-width:640px; margin:0 auto; }}
  .card {{ border:1px solid #ddd; border-radius:12px; padding:20px; }}
  h1 {{ font-size:22px; margin:0 0 8px; }}
  .badge {{ display:inline-block; padding:4px 10px; border-radius:999px;
           color:#fff; font-size:12px; letter-spacing:1px; font-weight:700;
           background:{color}; }}
  code {{ background:#f4f4f6; padding:2px 6px; border-radius:4px; }}
  .kv {{ margin-top:12px; font-size:14px; line-height:1.6; }}
  .kv b {{ display:inline-block; min-width:130px; color:#666; font-weight:600; }}
</style></head>
<body>
<div class="card">
  <h1>QuakeGuard test push</h1>
  <span class="badge">{"delivered" if ok else "not delivered"}</span>
  <div class="kv">
    <div><b>Recipients:</b> {result['recipients']}</div>
    <div><b>Push delivered:</b> {ok}</div>
    {err_line}
  </div>
  <p style="margin-top:20px;color:#666;font-size:13px">
    If <b>Push delivered = true</b> but no notification arrives on your device
    within ~30 seconds, the problem is between the Emergent push relay and
    Apple: either notifications are disabled for QuakeGuard in iOS Settings,
    or the APNs .p8 key uploaded during Publish is wrong / for a different
    bundle ID, or the app hasn't been foregrounded yet since install.
  </p>
</div>
</body></html>"""
    return HTMLResponse(html)

@api_router.get("/debug/last-push-events", response_class=HTMLResponse)
async def debug_last_push_events(token: str = Query(default="")):
    """Browser-friendly view of the last ~20 push-relay interactions —
    including the raw response body from the Emergent relay, which surfaces
    Apple/APNs error codes (BadDeviceToken, TopicDisallowed, etc.) that
    tell us why an accepted push never reached the device."""
    if not ADMIN_TRIGGER_PASSWORD:
        return HTMLResponse("<h2>Server error</h2>", status_code=500)
    if token != ADMIN_TRIGGER_PASSWORD:
        return HTMLResponse(
            "<h2 style='color:#c21818'>Wrong password.</h2>"
            "<p>Append <code>?token=&lt;password&gt;</code>.</p>",
            status_code=401,
        )
    events = list(_last_push_events)
    if not events:
        rows_html = (
            "<p style='color:#666'>No push events captured yet. "
            "Trigger a broadcast or hit "
            "<code>/api/debug/test-push?token=…</code> first, then reload.</p>"
        )
    else:
        rows = []
        for ev in events:
            status = ev.get("status_code")
            kind = ev.get("kind", "trigger")
            badge_color = (
                "#1F8A3A" if status and 200 <= status < 300
                else "#C21818" if status
                else "#666"
            )
            body_esc = _html.escape((ev.get("response_body") or "")[:1500])
            err_esc = _html.escape(ev.get("error") or "")
            if kind == "register":
                detail_line = (
                    f"<b>user_id:</b> {_html.escape(str(ev.get('user_id')))} · "
                    f"<b>platform:</b> {_html.escape(str(ev.get('platform')))}"
                )
            else:
                detail_line = (
                    f"<b>title:</b> {_html.escape(str(ev.get('title')))} · "
                    f"<b>recipients in chunk:</b> {ev.get('chunk_size')}"
                )
            rows.append(f"""
<div style="border:1px solid #ddd;border-radius:8px;padding:12px;margin-bottom:10px">
  <div style="display:flex;justify-content:space-between;align-items:center">
    <div>
      <span style="background:{badge_color};color:#fff;padding:2px 8px;border-radius:999px;font-size:11px;font-weight:700">{status or '—'}</span>
      <span style="background:#333;color:#fff;padding:2px 8px;border-radius:999px;font-size:11px;font-weight:700;margin-left:6px">{kind}</span>
    </div>
    <small style="color:#888">{_html.escape(ev.get('at',''))}</small>
  </div>
  <div style="margin-top:6px;font-size:12px;color:#333">{detail_line}</div>
  {f'<div style="margin-top:6px;font-size:12px;color:#C21818"><b>error:</b> {err_esc}</div>' if err_esc else ''}
  <pre style="background:#f4f4f6;padding:8px;border-radius:6px;font-size:11px;overflow:auto;max-height:220px;white-space:pre-wrap;word-break:break-all;margin-top:8px">{body_esc or '<i>(empty body)</i>'}</pre>
</div>""")
        rows_html = "".join(rows)

    html_page = f"""<!doctype html><html><head>
<title>QuakeGuard push events</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>body{{font-family:-apple-system,Segoe UI,sans-serif;padding:24px;max-width:720px;margin:0 auto;background:#fafafa}}
h1{{font-size:20px;margin:0 0 4px}} .sub{{color:#666;font-size:13px;margin-bottom:16px}}</style>
</head><body>
<h1>QuakeGuard push events</h1>
<p class="sub">Last {len(events)} interaction(s) with the Emergent push relay. Newest first. The <code>response_body</code> pre-block is the raw text the relay returned — APNs / Apple error codes surface here.</p>
{rows_html}
<p style="margin-top:24px;color:#888;font-size:12px">
  Tip: reload this page after tapping "Trigger Earthquake Alert" or hitting the test-push URL.
</p>
</body></html>"""
    return HTMLResponse(html_page)

async def _run_test_push():
    devices = await db.push_devices.find({}, {"_id": 0, "user_id": 1}).to_list(10000)
    recipients = [d["user_id"] for d in devices]
    push_delivered = True
    push_error: Optional[str] = None
    try:
        await send_push(
            recipients=recipients,
            data={
                "title": "QuakeGuard test push",
                "message": "If you see this, the push channel is working.",
                "action_url": "/",
            },
            idempotency_key=f"quake-testpush-{uuid.uuid4()}",
        )
    except HTTPException as e:
        push_delivered = False
        push_error = e.detail
    except Exception as e:
        push_delivered = False
        push_error = str(e)
    return {
        "recipients": len(recipients),
        "push_delivered": push_delivered,
        "push_error": push_error,
    }

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
