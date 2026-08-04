from fastapi import FastAPI, APIRouter, HTTPException, Header, Query, Body, Request
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

# ---------- CORS allowlist (single source of truth) ----------
# Both the CORSMiddleware wire-up at the bottom of this file AND the
# /api/cors-debug endpoint read from these constants. If you change one and
# forget the other, the debug endpoint will call it out on next hit.
CORS_ALLOWED_ORIGINS: List[str] = [
    # Original Render-hosted dashboard.
    "https://safequake.onrender.com",
    # New custom-domain dashboard (multi-city path style).
    "https://malta.quakeangel.app",
    # Root domain — reserved for a future landing page / redirector.
    "https://quakeangel.app",
    "https://www.quakeangel.app",
]
# Any subdomain of quakeangel.app (e.g. london.quakeangel.app, tokyo.quakeangel.app),
# plus localhost on any port for dashboard dev (Vite 5173, CRA 3000, etc.).
CORS_ALLOWED_ORIGIN_REGEX = r"^(http://localhost:\d+|https://[a-z0-9-]+\.quakeangel\.app)$"

# ---------- Deploy fingerprint ----------
# Computed once at process start. Cheap way to tell whether the running
# instance actually reloaded this file — useful when the dashboard swears
# it's talking to the "new" backend but CORS says otherwise.
import hashlib as _hashlib
import re as _re
try:
    _server_py_bytes = (ROOT_DIR / "server.py").read_bytes()
    _SERVER_PY_SHA256 = _hashlib.sha256(_server_py_bytes).hexdigest()[:12]
    _SERVER_PY_MTIME = datetime.fromtimestamp(
        (ROOT_DIR / "server.py").stat().st_mtime, tz=timezone.utc
    ).isoformat()
    _SERVER_PY_LINES = _server_py_bytes.count(b"\n") + 1
except Exception:
    _SERVER_PY_SHA256 = "unknown"
    _SERVER_PY_MTIME = "unknown"
    _SERVER_PY_LINES = 0
_PROCESS_STARTED_AT = datetime.now(timezone.utc).isoformat()

app = FastAPI()
api_router = APIRouter(prefix="/api")

