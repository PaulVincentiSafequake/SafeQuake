from fastapi import FastAPI, APIRouter, HTTPException, Header, Query
from fastapi.responses import HTMLResponse
from dotenv import load_dotenv
from starlette.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
import os
import logging
import httpx
import html as _html
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

async def send_push(
    recipients: List[str],
    data: dict,
    idempotency_key: Optional[str] = None,
) -> List[dict]:
    """Send a push in chunks via the Emergent (SuprSend) relay. Returns a list
    of per-chunk diagnostic events so callers can log/inspect what the relay
    (and downstream APNs/FCM) actually responded with. Never raises for 4xx
    at the relay — those are captured into the event list with ok=false."""
    events: List[dict] = []
    if not recipients:
        return events
    if "title" not in data or "message" not in data:
        raise ValueError("data must include title and message")
    CHUNK = 100
    for i in range(0, len(recipients), CHUNK):
        chunk = recipients[i:i + CHUNK]
        payload = {"recipients": chunk, "data": data}
        if idempotency_key:
            payload["$idempotency_key"] = f"{idempotency_key}-{i // CHUNK}"

        event: dict = {
            "chunk_index": i // CHUNK,
            "chunk_size": len(chunk),
            "recipients_sample": chunk[:20],
            "recipients_total": len(chunk),
            "ok": False,
            "status_code": None,
            "body": None,
            "error": None,
        }
        try:
            resp = await _push_client.post("/api/v1/push/trigger", json=payload)
            event["status_code"] = resp.status_code
            # Capture body regardless of status so we can see relay-level errors
            # like "invalid device token" or "APNs Unregistered".
            try:
                event["body"] = resp.json()
            except Exception:
                event["body"] = resp.text[:2000]
            event["ok"] = 200 <= resp.status_code < 300
            if resp.status_code == 401:
                event["error"] = "EMERGENT_PUSH_KEY missing or invalid"
                raise HTTPException(500, "EMERGENT_PUSH_KEY missing or invalid")
            if resp.status_code >= 500:
                event["error"] = f"Push provider {resp.status_code}"
                raise HTTPException(502, "Push provider unavailable")
            if not event["ok"]:
                event["error"] = f"Relay HTTP {resp.status_code}"
                logging.warning(
                    f"Push trigger relay {resp.status_code}: {str(event['body'])[:500]}"
                )
        except HTTPException:
            events.append(event)
            raise
        except Exception as e:
            event["error"] = str(e)
            logging.warning(f"Push trigger failed (non-blocking): {e}")
        events.append(event)
    return events

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
    events: List[dict] = []
    try:
        events = await send_push(
            recipients=recipients,
            data={
                "title": title,
                "message": message,
                "action_url": "/alert",
                # Critical-alert hints for iOS. Emergent's SuprSend relay
                # forwards these into the APNs aps.sound + interruption-level
                # fields. Requires the critical-alerts entitlement on the app.
                "interruption_level": "critical",
                "sound": {"critical": 1, "name": "default", "volume": 1.0},
            },
            idempotency_key=idem,
        )
    except HTTPException as e:
        push_delivered = False
        push_error = e.detail
    except Exception as e:
        push_delivered = False
        push_error = str(e)

    # If every chunk actually failed at the relay, mark the overall trigger
    # as not delivered (previous code returned true even on 400s).
    if events and not any(ev.get("ok") for ev in events):
        push_delivered = False
        if not push_error:
            first_err = next((ev.get("error") for ev in events if ev.get("error")), None)
            push_error = first_err or "All chunks failed at push relay"

    # Persist diagnostic record so /api/admin/last-push-events can show it.
    try:
        await db.push_events.insert_one({
            "idempotency_key": idem,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "triggered_by": body.triggeredBy,
            "magnitude": magnitude,
            "recipients_total": len(recipients),
            "recipients_sample": recipients[:20],
            "push_delivered": push_delivered,
            "push_error": push_error,
            "chunks": events,
        })
    except Exception as e:
        logging.warning(f"Failed to persist push_events: {e}")

    return {
        "status": "broadcast",
        "recipients": len(recipients),
        "push_delivered": push_delivered,
        "push_error": push_error,
        "idempotency_key": idem,
        "chunks": events,
    }

# ---------- Maintenance: purge leftover test / diagnostic rows ----------
async def _run_purge_test_devices() -> dict:
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

@api_router.post("/admin/purge-test-devices")
async def purge_test_devices(
    x_admin_token: Optional[str] = Header(default=None, alias="X-Admin-Token"),
):
    """Programmatic variant. Password-protected via X-Admin-Token header."""
    if not ADMIN_TRIGGER_PASSWORD:
        raise HTTPException(500, "ADMIN_TRIGGER_PASSWORD not configured on server")
    if x_admin_token != ADMIN_TRIGGER_PASSWORD:
        raise HTTPException(401, "Invalid or missing X-Admin-Token")
    return await _run_purge_test_devices()

