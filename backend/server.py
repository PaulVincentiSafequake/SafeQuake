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

from apns import (
    aclose as apns_aclose,
    apns_config_status,
    send_critical_alerts,
)


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
    now = datetime.now(timezone.utc).isoformat()
    await db.push_devices.update_one(
        {"user_id": body.user_id},
        {"$set": {
            "user_id": body.user_id,
            "platform": body.platform,
            "device_token": body.device_token,
            "updated_at": now,
        },
         "$setOnInsert": {"created_at": now}},
        upsert=True,
    )
    relay_status: Optional[int] = None
    relay_body = None
    relay_error: Optional[str] = None
    try:
        resp = await _push_client.post(
            "/api/v1/push/users/register",
            json=body.model_dump(),
        )
        relay_status = resp.status_code
        try:
            relay_body = resp.json()
        except Exception:
            relay_body = resp.text[:2000]
        if resp.status_code == 401:
            relay_error = "EMERGENT_PUSH_KEY missing or invalid"
            raise HTTPException(500, relay_error)
        if resp.status_code >= 500:
            relay_error = f"Push provider {resp.status_code}"
            raise HTTPException(502, "Push provider unavailable")
        if not (200 <= resp.status_code < 300):
            relay_error = f"Relay HTTP {resp.status_code}"
            logging.warning(
                f"Push register relay {resp.status_code}: {str(relay_body)[:500]}"
            )
    except HTTPException:
        raise
    except Exception as e:
        relay_error = str(e)
        logging.warning(f"Push register failed (non-blocking): {e}")
    finally:
        # Persist a diagnostic row regardless of outcome, so we can see what
        # SuprSend actually said when a specific device tried to register.
        try:
            token_len = len(body.device_token or "")
            fingerprint = None
            if token_len > 0:
                head = body.device_token[:8]
                tail = body.device_token[-8:] if token_len > 16 else ""
                fingerprint = f"{head}…{tail}" if tail else head
            await db.push_registrations_log.insert_one({
                "user_id": body.user_id,
                "platform": body.platform,
                "token_length": token_len,
                "token_fingerprint": fingerprint,
                "created_at": now,
                "relay_status": relay_status,
                "relay_body": relay_body,
                "relay_error": relay_error,
            })
        except Exception as e:
            logging.warning(f"Failed to persist push_registrations_log: {e}")
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
    devices = await db.push_devices.find(
        query,
        {"_id": 0, "user_id": 1, "platform": 1, "device_token": 1},
    ).to_list(10000)

    ios_devices = [
        d for d in devices
        if (d.get("platform") or "").lower() == "ios" and d.get("device_token")
    ]
    android_devices = [
        d for d in devices
        if (d.get("platform") or "").lower() != "ios"
    ]
    android_recipients = [d["user_id"] for d in android_devices]

    idem = f"quake-{uuid.uuid4()}"
    magnitude = body.magnitude or 6.4
    title = "EARTHQUAKE ALERT"
    message = f"Magnitude {magnitude}. Are you safe? Tap to check in."
    push_error: Optional[str] = None
    events: List[dict] = []      # Android/SuprSend chunk events
    apns_events: List[dict] = [] # iOS per-recipient APNs events

    # ---- iOS: direct APNs with true critical-alert payload ----
    try:
        apns_events = await send_critical_alerts(
            db=db,
            devices=ios_devices,
            title=title,
            body=message,
            action_url="/alert",
            idempotency_key=idem,
        )
    except Exception as e:
        push_error = f"APNs pipeline: {e}"
        logging.warning(push_error)

    # ---- Android: SuprSend relay (regular high-priority push) ----
    if android_recipients:
        try:
            events = await send_push(
                recipients=android_recipients,
                data={
                    "title": title,
                    "message": message,
                    "action_url": "/alert",
                },
                idempotency_key=idem,
            )
        except HTTPException as e:
            push_error = push_error or e.detail
        except Exception as e:
            push_error = push_error or str(e)

    ios_delivered = any(ev.get("delivered") for ev in apns_events)
    android_delivered = any(ev.get("ok") for ev in events) if events else False

    if ios_devices and not ios_delivered and apns_events:
        # Every APNs attempt failed — bubble a useful reason up.
        first = next(
            (ev.get("reason") or ev.get("error") for ev in apns_events
             if ev.get("reason") or ev.get("error")),
            None,
        )
        if first and not push_error:
            push_error = f"iOS APNs: {first}"
    if android_recipients and not android_delivered and events:
        first = next((ev.get("error") for ev in events if ev.get("error")), None)
        if first and not push_error:
            push_error = f"Android relay: {first}"

    push_delivered = ios_delivered or android_delivered or (
        len(ios_devices) == 0 and len(android_recipients) == 0
    )

    # Persist diagnostic record so /api/admin/last-push-events can show it.
    try:
        await db.push_events.insert_one({
            "idempotency_key": idem,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "triggered_by": body.triggeredBy,
            "magnitude": magnitude,
            "recipients_total": len(devices),
            "recipients_sample": [d["user_id"] for d in devices][:20],
            "ios_count": len(ios_devices),
            "android_count": len(android_recipients),
            "push_delivered": push_delivered,
            "push_error": push_error,
            "chunks": events,             # legacy field (Android)
            "apns_events": apns_events,   # new: per-recipient iOS results
        })
    except Exception as e:
        logging.warning(f"Failed to persist push_events: {e}")

    return {
        "status": "broadcast",
        "recipients": len(devices),
        "ios_count": len(ios_devices),
        "android_count": len(android_recipients),
        "push_delivered": push_delivered,
        "push_error": push_error,
        "idempotency_key": idem,
        "chunks": events,
        "apns_events": apns_events,
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

        # ---- iOS APNs per-recipient rows ----
        apns_rows = ""
        for a in ev.get("apns_events") or []:
            ok = a.get("delivered")
            code_color = "#1F8A3A" if ok else "#C21818"
            reason = a.get("reason") or a.get("error") or ""
            env_badge = a.get("environment") or "?"
            env_color = "#1F8A3A" if env_badge == "production" else ("#F0A500" if env_badge == "sandbox" else "#888")
            apns_rows += f"""
<tr>
<td style="font-family:ui-monospace,Menlo,monospace;font-size:11px">{_html.escape(str(a.get('user_id') or ''))}</td>
<td style="font-family:ui-monospace,Menlo,monospace;font-size:11px">{_html.escape(str(a.get('token_fingerprint') or ''))}</td>
<td><span style="background:{env_color};color:#fff;padding:2px 8px;border-radius:999px;font-size:11px">{_html.escape(env_badge)}</span></td>
<td style="color:{code_color};font-weight:700">{_html.escape(str(a.get('status_code') or '—'))}</td>
<td style="font-size:11px;color:#c21818">{_html.escape(str(reason))}</td>
<td style="font-size:11px;color:#666">{_html.escape(str(a.get('duration_ms') or ''))}ms</td>
</tr>"""
        apns_block = ""
        if apns_rows:
            apns_block = f"""
<div style="margin-top:12px">
  <div style="font-size:12px;color:#666;font-weight:700;text-transform:uppercase;letter-spacing:.05em;margin-bottom:6px">iOS (direct APNs)</div>
  <table style="width:100%;border-collapse:collapse;font-size:12px">
    <thead><tr style="background:#fafafa">
      <th style="text-align:left;padding:6px 8px;border-bottom:1px solid #eee">user_id</th>
      <th style="text-align:left;padding:6px 8px;border-bottom:1px solid #eee">token fp</th>
      <th style="text-align:left;padding:6px 8px;border-bottom:1px solid #eee">env</th>
      <th style="text-align:left;padding:6px 8px;border-bottom:1px solid #eee">HTTP</th>
      <th style="text-align:left;padding:6px 8px;border-bottom:1px solid #eee">reason</th>
      <th style="text-align:left;padding:6px 8px;border-bottom:1px solid #eee">time</th>
    </tr></thead>
    <tbody>{apns_rows}</tbody>
  </table>
</div>"""

        # ---- Android SuprSend chunk rows (legacy) ----
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
  <div><b>Android chunk {_html.escape(str(ch.get('chunk_index')))}</b> — status
    <span style="color:{ok_color};font-weight:700">{_html.escape(str(ch.get('status_code')))}</span>
    · {_html.escape(str(ch.get('chunk_size')))} recipient(s)
    {"· error: <code>" + _html.escape(str(ch.get('error'))) + "</code>" if ch.get("error") else ""}
  </div>
  <div style="font-size:12px;color:#666;margin-top:4px"><b>recipients:</b> {_html.escape(sample)}</div>
  <pre style="background:#0e1116;color:#d5dae0;padding:10px;border-radius:6px;font-size:11px;overflow:auto;max-height:280px;white-space:pre-wrap;word-break:break-word;margin-top:8px">{body_pretty}</pre>
</div>"""

        counts = ""
        if ev.get("ios_count") is not None or ev.get("android_count") is not None:
            counts = f" · iOS: {ev.get('ios_count') or 0} · Android: {ev.get('android_count') or 0}"

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
    <div><b>Recipients:</b> {ev.get('recipients_total')}{counts}</div>
    {f'<div><b>Error:</b> <code style="color:#c21818">{_html.escape(str(ev.get("push_error")))}</code></div>' if ev.get('push_error') else ''}
  </div>
  {apns_block}
  {chunks_html or ('<div style="color:#666;font-size:12px;margin-top:8px">No Android chunks (all iOS).</div>' if apns_rows else '<div style="color:#666;font-size:12px;margin-top:8px">No chunk events recorded.</div>')}
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

# ---------- Diagnostics: devices, registrations, self-test push ----------
def _fingerprint(token: Optional[str]) -> str:
    if not token:
        return ""
    n = len(token)
    if n <= 16:
        return _html.escape(token)
    return f"{_html.escape(token[:8])}…{_html.escape(token[-8:])}"


@api_router.get("/admin/devices", response_class=HTMLResponse)
async def devices_browser(token: str = Query(default="")):
    """List every registered device with token metadata for diagnosis."""
    if not ADMIN_TRIGGER_PASSWORD:
        return HTMLResponse("<h2>Server error</h2>", status_code=500)
    if token != ADMIN_TRIGGER_PASSWORD:
        return HTMLResponse(
            "<h2 style='color:#c21818'>Wrong password.</h2>"
            "<p>Append <code>?token=&lt;password&gt;</code>.</p>",
            status_code=401,
        )
    rows = await db.push_devices.find({}, {"_id": 0}).sort("updated_at", -1).to_list(1000)
    total = len(rows)

    def render(r: dict) -> str:
        tok = r.get("device_token") or ""
        fp = _fingerprint(tok)
        return f"""<tr>
<td style="font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px">{_html.escape(r.get('user_id') or '')}</td>
<td>{_html.escape((r.get('platform') or '').upper())}</td>
<td style="font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px">{fp}</td>
<td>{len(tok)}</td>
<td style="font-size:11px;color:#666">{_html.escape(r.get('created_at') or '')}</td>
<td style="font-size:11px;color:#666">{_html.escape(r.get('updated_at') or '')}</td>
</tr>"""

    body_html = "".join(render(r) for r in rows) or (
        "<tr><td colspan='6' style='padding:16px;color:#666'>No devices registered.</td></tr>"
    )
    return HTMLResponse(f"""<!doctype html><html><head>
<title>QuakeGuard — registered devices</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="referrer" content="no-referrer">
<style>
body{{font-family:-apple-system,Segoe UI,sans-serif;padding:20px;max-width:1000px;margin:0 auto;background:#f4f4f7}}
h1{{font-size:20px;margin:0 0 6px}}
.card{{border:1px solid #ddd;border-radius:12px;padding:16px 18px;background:#fff;margin-bottom:14px}}
table{{width:100%;border-collapse:collapse;background:#fff;border:1px solid #ddd;border-radius:8px;overflow:hidden}}
th,td{{padding:8px 10px;text-align:left;border-bottom:1px solid #eee;vertical-align:top}}
th{{background:#fafafa;font-size:12px;color:#666;font-weight:600;text-transform:uppercase;letter-spacing:.03em}}
tr:last-child td{{border-bottom:0}}
</style></head><body>
<div class="card">
  <h1>{total} registered device row(s)</h1>
  <p style="margin:0;color:#666;font-size:13px">Sorted by most recently updated. Token fingerprint = first 8 + last 8 chars. Valid iOS APNs tokens should be ~64 hex chars.</p>
</div>
<table>
<thead><tr><th>user_id</th><th>platform</th><th>token fingerprint</th><th>len</th><th>created</th><th>updated</th></tr></thead>
<tbody>{body_html}</tbody>
</table>
</body></html>""")


@api_router.get("/admin/last-registrations", response_class=HTMLResponse)
async def last_registrations_browser(
    token: str = Query(default=""),
    limit: int = Query(default=20, ge=1, le=100),
):
    """Show the last N /api/register-push calls with the raw SuprSend response."""
    if not ADMIN_TRIGGER_PASSWORD:
        return HTMLResponse("<h2>Server error</h2>", status_code=500)
    if token != ADMIN_TRIGGER_PASSWORD:
        return HTMLResponse(
            "<h2 style='color:#c21818'>Wrong password.</h2>"
            "<p>Append <code>?token=&lt;password&gt;</code>.</p>",
            status_code=401,
        )
    import json as _json
    logs = await db.push_registrations_log.find(
        {}, {"_id": 0}
    ).sort("created_at", -1).to_list(limit)

    def render(row: dict) -> str:
        ok = row.get("relay_status") and 200 <= (row.get("relay_status") or 0) < 300
        badge_color = "#1F8A3A" if ok else "#C21818"
        badge_text = f"HTTP {row.get('relay_status') or '—'}"
        body_pretty = _html.escape(
            _json.dumps(row.get("relay_body"), indent=2, default=str)
            if isinstance(row.get("relay_body"), (dict, list))
            else str(row.get("relay_body") or "")
        )
        return f"""
<div class="card">
  <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">
    <span class="badge" style="background:{badge_color}">{badge_text}</span>
    <span style="font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px;color:#666">{_html.escape(row.get('user_id') or '')}</span>
    <span style="color:#999;font-size:12px">· {_html.escape((row.get('platform') or '').upper())}</span>
  </div>
  <div class="kv" style="margin-top:8px">
    <div><b>When:</b> {_html.escape(row.get('created_at') or '')}</div>
    <div><b>Token fp:</b> <code>{_fingerprint(None) if not row.get('token_fingerprint') else _html.escape(row.get('token_fingerprint') or '')}</code> (len: {row.get('token_length')})</div>
    {f'<div><b>Error:</b> <code style="color:#c21818">{_html.escape(str(row.get("relay_error")))}</code></div>' if row.get('relay_error') else ''}
  </div>
  <pre style="background:#0e1116;color:#d5dae0;padding:10px;border-radius:6px;font-size:11px;overflow:auto;max-height:280px;white-space:pre-wrap;word-break:break-word;margin-top:8px">{body_pretty or '(empty)'}</pre>
</div>"""

    body_html = "".join(render(r) for r in logs) or (
        "<div class='card'><p style='color:#666'>No registration logs yet. Reopen the app to trigger a re-register.</p></div>"
    )
    return HTMLResponse(f"""<!doctype html><html><head>
<title>QuakeGuard — last registrations</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="referrer" content="no-referrer">
<style>body{{font-family:-apple-system,Segoe UI,sans-serif;padding:20px;max-width:760px;margin:0 auto;background:#f4f4f7}}
.card{{border:1px solid #ddd;border-radius:12px;padding:14px 16px;background:#fff;margin-bottom:14px}}
h1{{font-size:20px;margin:0 0 6px}}
.badge{{display:inline-block;padding:4px 10px;border-radius:999px;color:#fff;font-size:12px;font-weight:700}}
.kv{{font-size:14px;line-height:1.7}}
.kv b{{display:inline-block;min-width:100px;color:#666;font-weight:600}}
code{{background:#f4f4f6;padding:1px 6px;border-radius:4px;font-size:12px}}</style>
</head><body>
<div class="card">
  <h1>Last {len(logs)} registration(s)</h1>
  <p style="margin:0;color:#666;font-size:13px">Raw SuprSend response body captured per call.</p>
</div>
{body_html}
</body></html>""")


class SelfTestPushBody(BaseModel):
    user_id: str


@api_router.post("/admin/self-test-push")
async def self_test_push(
    body: SelfTestPushBody,
    x_admin_token: Optional[str] = Header(default=None, alias="X-Admin-Token"),
    token: str = Query(default=""),
):
    """Send a push to exactly one user_id. Auth via X-Admin-Token header OR
    ?token= query for browser convenience. Returns per-chunk relay events."""
    if not ADMIN_TRIGGER_PASSWORD:
        raise HTTPException(500, "ADMIN_TRIGGER_PASSWORD not configured")
    provided = x_admin_token or token
    if provided != ADMIN_TRIGGER_PASSWORD:
        raise HTTPException(401, "Invalid admin token")

    target = (body.user_id or "").strip()
    if not target:
        raise HTTPException(400, "user_id is required")
    device = await db.push_devices.find_one({"user_id": target}, {"_id": 0})
    if not device:
        raise HTTPException(404, f"No device row found for user_id={target}")

    idem = f"selftest-{uuid.uuid4()}"
    events: List[dict] = []
    push_delivered = True
    push_error: Optional[str] = None
    try:
        events = await send_push(
            recipients=[target],
            data={
                "title": "QuakeGuard self-test",
                "message": "If you see this, APNs delivery to this device is working.",
                "action_url": "/",
            },
            idempotency_key=idem,
        )
    except HTTPException as e:
        push_delivered = False
        push_error = e.detail
    except Exception as e:
        push_delivered = False
        push_error = str(e)
    if events and not any(ev.get("ok") for ev in events):
        push_delivered = False
        if not push_error:
            first_err = next((ev.get("error") for ev in events if ev.get("error")), None)
            push_error = first_err or "All chunks failed at push relay"

    try:
        await db.push_events.insert_one({
            "idempotency_key": idem,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "triggered_by": "self-test",
            "magnitude": None,
            "recipients_total": 1,
            "recipients_sample": [target],
            "push_delivered": push_delivered,
            "push_error": push_error,
            "chunks": events,
        })
    except Exception as e:
        logging.warning(f"Failed to persist self-test push_events: {e}")

    return {
        "status": "sent",
        "user_id": target,
        "device_platform": device.get("platform"),
        "device_token_fingerprint": _fingerprint(device.get("device_token")),
        "device_token_length": len(device.get("device_token") or ""),
        "push_delivered": push_delivered,
        "push_error": push_error,
        "idempotency_key": idem,
        "chunks": events,
    }


@api_router.get("/admin/self-test-push", response_class=HTMLResponse)
async def self_test_push_browser(
    token: str = Query(default=""),
    user_id: str = Query(default=""),
):
    """Browser form for firing a single-recipient test push."""
    if not ADMIN_TRIGGER_PASSWORD:
        return HTMLResponse("<h2>Server error</h2>", status_code=500)
    if token != ADMIN_TRIGGER_PASSWORD:
        return HTMLResponse(
            "<h2 style='color:#c21818'>Wrong password.</h2>"
            "<p>Append <code>?token=&lt;password&gt;</code>.</p>",
            status_code=401,
        )

    import json as _json
    result_html = ""
    if user_id.strip():
        try:
            result = await self_test_push(
                SelfTestPushBody(user_id=user_id.strip()),
                x_admin_token=ADMIN_TRIGGER_PASSWORD,
                token=ADMIN_TRIGGER_PASSWORD,
            )
            body_str = _html.escape(_json.dumps(result, indent=2, default=str))
            result_html = f'<div class="card"><h3 style="margin-top:0">Result</h3><pre style="background:#0e1116;color:#d5dae0;padding:10px;border-radius:6px;font-size:11px;overflow:auto;max-height:400px;white-space:pre-wrap;word-break:break-word">{body_str}</pre></div>'
        except HTTPException as e:
            result_html = f'<div class="card" style="border-color:#c21818"><h3 style="margin-top:0;color:#c21818">Error {e.status_code}</h3><p>{_html.escape(str(e.detail))}</p></div>'

    return HTMLResponse(f"""<!doctype html><html><head>
<title>QuakeGuard — self-test push</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="referrer" content="no-referrer">
<style>body{{font-family:-apple-system,Segoe UI,sans-serif;padding:20px;max-width:760px;margin:0 auto;background:#f4f4f7}}
.card{{border:1px solid #ddd;border-radius:12px;padding:16px;background:#fff;margin-bottom:14px}}
h1{{font-size:20px;margin:0 0 6px}}
input{{width:100%;padding:10px;border:1px solid #ccc;border-radius:8px;font-size:14px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace}}
button{{background:#c21818;color:#fff;border:0;padding:10px 20px;border-radius:8px;font-size:14px;font-weight:600;cursor:pointer;margin-top:10px}}
</style></head><body>
<div class="card">
  <h1>Self-test push</h1>
  <p style="margin:0 0 12px;color:#666;font-size:13px">Send a single-recipient push to identify whether a specific device row reaches its device. Grab the exact user_id from the Diagnostics screen in the app, or from <a href="/api/admin/devices?token={_html.escape(token)}">/admin/devices</a>.</p>
  <form method="GET" action="/api/admin/self-test-push">
    <input type="hidden" name="token" value="{_html.escape(token)}">
    <label style="font-size:12px;color:#666">user_id</label>
    <input type="text" name="user_id" value="{_html.escape(user_id)}" placeholder="qg-xxxxxxxx" autocapitalize="off" autocorrect="off">
    <button type="submit">Send test push</button>
  </form>
</div>
{result_html}
</body></html>""")


# ---------- APNs auth key status (read-only) ----------
# NOTE: The one-time upload endpoints (GET /admin/apns-key form + POST
# /admin/apns-key) have been removed after the key was successfully
# persisted, to reduce lingering attack surface. If the key ever needs to
# be rotated, restore the upload handler from git history for a single
# session, then remove it again.


@api_router.get("/admin/apns-status")
async def apns_status_json(
    x_admin_token: Optional[str] = Header(default=None, alias="X-Admin-Token"),
    token: str = Query(default=""),
):
    if (x_admin_token or token) != ADMIN_TRIGGER_PASSWORD:
        raise HTTPException(401, "Invalid admin token")
    return await apns_config_status(db)


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
    await apns_aclose()