# ---------- Legacy status-check demo endpoints ----------
class StatusCheck(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    client_name: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

@api_router.get("/")
async def root():
    return {"message": "Hello World"}


# ---------- Device check-in status + dashboard read endpoint ----------
# Accepts the same payload the app has been posting to
# https://safequake.onrender.com/api/status (mobile now dual-posts to both).
# Upserts into `device_status` collection so the dashboard can fetch real
# device state via GET /api/devices.

class LocationPayload(BaseModel):
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    accuracy: Optional[float] = None
    error: Optional[str] = None

class BatteryPayload(BaseModel):
    level: Optional[float] = None      # 0..1
    state: Optional[str] = None        # charging | full | unplugged | unknown

class StatusInPayload(BaseModel):
    # Permissive: accepts everything the app sends today plus new triage fields.
    # Extra unknown keys are ignored, so this is forward-compatible.
    model_config = {"extra": "ignore"}

    # Device identity — accept both `deviceId` (app format) and `device_id`.
    deviceId: Optional[str] = None
    device_id: Optional[str] = None

    # Status + triage
    status: str = Field(pattern=r"^(safe|trapped|not_responding)$")
    severity: Optional[str] = Field(default=None, pattern=r"^(green|yellow|red)$")
    # Mobility follow-up captured after severity for `trapped` check-ins.
    #   "mobile"  → user can move themselves out of danger
    #   "trapped" → user is pinned/under debris and cannot move
    # Ignored (nulled) by the normalizer for any non-trapped status.
    mobility: Optional[str] = Field(default=None, pattern=r"^(mobile|trapped)$")

    # Optional first name a responder should see next to the pin. Asked once
    # at first app launch and editable at any time from the main screen.
    # Sanitized in _normalize_status_payload (trimmed, control chars stripped,
    # max 40 chars) so mongo receives a clean value regardless of client bugs.
    # Empty / whitespace-only is normalized to None so the dashboard falls
    # back to short_code-only display, matching pre-rollout behavior.
    display_name: Optional[str] = Field(default=None, max_length=200)

    # Structured shapes
    location: Optional[LocationPayload] = None
    battery: Optional[BatteryPayload] = None

    # Flat aliases (the app fans lat/lng across multiple field names)
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    lat: Optional[float] = None
    lng: Optional[float] = None
    lon: Optional[float] = None
    accuracy: Optional[float] = None
    batteryLevel: Optional[float] = None
    batteryState: Optional[str] = None

    client_name: Optional[str] = None
    timestamp: Optional[str] = None


def _short_code(device_id: Optional[str]) -> Optional[str]:
    """Rescuer-facing tie-breaker code. Last 5 chars of the device_id,
    uppercased. Not unique globally — it exists ONLY to disambiguate 2-3
    victim pins already narrowed down by GPS proximity in the field.

    Returns None when device_id is missing / too short to be meaningful.
    """
    if not device_id:
        return None
    tail = str(device_id)[-5:]
    if len(tail) < 3:
        return None
    return tail.upper()


def _sanitize_display_name(raw) -> Optional[str]:
    """Clean a user-supplied first name for storage.

    - Trims whitespace.
    - Strips ASCII control chars (keeps unicode letters like é / ñ / 京).
    - Caps at 40 chars (post-strip) so the dashboard sidebar can render it
      inline without wrapping oddly.
    - Empty / whitespace-only / non-str → None (dashboard falls back to
      short_code-only, matching pre-rollout behavior).
    """
    if raw is None:
        return None
    if not isinstance(raw, str):
        try:
            raw = str(raw)
        except Exception:
            return None
    # Drop ASCII control chars (0x00-0x1F and 0x7F). Keeps unicode letters
    # so names like "José", "Aiko", "李" pass through untouched.
    cleaned = "".join(ch for ch in raw if 32 <= ord(ch) < 127 or ord(ch) >= 128)
    cleaned = cleaned.strip()
    if not cleaned:
        return None
    if len(cleaned) > 40:
        cleaned = cleaned[:40].rstrip()
    return cleaned or None


def _normalize_status_payload(p: StatusInPayload) -> dict:
    """Turn a permissive incoming payload into the canonical device_status doc."""
    device_id = (p.device_id or p.deviceId or "").strip()

    # Location — prefer the structured object, then any flat alias.
    lat = None
    lng = None
    acc = None
    loc_error = None
    if p.location is not None:
        lat = p.location.latitude
        lng = p.location.longitude
        acc = p.location.accuracy
        loc_error = p.location.error
    lat = lat if lat is not None else p.latitude
    lat = lat if lat is not None else p.lat
    lng = lng if lng is not None else p.longitude
    lng = lng if lng is not None else p.lng
    lng = lng if lng is not None else p.lon
    acc = acc if acc is not None else p.accuracy

    # Battery — 0..1 in wire, 0..100 in canonical.
    level_01 = None
    state = None
    if p.battery is not None:
        level_01 = p.battery.level
        state = p.battery.state
    if level_01 is None:
        level_01 = p.batteryLevel
    if state is None:
        state = p.batteryState
    battery_pct: Optional[int] = None
    if isinstance(level_01, (int, float)):
        try:
            battery_pct = max(0, min(100, int(round(level_01 * 100))))
        except Exception:
            battery_pct = None

    # Enforce: severity may only be set when status is 'trapped'.
    severity: Optional[str] = p.severity
    if p.status != "trapped":
        severity = None

    # Mobility is likewise only meaningful for 'trapped' reports.
    mobility: Optional[str] = p.mobility
    if p.status != "trapped":
        mobility = None

    return {
        "device_id": device_id,
        "status": p.status,
        "severity": severity,
        "mobility": mobility,
        "display_name": _sanitize_display_name(p.display_name),
        "latitude": lat,
        "longitude": lng,
        "accuracy_m": acc,
        "battery_pct": battery_pct,
        "battery_state": state,
        "location_error": loc_error,
    }


@api_router.post("/status")
async def post_status(payload: StatusInPayload):
    doc = _normalize_status_payload(payload)
    if not doc["device_id"]:
        raise HTTPException(400, "deviceId (or device_id) is required")
    now = datetime.now(timezone.utc).isoformat()
    doc["updated_at"] = now
    # Enrich with platform from push_devices if we know it, so the dashboard
    # can render iOS vs Android without another lookup.
    dev = await db.push_devices.find_one(
        {"user_id": doc["device_id"]}, {"_id": 0, "platform": 1},
    )
    doc["platform"] = (dev or {}).get("platform")
    # Upsert latest state.
    await db.device_status.update_one(
        {"device_id": doc["device_id"]},
        {"$set": doc, "$setOnInsert": {"created_at": now}},
        upsert=True,
    )
    # Append immutable history row for the audit log. `device_status` only
    # holds the LATEST state; `status_events` is the append-only ledger.
    try:
        await db.status_events.insert_one({
            **doc,
            "recorded_at": now,
        })
    except Exception as e:
        logging.warning(f"Failed to append status_events: {e}")
    return {"status": "ok", "device_id": doc["device_id"], "updated_at": now}


@api_router.get("/devices")
async def get_devices(
    since: Optional[str] = Query(
        default=None,
        description="ISO-8601 timestamp. Only return devices updated on/after this instant.",
    ),
    limit: int = Query(default=1000, ge=1, le=5000),
):
    """Return every known device's latest state for the rescuer dashboard.

    CORS is limited to https://safequake.onrender.com,
    https://*.quakeangel.app (any subdomain), and http://localhost:*
    (see middleware config below). Response is snake_case, null-safe, and
    stable — field names will not change after this ship.
    """
    query: dict = {}
    if since:
        query["updated_at"] = {"$gte": since}

    rows = await db.device_status.find(query, {"_id": 0}).sort("updated_at", -1).to_list(limit)

    def clean(r: dict) -> dict:
        return {
            "device_id": r.get("device_id"),
            # short_code is derived on read, not stored — that way any change
            # to the algorithm (e.g. hash-based instead of tail) applies to
            # existing rows without a migration.
            "short_code": _short_code(r.get("device_id")),
            # Optional first name captured at first app launch. Nullable —
            # dashboards should render "NAME · CODE" when present and fall
            # back to "CODE" alone when not, so pre-rollout devices without
            # a name still work.
            "display_name": r.get("display_name"),
            "status": r.get("status") or "unknown",
            "severity": r.get("severity"),
            "mobility": r.get("mobility"),
            "latitude": r.get("latitude"),
            "longitude": r.get("longitude"),
            "accuracy_m": r.get("accuracy_m"),
            "battery_pct": r.get("battery_pct"),
            "battery_state": r.get("battery_state"),
            "platform": r.get("platform"),
            "updated_at": r.get("updated_at"),
            # Rescue fields — set when a dashboard operator marks a case
            # closed via POST /api/mark-rescued. Distinct from `safe` (which
            # is self-reported by the user via the mobile app). The
            # `pre_rescue_*` fields let the dashboard offer an Undo that
            # restores the exact prior triage state.
            "rescued_at": r.get("rescued_at"),
            "rescued_by": r.get("rescued_by"),
            "pre_rescue_status": r.get("pre_rescue_status"),
            "pre_rescue_severity": r.get("pre_rescue_severity"),
            "pre_rescue_mobility": r.get("pre_rescue_mobility"),
        }

    return {
        "count": len(rows),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "devices": [clean(r) for r in rows],
    }


# ---------- Legacy status-check demo endpoint (unused; kept for compat) ----------
@api_router.get("/status", response_model=List[StatusCheck])
async def get_status_checks():
    status_checks = await db.status_checks.find({}, {"_id": 0}).to_list(1000)
    return [StatusCheck(**s) for s in status_checks]


# ---------- Audit log (unified trigger + status feed) ----------
@api_router.get("/audit")
async def get_audit_log(
    limit: int = Query(default=100, ge=1, le=500),
    since: Optional[str] = Query(
        default=None,
        description="ISO-8601 timestamp — only include events on/after this instant.",
    ),
    kind: Optional[str] = Query(
        default=None,
        description="Filter to a single event kind: 'trigger', 'status', 'rescued', or 'rescue_reverted'.",
    ),
):
    """Unified, dashboard-facing audit feed.

    Interleaves four event kinds by timestamp, most recent first:
      - `trigger`:          an alert was broadcast (from push_events)
      - `status`:           a device self-reported a status change (from
                            status_events with status ∈ safe/trapped/not_responding)
      - `rescued`:          a dashboard operator marked a trapped case as
                            found & safe (from status_events with status='rescued')
      - `rescue_reverted`:  the rescued mark was undone (from status_events
                            with rescue_reverted=True)

    CORS is limited to https://safequake.onrender.com,
    https://*.quakeangel.app (any subdomain), and http://localhost:*
    (see middleware config). Field names are stable snake_case.
    """
    events: List[dict] = []

    # ---- Trigger events ----
    if kind in (None, "trigger"):
        tq: dict = {}
        if since:
            tq["created_at"] = {"$gte": since}
        rows = await db.push_events.find(tq, {"_id": 0}).sort("created_at", -1).to_list(limit)
        for r in rows:
            events.append({
                "kind": "trigger",
                "at": r.get("created_at"),
                "idempotency_key": r.get("idempotency_key"),
                "triggered_by": r.get("triggered_by") or "dashboard",
                "magnitude": r.get("magnitude"),
                "recipients_total": r.get("recipients_total") or 0,
                "ios_count": r.get("ios_count") or 0,
                "android_count": r.get("android_count") or 0,
                "delivered": bool(r.get("push_delivered")),
                "error": r.get("push_error"),
            })

    # ---- Status / rescue events (all live in status_events) ----
    # We do a single query and classify each row into one of three kinds
    # based on its shape. This keeps the ledger single-source-of-truth and
    # avoids a proliferation of collections.
    want_status = kind in (None, "status")
    want_rescued = kind in (None, "rescued")
    want_reverted = kind in (None, "rescue_reverted")
    if want_status or want_rescued or want_reverted:
        sq: dict = {}
        if since:
            sq["recorded_at"] = {"$gte": since}
        rows = await db.status_events.find(sq, {"_id": 0}).sort("recorded_at", -1).to_list(limit)
        for r in rows:
            base = {
                "at": r.get("recorded_at") or r.get("updated_at"),
                "device_id": r.get("device_id"),
                # Same derivation as /api/devices so the dashboard sees a
                # single stable identifier scheme across both feeds.
                "short_code": _short_code(r.get("device_id")),
                # Snapshotted at the moment the event was recorded, so an
                # old audit row keeps its historical name even if the user
                # later changes it in settings.
                "display_name": r.get("display_name"),
                "status": r.get("status"),
                "severity": r.get("severity"),
                "mobility": r.get("mobility"),
                "latitude": r.get("latitude"),
                "longitude": r.get("longitude"),
                "accuracy_m": r.get("accuracy_m"),
                "battery_pct": r.get("battery_pct"),
                "battery_state": r.get("battery_state"),
                "platform": r.get("platform"),
            }
            if r.get("rescue_reverted"):
                if not want_reverted:
                    continue
                events.append({
                    **base,
                    "kind": "rescue_reverted",
                    "reverted_by": r.get("reverted_by") or "dashboard",
                    "restored_status": r.get("status"),
                    "restored_severity": r.get("severity"),
                    "restored_mobility": r.get("mobility"),
                })
            elif r.get("status") == "rescued":
                if not want_rescued:
                    continue
                events.append({
                    **base,
                    "kind": "rescued",
                    "rescued_by": r.get("rescued_by") or "dashboard",
                    "notes": r.get("notes"),
                    "prior_status": r.get("prior_status"),
                    "prior_severity": r.get("prior_severity"),
                    "prior_mobility": r.get("prior_mobility"),
                })
            else:
                if not want_status:
                    continue
                events.append({**base, "kind": "status"})

    # Merge and clip.
    events.sort(key=lambda e: e.get("at") or "", reverse=True)
    events = events[:limit]

    return {
        "count": len(events),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "events": events,
    }


@api_router.get("/admin/audit-log", response_class=HTMLResponse)
async def audit_log_browser(
    token: str = Query(default=""),
    limit: int = Query(default=100, ge=1, le=500),
):
    """Browser-viewable version of /api/audit for quick inspection in Safari."""
    if not ADMIN_TRIGGER_PASSWORD:
        return HTMLResponse("<h2>Server error</h2>", status_code=500)
    if token != ADMIN_TRIGGER_PASSWORD:
        return HTMLResponse(
            "<h2 style='color:#c21818'>Wrong password.</h2>"
            "<p>Append <code>?token=&lt;password&gt;</code>.</p>",
            status_code=401,
        )
    feed = await get_audit_log(limit=limit, since=None, kind=None)  # type: ignore[arg-type]

    def sev_color(s: Optional[str]) -> str:
        return {"red": "#C21818", "yellow": "#EA9500", "green": "#2E7D32"}.get(s or "", "#666")

    def row_html(e: dict) -> str:
        at = _html.escape(str(e.get("at") or ""))
        if e.get("kind") == "trigger":
            delivered = e.get("delivered")
            badge = f'<span style="background:{"#1F8A3A" if delivered else "#C21818"};color:#fff;padding:2px 8px;border-radius:999px;font-size:11px;font-weight:700">{"delivered" if delivered else "FAILED"}</span>'
            body = (
                f'<b>TRIGGER</b> · magnitude {_html.escape(str(e.get("magnitude") or "?"))} · '
                f'{e.get("recipients_total") or 0} devices (iOS: {e.get("ios_count") or 0}, Android: {e.get("android_count") or 0}) '
                f'· by <code>{_html.escape(str(e.get("triggered_by") or ""))}</code>'
            )
            err = e.get("error")
            error_html = f'<div style="color:#c21818;font-size:12px;margin-top:4px"><b>Error:</b> {_html.escape(str(err))}</div>' if err else ""
            return f'<div class="row trigger">{badge}<div class="body">{body}{error_html}<div class="at">{at}</div></div></div>'
        elif e.get("kind") == "rescued":
            rescued_by = _html.escape(str(e.get("rescued_by") or "dashboard"))
            prior = e.get("prior_status") or "trapped"
            prior_sev = e.get("prior_severity")
            prior_badge = f' <span style="background:{sev_color(prior_sev)};color:#fff;padding:2px 6px;border-radius:999px;font-size:11px;font-weight:700">{_html.escape(prior_sev)}</span>' if prior_sev else ""
            notes_html = ""
            if e.get("notes"):
                notes_html = f'<div style="color:#555;font-size:12px;margin-top:4px">📝 {_html.escape(str(e.get("notes")))}</div>'
            body = (
                f'<b>RESCUED</b> ✅ · <code>{_html.escape(str(e.get("device_id") or ""))}</code> '
                f'was <b>{_html.escape(str(prior))}</b>{prior_badge} · closed by <code>{rescued_by}</code>'
            )
            return f'<div class="row rescued">{body}{notes_html}<div class="at">{at}</div></div>'
        elif e.get("kind") == "rescue_reverted":
            reverted_by = _html.escape(str(e.get("reverted_by") or "dashboard"))
            body = (
                f'<b>RESCUE REVERTED</b> ↩️ · <code>{_html.escape(str(e.get("device_id") or ""))}</code> '
                f'restored to <b>{_html.escape(str(e.get("status") or ""))}</b> · by <code>{reverted_by}</code>'
            )
            return f'<div class="row reverted">{body}<div class="at">{at}</div></div>'
        else:
            sev = e.get("severity")
            sev_badge = f'<span style="background:{sev_color(sev)};color:#fff;padding:2px 8px;border-radius:999px;font-size:11px;font-weight:700;margin-left:6px">{_html.escape(sev)}</span>' if sev else ""
            mob = e.get("mobility")
            mob_badge = ""
            if mob:
                mob_color = "#C21818" if mob == "trapped" else "#2E7D32"
                mob_label = "trapped/pinned" if mob == "trapped" else "can move"
                mob_badge = f'<span style="background:{mob_color};color:#fff;padding:2px 8px;border-radius:999px;font-size:11px;font-weight:700;margin-left:6px">{mob_label}</span>'
            loc = ""
            if e.get("latitude") is not None and e.get("longitude") is not None:
                loc = f' · <a href="https://www.google.com/maps/place/{e.get("latitude")},{e.get("longitude")}" target="_blank" rel="noopener">📍 map</a>'
            bat = ""
            if e.get("battery_pct") is not None:
                bat = f' · 🔋 {_html.escape(str(e.get("battery_pct")))}%'
            body = (
                f'<b>STATUS</b> · <code>{_html.escape(str(e.get("device_id") or ""))}</code> → '
                f'<b>{_html.escape(str(e.get("status") or ""))}</b>{sev_badge}{mob_badge}{loc}{bat}'
            )
            return f'<div class="row status">{body}<div class="at">{at}</div></div>'

    rows_html = "\n".join(row_html(e) for e in feed["events"]) or (
        '<p style="color:#666">No events yet.</p>'
    )
    return HTMLResponse(f"""<!doctype html><html><head>
<title>Quake Angel — audit log</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="referrer" content="no-referrer">
<style>
body{{font-family:-apple-system,Segoe UI,sans-serif;padding:20px;max-width:820px;margin:0 auto;background:#f4f4f7}}
.card{{border:1px solid #ddd;border-radius:12px;padding:14px 16px;background:#fff;margin-bottom:14px}}
h1{{font-size:20px;margin:0 0 6px}}
.row{{background:#fff;border:1px solid #e6e6ea;border-left-width:4px;border-radius:8px;padding:10px 14px;margin-bottom:8px;display:flex;gap:10px;align-items:flex-start}}
.row.trigger{{border-left-color:#c21818}}
.row.status{{border-left-color:#4a90e2}}
.row.rescued{{border-left-color:#1f8a3a;background:#f2fbf5}}
.row.reverted{{border-left-color:#EA9500;background:#fff8ec}}
.row .body{{flex:1}}
.row .at{{color:#888;font-size:11px;margin-top:4px;font-family:ui-monospace,Menlo,monospace}}
code{{background:#f4f4f6;padding:1px 6px;border-radius:4px;font-size:12px;font-family:ui-monospace,Menlo,monospace}}
</style></head><body>
<div class="card">
  <h1>Audit log · last {feed["count"]} event(s)</h1>
  <p style="margin:0;color:#666;font-size:13px">Interleaved feed of dashboard triggers and device status changes. Most recent first.</p>
</div>
{rows_html}
</body></html>""")

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

@api_router.post("/mark-rescued")
async def mark_rescued(
    payload: dict = Body(...),
    x_admin_token: Optional[str] = Header(default=None, alias="X-Admin-Token"),
):
    """Dashboard operator marks a trapped case as physically found & safe.

    Distinct from a `safe` self-report — this is a responder attestation.
    The prior triage state (status/severity/mobility) is snapshotted onto
    the device doc as `pre_rescue_*` so an Undo can restore the exact
    situation the operator saw before clicking.

    Auth: header `X-Admin-Token` matching ADMIN_TRIGGER_PASSWORD.

    Body: `{"deviceId": "qg-...", "notes": "optional freeform text"}`
    """
    if not ADMIN_TRIGGER_PASSWORD:
        raise HTTPException(500, "ADMIN_TRIGGER_PASSWORD not configured on server")
    if x_admin_token != ADMIN_TRIGGER_PASSWORD:
        raise HTTPException(401, "Invalid or missing X-Admin-Token")

    device_id = str(payload.get("deviceId") or payload.get("device_id") or "").strip()
    if not device_id:
        raise HTTPException(400, "deviceId is required")
    notes = payload.get("notes")
    rescued_by = str(payload.get("rescued_by") or "dashboard").strip() or "dashboard"

    # Find the current row so we can snapshot prior state before overwriting.
    current = await db.device_status.find_one({"device_id": device_id}, {"_id": 0})
    if not current:
        raise HTTPException(404, f"Unknown device_id: {device_id}")

    # Idempotent: if already rescued, don't overwrite the original prior-state
    # snapshot — return the existing doc.
    now = datetime.now(timezone.utc).isoformat()
    if current.get("status") == "rescued":
        return {
            "status": "ok",
            "already_rescued": True,
            "device_id": device_id,
            "rescued_at": current.get("rescued_at"),
            "rescued_by": current.get("rescued_by"),
        }

    prior_status = current.get("status")
    prior_severity = current.get("severity")
    prior_mobility = current.get("mobility")

    update_doc = {
        "status": "rescued",
        "severity": None,
        "mobility": None,
        "rescued_at": now,
        "rescued_by": rescued_by,
        "pre_rescue_status": prior_status,
        "pre_rescue_severity": prior_severity,
        "pre_rescue_mobility": prior_mobility,
        "updated_at": now,
    }
    await db.device_status.update_one(
        {"device_id": device_id},
        {"$set": update_doc},
    )

    # Append immutable audit trail entry.
    try:
        await db.status_events.insert_one({
            "device_id": device_id,
            "status": "rescued",
            "severity": None,
            "mobility": None,
            "display_name": current.get("display_name"),
            "latitude": current.get("latitude"),
            "longitude": current.get("longitude"),
            "accuracy_m": current.get("accuracy_m"),
            "battery_pct": current.get("battery_pct"),
            "battery_state": current.get("battery_state"),
            "platform": current.get("platform"),
            "rescued_by": rescued_by,
            "notes": notes,
            "prior_status": prior_status,
            "prior_severity": prior_severity,
            "prior_mobility": prior_mobility,
            "recorded_at": now,
        })
    except Exception as e:
        logging.warning(f"Failed to append rescue status_events: {e}")

    return {
        "status": "ok",
        "device_id": device_id,
        "rescued_at": now,
        "rescued_by": rescued_by,
        "prior_status": prior_status,
        "prior_severity": prior_severity,
        "prior_mobility": prior_mobility,
    }


@api_router.post("/unmark-rescued")
async def unmark_rescued(
    payload: dict = Body(...),
    x_admin_token: Optional[str] = Header(default=None, alias="X-Admin-Token"),
):
    """Undo a mark-rescued — restores the exact prior triage state from
    `pre_rescue_*` snapshot. Use this to recover from a mis-click.

    Auth: header `X-Admin-Token` matching ADMIN_TRIGGER_PASSWORD.
    """
    if not ADMIN_TRIGGER_PASSWORD:
        raise HTTPException(500, "ADMIN_TRIGGER_PASSWORD not configured on server")
    if x_admin_token != ADMIN_TRIGGER_PASSWORD:
        raise HTTPException(401, "Invalid or missing X-Admin-Token")

    device_id = str(payload.get("deviceId") or payload.get("device_id") or "").strip()
    if not device_id:
        raise HTTPException(400, "deviceId is required")
    reverted_by = str(payload.get("reverted_by") or "dashboard").strip() or "dashboard"

    current = await db.device_status.find_one({"device_id": device_id}, {"_id": 0})
    if not current:
        raise HTTPException(404, f"Unknown device_id: {device_id}")
    if current.get("status") != "rescued":
        raise HTTPException(
            409, f"device is not rescued (current status: {current.get('status')})"
        )

    # Restore snapshot. Default fall-back is 'not_responding' — if we don't
    # know the prior status, that's the most conservative option (keeps
    # the pin visible on the dashboard).
    restored_status = current.get("pre_rescue_status") or "not_responding"
    restored_severity = current.get("pre_rescue_severity")
    restored_mobility = current.get("pre_rescue_mobility")
    now = datetime.now(timezone.utc).isoformat()

    await db.device_status.update_one(
        {"device_id": device_id},
        {
            "$set": {
                "status": restored_status,
                "severity": restored_severity,
                "mobility": restored_mobility,
                "updated_at": now,
            },
            "$unset": {
                "rescued_at": "",
                "rescued_by": "",
                "pre_rescue_status": "",
                "pre_rescue_severity": "",
                "pre_rescue_mobility": "",
            },
        },
    )

    try:
        await db.status_events.insert_one({
            "device_id": device_id,
            "status": restored_status,
            "severity": restored_severity,
            "mobility": restored_mobility,
            "display_name": current.get("display_name"),
            "latitude": current.get("latitude"),
            "longitude": current.get("longitude"),
            "accuracy_m": current.get("accuracy_m"),
            "battery_pct": current.get("battery_pct"),
            "battery_state": current.get("battery_state"),
            "platform": current.get("platform"),
            "rescue_reverted": True,
            "reverted_by": reverted_by,
            "recorded_at": now,
        })
    except Exception as e:
        logging.warning(f"Failed to append revert status_events: {e}")

    return {
        "status": "ok",
        "device_id": device_id,
        "restored_status": restored_status,
        "restored_severity": restored_severity,
        "restored_mobility": restored_mobility,
        "reverted_by": reverted_by,
        "reverted_at": now,
    }


@api_router.post("/trigger-alert")
async def trigger_alert(
    body: TriggerAlertBody,
    x_admin_token: Optional[str] = Header(default=None, alias="X-Admin-Token"),
):
    """Broadcast a Quake Angel alert to every registered device (except the
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
    apns_payload: Optional[dict] = None  # exact JSON POSTed to Apple

    # ---- iOS: direct APNs with true critical-alert payload ----
    try:
        apns_result = await send_critical_alerts(
            db=db,
            devices=ios_devices,
            title=title,
            body=message,
            action_url="/alert",
            idempotency_key=idem,
        )
        apns_events = apns_result.get("events", []) or []
        apns_payload = apns_result.get("payload")
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
            "chunks": events,               # legacy field (Android)
            "apns_events": apns_events,     # per-recipient iOS results
            "apns_payload": apns_payload,   # exact JSON body POSTed to Apple
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
        "apns_payload": apns_payload,
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
            # Render the exact JSON payload that was POSTed to Apple's APNs.
            # This lets us verify sound.name / interruption-level / critical
            # were actually set at wire time, not just intended in code.
            payload = ev.get("apns_payload")
            payload_html = ""
            if payload:
                payload_pretty = _html.escape(_json.dumps(payload, indent=2))
                aps = payload.get("aps") if isinstance(payload, dict) else None
                sound = aps.get("sound") if isinstance(aps, dict) else None
                is_critical_sound = (
                    isinstance(sound, dict) and sound.get("critical") == 1
                )
                interruption = aps.get("interruption-level") if isinstance(aps, dict) else None
                sound_name = sound.get("name") if isinstance(sound, dict) else None
                summary_color = (
                    "#1F8A3A"
                    if is_critical_sound
                    and interruption == "critical"
                    and sound_name
                    and sound_name != "default"
                    else "#C21818"
                )
                payload_html = f"""
<div style="margin-top:10px">
  <div style="font-size:12px;color:#666;font-weight:700;text-transform:uppercase;letter-spacing:.05em;margin-bottom:6px">APNs request payload</div>
  <div style="display:flex;gap:8px;flex-wrap:wrap;font-size:12px;margin-bottom:6px">
    <span style="background:{summary_color};color:#fff;padding:2px 8px;border-radius:999px;font-weight:700">sound.critical: {_html.escape(str(sound.get('critical') if isinstance(sound, dict) else '—'))}</span>
    <span style="background:{summary_color};color:#fff;padding:2px 8px;border-radius:999px;font-weight:700">sound.name: {_html.escape(str(sound_name or '—'))}</span>
    <span style="background:{summary_color};color:#fff;padding:2px 8px;border-radius:999px;font-weight:700">interruption-level: {_html.escape(str(interruption or '—'))}</span>
  </div>
  <pre style="background:#0e1116;color:#d5dae0;padding:10px;border-radius:6px;font-size:11px;overflow:auto;max-height:280px;white-space:pre-wrap;word-break:break-word">{payload_pretty}</pre>
</div>"""
            else:
                payload_html = '<div style="margin-top:8px;font-size:12px;color:#c21818">⚠️ apns_payload not recorded for this event (pre-payload-capture backend).</div>'

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
  {payload_html}
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
<title>Quake Angel — last push events</title>
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
<title>Quake Angel — registered devices</title>
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
<title>Quake Angel — last registrations</title>
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
                "title": "Quake Angel self-test",
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
<title>Quake Angel — self-test push</title>
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


# ---------- Routing pre-flight (which push path would this device take?) ----------
@api_router.get("/admin/route-check")
async def route_check(
    user_id: str = Query(..., description="user_id of the device to inspect"),
    token: str = Query(default=""),
    x_admin_token: Optional[str] = Header(default=None, alias="X-Admin-Token"),
):
    """Show which push delivery path a given device would take on the next
    /api/trigger-alert call. Does not send anything. Answers the question:
    'Was the last silent-no-CRITICAL-badge because we fell back to SuprSend?'
    """
    if not ADMIN_TRIGGER_PASSWORD:
        raise HTTPException(500, "ADMIN_TRIGGER_PASSWORD not configured")
    if (x_admin_token or token) != ADMIN_TRIGGER_PASSWORD:
        raise HTTPException(401, "Invalid admin token")

    target = user_id.strip()
    device = await db.push_devices.find_one({"user_id": target}, {"_id": 0})
    if not device:
        raise HTTPException(404, f"No device row for user_id={target}")

    platform = (device.get("platform") or "").lower()
    device_token = device.get("device_token") or ""
    token_len = len(device_token)

    apns_status = await apns_config_status(db)

    # Replicate the exact filter used in /api/trigger-alert.
    would_take_apns = platform == "ios" and bool(device_token)
    would_take_suprsend = platform != "ios"
    would_be_dropped = not would_take_apns and not would_take_suprsend

    if would_take_apns and not apns_status.get("configured"):
        expected_outcome = (
            "APNs config MISSING → send_critical_alerts returns a stub event "
            "with reason APNS_NOT_CONFIGURED; device gets NOTHING."
        )
    elif would_take_apns:
        expected_outcome = (
            "Direct APNs (Critical Alert payload). Screen wakes, CRITICAL "
            "badge shown, plays over silent — provided the device token is a "
            "real production APNs token."
        )
    elif would_take_suprsend:
        expected_outcome = (
            "SuprSend relay → regular push. No CRITICAL badge, no screen "
            "wake, respects silent/DND/Focus."
        )
    else:
        expected_outcome = (
            "DROPPED. Device is marked platform=ios but has no device_token "
            "in the DB — falls through both filters. Re-register from the "
            "Diagnostics screen in the app to fix."
        )

    return {
        "user_id": target,
        "platform_in_db": device.get("platform"),
        "device_token_length": token_len,
        "device_token_fingerprint": (
            f"{device_token[:8]}…{device_token[-8:]}" if token_len > 16 else device_token
        ),
        "routing": {
            "would_take_apns_critical": would_take_apns,
            "would_take_suprsend": would_take_suprsend,
            "would_be_dropped": would_be_dropped,
        },
        "apns_configured": apns_status.get("configured", False),
        "apns_metadata": {
            "key_id": apns_status.get("key_id"),
            "team_id": apns_status.get("team_id"),
            "bundle_id": apns_status.get("bundle_id"),
            "updated_at": apns_status.get("updated_at"),
        },
        "expected_outcome": expected_outcome,
    }


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


@api_router.get("/cors-debug")
async def cors_debug(request: Request):
    """Echo the deployed CORS allowlist and evaluate the caller's Origin
    against it. Use this to tell code-vs-deploy drift apart at a glance —
    if the dashboard is empty and this endpoint says allowed=false for the
    dashboard's origin, the fix is a redeploy, not a code change.

    Not admin-gated: reveals no secrets, just the CORS config that any
    browser can already probe with an OPTIONS preflight.

    Response fields:
      request_origin        The Origin header the caller sent (or null).
      allowed               true if request_origin would pass CORS.
      allow_reason          "exact_match" | "regex_match" | "no_origin_header"
                            | "not_allowlisted"
      allowed_origins       Exact origins currently whitelisted.
      allowed_origin_regex  Regex applied to any origin not in the exact list.
      deploy_fingerprint    SHA + mtime of the running server.py — lets you
                            check "is the running instance the file I just
                            edited?" without redeploying blind.
    """
    origin = request.headers.get("origin")

    allowed = False
    allow_reason = "no_origin_header"
    if origin:
        if origin in CORS_ALLOWED_ORIGINS:
            allowed = True
            allow_reason = "exact_match"
        elif _re.match(CORS_ALLOWED_ORIGIN_REGEX, origin):
            allowed = True
            allow_reason = "regex_match"
        else:
            allowed = False
            allow_reason = "not_allowlisted"

    return {
        "request_origin": origin,
        "allowed": allowed,
        "allow_reason": allow_reason,
        "allowed_origins": CORS_ALLOWED_ORIGINS,
        "allowed_origin_regex": CORS_ALLOWED_ORIGIN_REGEX,
        "deploy_fingerprint": {
            "server_py_sha256_prefix": _SERVER_PY_SHA256,
            "server_py_mtime_utc": _SERVER_PY_MTIME,
            "server_py_lines": _SERVER_PY_LINES,
            "process_started_at_utc": _PROCESS_STARTED_AT,
        },
    }




# ---------- Wire up ----------
app.include_router(api_router)

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=CORS_ALLOWED_ORIGINS,
    allow_origin_regex=CORS_ALLOWED_ORIGIN_REGEX,
    allow_methods=["GET", "POST", "OPTIONS"],
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