@api_router.get("/admin/purge-test-devices", response_class=HTMLResponse)
async def purge_test_devices_browser(
    token: str = Query(default=""),
    confirm: str = Query(default=""),
):
    """Browser-openable variant. Two-step to prevent accidents when the URL
    is shared:
      /api/admin/purge-test-devices?token=<pwd>              → preview page
      /api/admin/purge-test-devices?token=<pwd>&confirm=yes  → actually delete
    """
    if not ADMIN_TRIGGER_PASSWORD:
        return HTMLResponse("<h2>Server error</h2>", status_code=500)
    if token != ADMIN_TRIGGER_PASSWORD:
        return HTMLResponse(
            "<h2 style='color:#c21818'>Wrong password.</h2>"
            "<p>Append <code>?token=&lt;password&gt;</code>.</p>",
            status_code=401,
        )

    # Preview matching rows without deleting
    filt = {
        "$or": [
            {"user_id": {"$regex": "^TEST_"}},
            {"user_id": {"$regex": "^test-"}},
            {"user_id": {"$regex": "^diag-"}},
            {"user_id": "dashboard"},
        ]
    }
    matches = await db.push_devices.find(
        filt, {"_id": 0, "user_id": 1, "platform": 1}
    ).to_list(500)
    total_before = await db.push_devices.count_documents({})

    if confirm != "yes":
        rows_html = "".join(
            f"<li><code>{_html.escape(str(m.get('user_id') or ''))}</code> "
            f"<small style='color:#888'>({_html.escape(str(m.get('platform') or '?'))})</small></li>"
            for m in matches
        ) or "<li style='color:#666;font-style:italic'>Nothing matching to delete.</li>"
        return HTMLResponse(f"""<!doctype html><html><head>
<title>Purge test devices — preview</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="referrer" content="no-referrer">
<style>body{{font-family:-apple-system,Segoe UI,sans-serif;padding:24px;max-width:640px;margin:0 auto;background:#fafafa}}
.card{{border:1px solid #ddd;border-radius:12px;padding:20px;background:#fff}}
h1{{font-size:20px;margin:0 0 8px}}
ul{{padding-left:20px;max-height:280px;overflow:auto;background:#f4f4f6;border-radius:6px;padding:12px 12px 12px 32px}}
.btn{{display:inline-block;background:#C21818;color:#fff;padding:12px 20px;border-radius:8px;
       text-decoration:none;font-weight:700;font-size:14px;margin-top:16px}}
.btn:active{{opacity:.85}}
small{{color:#888}}</style>
</head><body>
<div class="card">
<h1>Preview: {len(matches)} test row(s) will be deleted</h1>
<p><small>Matches user_ids starting with <code>TEST_</code>, <code>test-</code>, <code>diag-</code>, or exactly <code>dashboard</code>. Currently {total_before} total device rows in the DB.</small></p>
<ul>{rows_html}</ul>
<a class="btn" href="?token={_html.escape(token)}&amp;confirm=yes">Confirm delete {len(matches)} row(s)</a>
<p><small style="margin-top:14px;display:block">Tap the red button to actually purge. Just opening this URL does nothing destructive — you have to confirm.</small></p>
</div></body></html>""")

    result = await _run_purge_test_devices()
    return HTMLResponse(f"""<!doctype html><html><head>
<title>Purged</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="referrer" content="no-referrer">
<style>body{{font-family:-apple-system,Segoe UI,sans-serif;padding:24px;max-width:640px;margin:0 auto;background:#fafafa}}
.card{{border:1px solid #ddd;border-radius:12px;padding:20px;background:#fff}}
h1{{font-size:20px;margin:0 0 8px}}
.badge{{display:inline-block;padding:4px 10px;border-radius:999px;color:#fff;font-size:12px;font-weight:700;background:#1F8A3A}}
.kv{{margin-top:14px;font-size:14px;line-height:1.7}}
.kv b{{display:inline-block;min-width:110px;color:#666;font-weight:600}}</style>
</head><body>
<div class="card">
<h1>Purged</h1>
<span class="badge">done</span>
<div class="kv">
<div><b>Deleted:</b> {result['deleted']}</div>
<div><b>Remaining:</b> {result['remaining']} real device row(s)</div>
</div>
</div></body></html>""")

# ---------- Diagnostics: view last N push relay responses ----------
import json as _json

@api_router.get("/admin/last-push-events", response_class=HTMLResponse)
async def last_push_events_browser(
    token: str = Query(default=""),
    limit: int = Query(default=10, ge=1, le=50),
):
    """Browser-viewable diagnostic. Renders the most recent /api/trigger-alert
    attempts with the full raw SuprSend response body per chunk, so we can
    tell whether APNs accepted or rejected each push."""
    if not ADMIN_TRIGGER_PASSWORD:
        return HTMLResponse("<h2>Server error</h2>", status_code=500)
    if token != ADMIN_TRIGGER_PASSWORD:
        return HTMLResponse(
            "<h2 style='color:#c21818'>Wrong password.</h2>"
            "<p>Append <code>?token=&lt;password&gt;</code>.</p>",
            status_code=401,
        )

    events = await db.push_events.find(
        {}, {"_id": 0}
    ).sort("created_at", -1).to_list(limit)

    def render_event(ev: dict) -> str:
        delivered = ev.get("push_delivered")
        badge_color = "#1F8A3A" if delivered else "#C21818"
        badge_text = "delivered" if delivered else "failed"
        chunks_html = ""
        for ch in ev.get("chunks") or []:
            ok = ch.get("ok")
            ok_color = "#1F8A3A" if ok else "#C21818"
            body_pretty = _html.escape(
                _json.dumps(ch.get("body"), indent=2, default=str)
                if isinstance(ch.get("body"), (dict, list))
                else str(ch.get("body") or "")
            )
            sample = ", ".join(ch.get("recipients_sample") or [])
            if len(ch.get("recipients_sample") or []) < (ch.get("chunk_size") or 0):
                sample += f" …(+{(ch.get('chunk_size') or 0) - len(ch.get('recipients_sample') or [])} more)"
            chunks_html += f"""
<div style="border:1px solid #eee;border-radius:8px;padding:12px;margin-top:10px;background:#fbfbfd">
  <div><b>chunk {_html.escape(str(ch.get('chunk_index')))}</b> — status
    <span style="color:{ok_color};font-weight:700">{_html.escape(str(ch.get('status_code')))}</span>
    · {_html.escape(str(ch.get('chunk_size')))} recipient(s)
    {"· error: <code>" + _html.escape(str(ch.get('error'))) + "</code>" if ch.get("error") else ""}
  </div>
  <div style="font-size:12px;color:#666;margin-top:4px"><b>recipients:</b> {_html.escape(sample)}</div>
  <pre style="background:#0e1116;color:#d5dae0;padding:10px;border-radius:6px;font-size:11px;overflow:auto;max-height:280px;white-space:pre-wrap;word-break:break-word;margin-top:8px">{body_pretty}</pre>
</div>"""
        return f"""
<div class="card" style="margin-top:14px">
  <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
    <span class="badge" style="background:{badge_color}">{badge_text}</span>
    <span style="font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px;color:#666">{_html.escape(str(ev.get('idempotency_key') or ''))}</span>
  </div>
  <div class="kv" style="margin-top:8px">
    <div><b>When:</b> {_html.escape(str(ev.get('created_at') or ''))}</div>
    <div><b>Triggered by:</b> <code>{_html.escape(str(ev.get('triggered_by') or 'dashboard'))}</code></div>
    <div><b>Magnitude:</b> {_html.escape(str(ev.get('magnitude') or ''))}</div>
    <div><b>Recipients:</b> {ev.get('recipients_total')}</div>
    {f'<div><b>Error:</b> <code style="color:#c21818">{_html.escape(str(ev.get("push_error")))}</code></div>' if ev.get('push_error') else ''}
  </div>
  {chunks_html or '<div style="color:#666;font-size:12px;margin-top:8px">No chunk events recorded.</div>'}
</div>"""

    if not events:
        body_html = "<div class='card'><p style='color:#666'>No push events recorded yet. Trigger an alert first.</p></div>"
    else:
        body_html = "".join(render_event(ev) for ev in events)

    return HTMLResponse(f"""<!doctype html><html><head>
<title>QuakeGuard — last push events</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="referrer" content="no-referrer">
<style>body{{font-family:-apple-system,Segoe UI,sans-serif;padding:24px;max-width:760px;margin:0 auto;background:#f4f4f7}}
.card{{border:1px solid #ddd;border-radius:12px;padding:16px 18px;background:#fff}}
h1{{font-size:20px;margin:0 0 6px}}
.badge{{display:inline-block;padding:4px 10px;border-radius:999px;color:#fff;font-size:12px;font-weight:700}}
.kv{{font-size:14px;line-height:1.7}}
.kv b{{display:inline-block;min-width:110px;color:#666;font-weight:600}}
code{{background:#f4f4f6;padding:1px 6px;border-radius:4px;font-size:12px}}</style>
</head><body>
<div class="card">
  <h1>Last {len(events)} push event(s)</h1>
  <p style="margin:0;color:#666;font-size:13px">Most recent first. Raw SuprSend/APNs response per chunk is shown below each event.</p>
</div>
{body_html}
</body></html>""")

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
