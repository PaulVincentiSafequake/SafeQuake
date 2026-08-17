from fastapi import FastAPI, APIRouter, HTTPException, Header, Query, Body, Request
from fastapi.responses import HTMLResponse, Response
from dotenv import load_dotenv, dotenv_values
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
from datetime import datetime, timedelta, timezone

# .env must load BEFORE any module that reads env at import time.
# We used to have `from auth import ...` above load_dotenv, which meant
# the auth module's config helpers (if they'd been eager) saw an empty
# environment. Belt-and-suspenders: auth.py now reads env lazily on each
# call, AND we load .env early. Either alone would suffice; both together
# make the import order noise-free.
ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

from apns import (
    aclose as apns_aclose,
    apns_config_status,
    send_critical_alerts,
    send_preview_alerts,
    send_silent_cancel_reminders,
)

# EMSC/USGS shadow-mode monitoring (Phase 1, 2026-08).
# Poll loop runs in-process alongside the FastAPI app; started in the
# `startup` handler and cancelled cleanly in `shutdown`. Does not fire
# any user-facing pushes in Phase 1 — logs would_have_fired decisions
# to emsc_events for a 1-2 week soak.
from emsc.poller import EMSCPoller
from emsc.seed import seed_country_configs
from emsc.testimonies import TestimoniesSweeper
from notification_presets import VALID_PRESETS, DEFAULT_PRESET
from entitlements import (
    VALID_STATES as ENTITLEMENT_VALID_STATES,
    VALID_REASONS as ENTITLEMENT_VALID_REASONS,
    GRACE_PERIOD_DAYS,
    public_entitlement_view,
    upsert_entitlement,
    clear_test_override,
)

# Auth module — per-user Google sign-in (task #9, 2026-08-04).
# Handles: Google ID token verification, JWT issuance/decoding, request-time
# principal resolution (JWT-first with legacy X-Admin-Token fallback), role
# enforcement, and audit attribution. See /app/backend/auth.py for the full
# architecture write-up.
from auth import (
    AuthError,
    LEGACY_PRINCIPAL,
    VALID_ROLES,
    audit_attribution,
    decode_app_jwt,
    issue_app_jwt,
    legacy_token_enabled,
    require_role,
    resolve_principal,
    verify_google_id_token,
)


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
#
# .env-first read (targeted inversion of the default OS-wins priority). Why:
# the production deploy pipeline injects an OS-level ADMIN_TRIGGER_PASSWORD
# at container-provision time that survives redeploys and is not editable
# via any user-facing console. With the default load_dotenv() behavior
# (override=False), that stale OS value would silently win over whatever we
# ship in .env, so token rotation via .env would never take effect on prod.
#
# We invert the priority for this specific variable ONLY — all other env
# vars (MONGO_URL, DB_NAME, EMERGENT_PUSH_KEY) keep the default OS-wins
# priority so the prod-injected MongoDB URL etc. continue to work. Using
# dotenv_values() rather than override=True on load_dotenv() keeps the
# scope narrow to this one key.
#
# See security incident notes 2026-08-04 in memory/test_credentials.md.
_env_file_values = dotenv_values(ROOT_DIR / '.env')
ADMIN_TRIGGER_PASSWORD = (
    _env_file_values.get("ADMIN_TRIGGER_PASSWORD")
    or os.environ.get("ADMIN_TRIGGER_PASSWORD", "")
)

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

# EMSC/USGS poller — instantiated at import time, started in the
# `startup` handler. Held as a module global so admin endpoints
# further down can read `.started_at` etc.
# Preview APNs sender is injected here (rather than imported inside
# emsc/) so the poller subpackage stays transport-agnostic and testable
# in isolation.
emsc_poller = EMSCPoller(db, apns_send_preview=send_preview_alerts)

# EMSC testimonies sweeper — Part 1a validation channel. Every 15 min,
# fetches EMS-98 felt-report intensities for recent events and updates
# `intensity_estimates.from_emsc_testimonies` in place. Separate task
# from the poller because of the different cadence (15min vs 60sec)
# and the different failure semantics — testimonies data being late
# by hours is fine; missing a poll is not.
emsc_testimonies = TestimoniesSweeper(db)

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


# ---------- #146: telling test entries apart from real casualties -------
# Synthetic/test rows used to sit in the live trapped list looking exactly
# like real people. That's dangerous in both directions: an operator can
# waste attention on a ghost, or dismiss a real person as "probably another
# test". Detection is deliberately two-pronged.
TEST_DEVICE_MARKERS = (
    "test",       # qg-snippet-test-…, qg-rescue-test-…, TEST_…
    "e2e",        # qg-rescue-e2e-…
    "loadtest",   # B5 load-test seeder
    "diag",       # diagnostics screen
    "snippet",    # browser-automation harnesses
    "playwright",
    "demo",
    "-mob-",      # qg-mob-safe-…, qg-mob-mobile-…
)

# The mobile app's own ids look like `qg-<13-digit epoch>-<8 random chars>`.
# Anything matching this is treated as a REAL person no matter what its
# random suffix happens to spell, so a marker substring can never
# accidentally hide a genuine casualty. Only the explicit operator flag can
# hide one of these — a decision with a name attached to it in the audit log.
_REAL_DEVICE_ID_RE = __import__("re").compile(r"^qg-\d{10,14}-[a-z0-9]{6,12}$")


def _is_test_device(row: dict) -> bool:
    """True when a device_status row is a test/synthetic entry.

    The explicit flag comes first and is the important one: test check-ins
    made from a real phone (which is how ours are made) cannot be
    recognised from the id, so an operator tags them by hand.
    """
    if row.get("synthetic") is True:
        return True
    did = str(row.get("device_id") or "")
    if did == "dashboard":
        return True
    if _REAL_DEVICE_ID_RE.match(did):
        return False
    low = did.lower()
    return any(m in low for m in TEST_DEVICE_MARKERS)


@api_router.get("/devices")
async def get_devices(
    request: Request,
    since: Optional[str] = Query(
        default=None,
        description="ISO-8601 timestamp. Only return devices updated on/after this instant.",
    ),
    limit: int = Query(default=1000, ge=1, le=5000),
):
    """Return every known device's latest state for the rescuer dashboard.

    GATED to operator/admin as of 2026-08-13: signed-out visitors could
    previously see per-device triage detail, short codes, battery levels
    and precise coordinates — live GDPR exposure. Anonymous consumers get
    GET /api/public/summary (aggregate counts only) instead.

    CORS is limited to https://safequake.onrender.com,
    https://*.quakeangel.app (any subdomain), and http://localhost:*
    (see middleware config below). Response is snake_case, null-safe, and
    stable — field names will not change after this ship.
    """
    principal = await resolve_principal(
        request, request.headers.get("x-admin-token"), ADMIN_TRIGGER_PASSWORD, db
    )
    require_role(principal, "admin", "operator")

    query: dict = {}
    if since:
        query["updated_at"] = {"$gte": since}

    rows = await db.device_status.find(query, {"_id": 0}).sort("updated_at", -1).to_list(limit)

    # Collision-safe short codes across the ACTIVE device set (item 2) and
    # 'trapped since' timestamps for the current trapped spell (item 3).
    code_map = _short_codes_for([r.get("device_id") for r in rows])
    trapped_since = await _trapped_since_map(
        [r.get("device_id") for r in rows if r.get("status") == "trapped"]
    )

    def clean(r: dict) -> dict:
        return {
            "device_id": r.get("device_id"),
            # short_code is derived on read, not stored — that way any change
            # to the algorithm (e.g. hash-based instead of tail) applies to
            # existing rows without a migration.
            "short_code": code_map.get(r.get("device_id")) or _short_code(r.get("device_id")),
            "trapped_since": trapped_since.get(r.get("device_id")),
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
            # ── #146: is this row a test/synthetic entry? ────────────────
            # Old test check-ins were cluttering the live trapped list with
            # no way to tell them from real casualties. Two sources:
            #   * an explicit `synthetic: true` flag (set by the load-test
            #     seeder, or by an operator via POST
            #     /api/admin/devices/{id}/mark-test — needed because Paul's
            #     test check-ins come from his own REAL device, which no
            #     naming pattern can catch);
            #   * a recognised test device_id pattern.
            # Returned as a field rather than filtered server-side: the
            # dashboard hides them by default but can show them on demand,
            # and nothing is ever silently dropped from a legal record.
            "is_test": _is_test_device(r),
        }

    return {
        "count": len(rows),
        "test_count": sum(1 for r in rows if _is_test_device(r)),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "devices": [clean(r) for r in rows],
    }


# ---------- Per-person full history (B3, 2026-08-17) ----------
@api_router.get("/admin/device-history/{device_id}")
async def get_device_history(
    device_id: str,
    request: Request,
    limit: int = Query(default=500, ge=1, le=2000),
    since: Optional[str] = Query(default=None, description="ISO-8601 — only events on/after this instant."),
):
    """Complete per-person ledger for one device, oldest context first
    in `last_known`, events newest-first.

    Includes EVERY status event — reconfirmations of the same status are
    their own dated rows, never collapsed ("reconfirmed still trapped
    every hour for three days" must be distinguishable from "phone died
    three days ago"). Battery and location are per-event snapshots from
    the append-only `status_events` ledger, plus every alert broadcast
    (`push_events`) so the record shows when alerts went out to the
    device. Admin/operator gated — this is a legal-record surface."""
    principal = await resolve_principal(
        request, request.headers.get("x-admin-token"), ADMIN_TRIGGER_PASSWORD, db
    )
    require_role(principal, "admin", "operator")
    is_admin = principal.get("role") == "admin"

    sq: dict = {"device_id": device_id}
    tq: dict = {}
    if since:
        sq["recorded_at"] = {"$gte": since}
        tq["created_at"] = {"$gte": since}

    rows = await db.status_events.find(sq, {"_id": 0}).sort("recorded_at", 1).to_list(limit)
    alerts = await db.push_events.find(tq, {"_id": 0}).sort("created_at", 1).to_list(limit)
    latest = await db.device_status.find_one({"device_id": device_id}, {"_id": 0})

    events: List[dict] = []
    for a in alerts:
        events.append({
            "kind": "alert_sent",
            "at": a.get("created_at"),
            "triggered_by": a.get("triggered_by") or "dashboard",
            "magnitude": a.get("magnitude"),
            "recipients_total": a.get("recipients_total") or 0,
        })

    # Reconfirmation detection: an event whose (status, severity, mobility)
    # triple matches the CHRONOLOGICALLY PREVIOUS status event is a
    # reconfirmation — same situation, re-reported. It still gets its own
    # row; the flag only labels it for the reader.
    prev_triple = None
    for r in rows:
        triple = (r.get("status"), r.get("severity"), r.get("mobility"))
        is_plain_status = not r.get("rescue_reverted") and r.get("status") != "rescued"
        ev = {
            "kind": ("rescue_reverted" if r.get("rescue_reverted")
                     else "rescued" if r.get("status") == "rescued"
                     else "status"),
            "at": r.get("recorded_at") or r.get("updated_at"),
            "status": r.get("status"),
            "severity": r.get("severity"),
            "mobility": r.get("mobility"),
            "latitude": r.get("latitude"),
            "longitude": r.get("longitude"),
            "accuracy_m": r.get("accuracy_m"),
            "battery_pct": r.get("battery_pct"),
            "battery_state": r.get("battery_state"),
            "reconfirmation": is_plain_status and triple == prev_triple,
        }
        if ev["kind"] == "rescued":
            ev["rescued_by"] = r.get("rescued_by") or "dashboard"
            if is_admin and r.get("notes"):
                ev["notes"] = r.get("notes")
        prev_triple = triple
        events.append(ev)

    events.sort(key=lambda e: e.get("at") or "", reverse=True)

    now = datetime.now(timezone.utc)
    silent_seconds = None
    if latest and latest.get("updated_at"):
        try:
            last_dt = datetime.fromisoformat(str(latest["updated_at"]).replace("Z", "+00:00"))
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=timezone.utc)
            silent_seconds = max(0, int((now - last_dt).total_seconds()))
        except ValueError:
            pass

    return {
        "device_id": device_id,
        "short_code": _short_code(device_id),
        "display_name": (latest or {}).get("display_name"),
        "generated_at": now.isoformat(),
        "last_known": {
            "status": (latest or {}).get("status"),
            "severity": (latest or {}).get("severity"),
            "mobility": (latest or {}).get("mobility"),
            "latitude": (latest or {}).get("latitude"),
            "longitude": (latest or {}).get("longitude"),
            "battery_pct": (latest or {}).get("battery_pct"),
            "updated_at": (latest or {}).get("updated_at"),
            "silent_seconds": silent_seconds,
            # >30 min of silence = treat position/status as LAST KNOWN,
            # not current. The dashboard labels it accordingly.
            "is_stale": silent_seconds is not None and silent_seconds > 1800,
        },
        "count": len(events),
        "events": events,
    }


# ---------- Legacy status-check demo endpoint (unused; kept for compat) ----------
@api_router.get("/status", response_model=List[StatusCheck])
async def get_status_checks():
    status_checks = await db.status_checks.find({}, {"_id": 0}).to_list(1000)
    return [StatusCheck(**s) for s in status_checks]


# ---------- Public aggregate summary (signed-out dashboard view) ----------
@api_router.get("/public/summary")
async def public_summary():
    """Aggregate counts ONLY — the B2-style view for anonymous visitors.
    No device ids, no short codes, no coordinates, no operator identities.
    This is everything a signed-out dashboard is allowed to show."""
    rows = await db.device_status.find({}, {"_id": 0, "status": 1}).to_list(10000)
    counts = {"safe": 0, "trapped": 0, "rescued": 0, "not_responding": 0, "unknown": 0}
    for r in rows:
        st = r.get("status") or "unknown"
        counts[st if st in counts else "unknown"] += 1
    alert_dt = await _last_alert_start()
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total": len(rows),
        "counts": counts,
        # Timestamp of the most recent alert broadcast (non-personal). The
        # dashboard anchors its "Since the alert" window to this.
        "last_alert_at": alert_dt.isoformat() if alert_dt else None,
    }


# ---------- Audit log (unified trigger + status feed) ----------
@api_router.get("/audit")
async def get_audit_log(
    request: Request,
    limit: int = Query(default=100, ge=1, le=500),
    since: Optional[str] = Query(
        default=None,
        description="ISO-8601 timestamp — only include events on/after this instant.",
    ),
    kind: Optional[str] = Query(
        default=None,
        description="Filter to a single event kind: 'trigger', 'status', 'rescued', or 'rescue_reverted'.",
    ),
    x_admin_token: Optional[str] = Header(default=None, alias="X-Admin-Token"),
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

    Notes visibility:
      Free-form operator `notes` on rescued events are ONLY included when the
      caller provides a valid `X-Admin-Token` header. Unauthenticated callers
      (i.e. the public dashboard's Recent Activity panel) get `notes_present`
      as a boolean instead of the note text. This closes the "admin
      accidentally types a credential into notes → public dashboard leaks it"
      failure class demonstrated by incident 2026-08-04. Authenticated
      operators wanting to read notes should hit /api/admin/audit-log (HTML
      view, already admin-gated) or re-request /api/audit with the token.

    CORS is limited to https://safequake.onrender.com,
    https://*.quakeangel.app (any subdomain), and http://localhost:*
    (see middleware config). Field names are stable snake_case.
    """
    # GATED to operator/admin as of 2026-08-13 — the feed exposed device
    # short codes, triage severity, map links and operator EMAILS to anyone
    # with the URL. Notes remain admin-only on top of that.
    principal = await resolve_principal(request, x_admin_token, ADMIN_TRIGGER_PASSWORD, db)
    require_role(principal, "admin", "operator")
    is_admin = principal.get("role") == "admin"

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
                # Notes visibility contract: expose the free-form text only
                # to admin-authenticated callers. Everyone else gets a
                # boolean flag so the dashboard can still surface "📝 note
                # exists, admin-only" without leaking the content. See the
                # endpoint docstring for the incident that motivated this.
                raw_notes = r.get("notes")
                notes_present = bool(raw_notes)
                rescued_event = {
                    **base,
                    "kind": "rescued",
                    "rescued_by": r.get("rescued_by") or "dashboard",
                    "notes_present": notes_present,
                    "prior_status": r.get("prior_status"),
                    "prior_severity": r.get("prior_severity"),
                    "prior_mobility": r.get("prior_mobility"),
                }
                if is_admin:
                    rescued_event["notes"] = raw_notes
                events.append(rescued_event)
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
    # Pass the admin token through to /api/audit's internal machinery so
    # this already-admin-gated HTML view still shows the full `notes` field
    # on rescued events — otherwise the notes-behind-auth change on
    # /api/audit would blank them here even for legitimate operators.
    feed = await get_audit_log(
        limit=limit,
        since=None,
        kind=None,
        x_admin_token=ADMIN_TRIGGER_PASSWORD,
    )  # type: ignore[arg-type]

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


# ---------- Audit log exports (CSV + PDF) ---------------------------------
#
# Both endpoints are admin-gated (same auth as /api/admin/audit-log HTML
# view). They return the same data as GET /api/audit, but formatted for
# offline archival, incident reporting, and Malta Civil Protection
# handover packages.
#
# Design decisions:
#
#  1. **CSV includes ALL fields flat.** Every event kind gets its own
#     column footprint — trigger events populate magnitude/recipients,
#     status events populate lat/lon/battery, rescued events populate
#     rescued_by/notes/prior_status. Empty cells where a field doesn't
#     apply. This means the CSV is wide but any single row is
#     unambiguously self-describing — no cross-referencing needed when
#     the file lands on someone's desk two months later.
#
#  2. **PDF is landscape A4, table-first, no logo/branding chrome.**
#     Reports get printed and archived; a colourful header wastes ink and
#     confuses non-Quake-Angel readers ("what is this branding, am I
#     looking at a marketing document?"). We're producing evidence.
#
#  3. **Since/until filters can span arbitrary windows.** The CSV path
#     accepts up to 30 days at a time (capped server-side); PDF caps at
#     500 events per document (readability). Longer windows → paginate
#     the request client-side.
#
#  4. **Notes are included in BOTH exports** (admin-gated, so no
#     leak-to-public risk). Redaction happens at storage time via
#     /api/admin/redact-notes if operationally needed.
#
#  5. **UTC timestamps in ISO 8601, always.** Malta uses CET/CEST which
#     shifts twice a year — any local-time timestamp on a paper report
#     becomes ambiguous the moment DST changes. Reader can convert if
#     they need local.

import csv as _csv
import io as _io

MAX_EXPORT_WINDOW_DAYS = 30
MAX_EXPORT_ROWS = 500


# ─── Dashboard settings (org logo, next-of-kin authority name, etc.) ─────
#
# Single-document collection `dashboard_settings` with well-known _id="global".
# Motivation:
#   - Some things (org logo bytes, authority name for casualty reports) are
#     deployment-specific and must NOT be hard-coded. "Malta Civil Protection"
#     was hard-coded in the B2 report footer, which implied an operational
#     agreement that didn't exist and would have been misleading if the
#     PDF ever reached press. Also blocks selling the platform to other
#     civil-protection agencies without a source-code fork.
#   - This is not per-country — it's per-deployment. If Quake Angel later
#     multi-tenants, this becomes per-tenant. For now, single doc.
#
# Fields:
#   authority_name    : str  — what to render in the B2 report footer. When
#                              unset, the report uses the generic phrasing
#                              "the responsible authorities" (Paul, 2026-08-10).
#   logo_b64          : str  — base64-encoded PNG or SVG. Bounded at 200 KB
#                              PNG / 100 KB SVG at the API layer.
#   logo_mime         : "image/png" | "image/svg+xml"
#   logo_updated_at   : datetime
#   logo_updated_by   : str
DASHBOARD_SETTINGS_ID = "global"

async def _get_dashboard_settings() -> dict:
    """Fetch the single dashboard-settings doc. Never raises — returns {}
    if the collection is empty, so callers can `settings.get(...)` freely
    without null-guarding a missing doc."""
    doc = await db.dashboard_settings.find_one({"_id": DASHBOARD_SETTINGS_ID})
    return doc or {}


async def _get_authority_name() -> str:
    """Resolve the "responsible authorities" phrase used in casualty
    reports and the B1/B2 legal footers. Falls back to a generic wording
    when no specific authority has been configured — this is intentional:
    naming an authority we have no agreement with (e.g. "Malta Civil
    Protection") in a distributable PDF creates a legal + reputational
    exposure that a generic phrase does not."""
    settings = await _get_dashboard_settings()
    name = (settings.get("authority_name") or "").strip()
    return name or "the responsible authorities"


# ─── Confidentiality banner (PDF + CSV) ─────────────────────────────────
#
# Every export that contains personal data (CSV audit, PDF audit, B1
# operational report) must carry a visible confidentiality notice on
# EVERY page (PDF) or as the FIRST ROW (CSV). B2 Public is exempt — it
# contains only aggregate counts and is designed for press/family use.
#
# Wording locked in by Paul on 2026-08-10. Do not paraphrase or shorten
# the text below without checking with him — the current phrasing was
# specifically negotiated to (a) invoke GDPR by name, (b) tell the reader
# where to get a shareable version instead of just refusing, (c) not
# assume any specific enforcement regime beyond GDPR (which the EU-wide
# deployment target actually gives us jurisdiction over).
CONFIDENTIALITY_TEXT = (
    "CONFIDENTIAL — contains personal data including precise locations "
    "and device identifiers. Do not share outside authorised emergency "
    "personnel. Handling is subject to GDPR. For public, press or family "
    "communication use the public \u201Csafe to share\u201D report instead."
)


def _pdf_confidentiality_onpage(canvas_, doc_):
    """Reportlab onPage callback — draws a red confidentiality banner
    across the top of every page and a matching footer line at the
    bottom. Used by CSV/audit PDF and B1 operational PDF; explicitly
    NOT used by B2 Public.

    Colour choice: dark red (#B0141A) — WCAG-AA on white background for
    the 10pt banner text. On B&W printers this shows up as ~50% grey,
    still legible; combined with the ALL-CAPS "CONFIDENTIAL" prefix and
    bold face, the label carries even when colour is stripped.
    """
    from reportlab.lib import colors as _rl_colors
    canvas_.saveState()
    w, h = doc_.pagesize   # width, height in points

    # Margin-band watermark on EVERY page — rotated "CONFIDENTIAL"
    # running up BOTH side margins. Replaced the centred diagonal
    # watermark (issue #131): the diagonal crossed the Rescued table
    # row and the chart legend on some layouts. The 12 mm side margins
    # are guaranteed content-free on every confidential document, so
    # the mark can be bolder AND can never obscure report data.
    canvas_.setFont("Helvetica-Bold", 11)
    try:
        canvas_.setFillAlpha(0.30)
    except Exception:
        pass  # very old reportlab without alpha — banner still carries
    canvas_.setFillColor(_rl_colors.HexColor("#B0141A"))
    _wm = "CONFIDENTIAL"
    _wm_w = canvas_.stringWidth(_wm, "Helvetica-Bold", 11)
    _y = 60.0
    while _y + _wm_w < h - 80:   # stop short of the top banner + header logos
        for _x in (20.0, w - 11.0):   # left + right margin bands
            canvas_.saveState()
            canvas_.translate(_x, _y)
            canvas_.rotate(90)
            canvas_.drawString(0, 0, _wm)
            canvas_.restoreState()
        _y += _wm_w + 60
    try:
        canvas_.setFillAlpha(1)
    except Exception:
        pass

    # Top band: 30pt tall with a 13pt bold CONFIDENTIAL — the single most
    # important line on the document must be legible at a glance, even on
    # a mis-scaled print (bug 1b, 2026-08-13; was 8pt in a 22pt band).
    banner_h = 30
    canvas_.setFillColor(_rl_colors.HexColor("#B0141A"))
    canvas_.rect(0, h - banner_h, w, banner_h, fill=1, stroke=0)
    canvas_.setFillColor(_rl_colors.white)
    canvas_.setFont("Helvetica-Bold", 13)
    canvas_.drawString(12, h - 14, "CONFIDENTIAL — personal data")
    canvas_.setFont("Helvetica", 7.5)
    # Fits a single line on portrait A4 at 7.5pt (verified).
    canvas_.drawString(12, h - 25,
        "Do not share outside authorised emergency personnel. "
        "For public use the \u201Csafe to share\u201D public report."
    )
    # Footer band
    canvas_.setFillColor(_rl_colors.HexColor("#B0141A"))
    canvas_.setFont("Helvetica-Bold", 7)
    canvas_.drawString(12, 8, "CONFIDENTIAL · GDPR-protected · use the \u201Csafe to share\u201D public report for external distribution")
    # Page number on the right — trivial but reviewers ask for it
    canvas_.setFont("Helvetica", 7)
    canvas_.setFillColor(_rl_colors.HexColor("#666666"))
    canvas_.drawRightString(w - 12, 8, f"Page {doc_.page}")
    canvas_.restoreState()





def _round5(v):
    """GDPR data-minimisation: coordinates leave the system at 5 decimal
    places (~1 m). Device-reported accuracy is 5–19 m, so further digits
    carry no information — only privacy risk."""
    try:
        return round(float(v), 5)
    except (TypeError, ValueError):
        return v


async def _backfill_display_names(events: list[dict]) -> None:
    """Fill missing `display_name` on export rows from the device's
    CURRENT record. Historical status_events predate display_name being
    snapshotted onto every event, so exports showed a permanently blank
    column — which reads as data loss."""
    missing = {e["device_id"] for e in events
               if e.get("device_id") and not e.get("display_name")}
    if not missing:
        return
    rows = await db.device_status.find(
        {"device_id": {"$in": list(missing)}},
        {"_id": 0, "device_id": 1, "display_name": 1},
    ).to_list(len(missing))
    names = {r["device_id"]: r["display_name"] for r in rows if r.get("display_name")}
    for e in events:
        if not e.get("display_name") and e.get("device_id") in names:
            e["display_name"] = names[e["device_id"]]


# ─── Operator pseudonymisation on export ────────────────────────────────
# Optional (query param `pseudonymise=true`): operator emails in
# triggered_by / rescued_by / reverted_by become stable aliases like
# "operator-3". The real mapping lives server-side in
# `operator_pseudonyms` so accountability is preserved — an admin can
# always resolve who operator-3 is, but a leaked export can't.

async def _operator_alias(identity: str) -> str:
    row = await db.operator_pseudonyms.find_one({"identity": identity}, {"_id": 0, "alias": 1})
    if row:
        return row["alias"]
    n = await db.operator_pseudonyms.count_documents({})
    alias = f"operator-{n + 1}"
    try:
        await db.operator_pseudonyms.insert_one({
            "identity": identity,
            "alias": alias,
            "created_at": datetime.now(timezone.utc).isoformat(),
        })
    except Exception:
        row = await db.operator_pseudonyms.find_one({"identity": identity}, {"_id": 0, "alias": 1})
        if row:
            return row["alias"]
    return alias


async def _pseudonymise_events(events: list[dict]) -> None:
    """In-place: replace personal operator identities (anything with an
    '@') with stable server-side aliases. Non-personal attributions like
    'dashboard' pass through unchanged."""
    cache: dict = {}
    for e in events:
        for f in ("triggered_by", "rescued_by", "reverted_by"):
            v = e.get(f)
            if isinstance(v, str) and "@" in v:
                if v not in cache:
                    cache[v] = await _operator_alias(v)
                e[f] = cache[v]


# ─── Credential detection for free-text notes ───────────────────────────
# An admin password leaked into a rescue note previously, forcing an
# urgent rotation + audit redaction. Server-side gate: reject anything
# resembling a credential BEFORE it is stored. Deliberately errs on the
# side of caution — a false rejection costs a reworded note; a false
# accept costs another credential rotation.
_CREDENTIAL_KEYWORD_RE = _re.compile(
    r"(?i)\b(password|passwd|pwd|passphrase|api[_ -]?key|secret|token|bearer|credentials?)\b"
    r"\s*(?:is|was|[:=])?\s*(\S{6,})"
)
_CREDENTIAL_TOKEN_RES = [
    _re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}"),                 # JWT
    _re.compile(r"\bAKIA[0-9A-Z]{16}\b"),                                     # AWS access key
    _re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),                       # PEM key
    _re.compile(r"\b(?:sk|pk|rk|ghp|gho|xox[abps])[_-][A-Za-z0-9_-]{16,}\b"), # common API-key prefixes
]


def _looks_like_credential(text: str) -> Optional[str]:
    """Returns a human-readable reason if `text` appears to contain a
    credential, else None."""
    for rx in _CREDENTIAL_TOKEN_RES:
        if rx.search(text):
            return "contains what looks like an API key, token or private key"
    m = _CREDENTIAL_KEYWORD_RE.search(text)
    if m:
        tail = m.group(2)
        # Only fire when the token after the keyword looks secret-shaped
        # (digits/symbols or mixed case) — "asked for the password" must pass.
        if _re.search(r"[0-9!@#$%^&*_\-+=/\\]", tail) or (
            tail != tail.lower() and tail != tail.upper() and len(tail) >= 8
        ):
            return f"looks like a credential following the word '{m.group(1)}'"
    for tok in _re.findall(r"\S{24,}", text):
        if (_re.search(r"[a-z]", tok) and _re.search(r"[A-Z]", tok)
                and _re.search(r"[0-9]", tok)):
            return "contains a long random-looking string"
    return None


# ─── Org logo on PDF headers ─────────────────────────────────────────────

def _logo_is_brand_duplicate(logo_bytes: bytes) -> bool:
    """True when an uploaded org logo is visually the Quake Angel mark
    itself (issue A2): re-exports of our own artwork used to print the
    mark TWICE on report headers. Mirrors the dashboard's check
    (looksLikeBrandMark in index.html): composite both onto the dark
    header colour at 16×16, compare RGB, mean-abs-diff < 12
    (measured: same artwork ≈ 1.4, a real partner logo ≈ 166)."""
    try:
        import base64
        from PIL import Image

        def _sig(data: bytes) -> bytes:
            im = Image.open(_io.BytesIO(data)).convert("RGBA")
            bg = Image.new("RGBA", (16, 16), (15, 15, 15, 255))
            bg.alpha_composite(im.resize((16, 16)))
            return bg.convert("RGB").tobytes()

        a = _sig(logo_bytes)
        b = _sig(base64.b64decode(_QA_LOGO_B64))
        diff = sum(abs(x - y) for x, y in zip(a, b)) / len(a)
        return diff < 12
    except Exception:
        return False   # if in doubt keep the logo — never hide a real partner


async def _get_logo_image_reader():
    """PNG org logo as a reportlab ImageReader, or None. SVG cannot be
    rasterised by reportlab, so an SVG logo appears on the dashboard but
    not on PDFs (documented limitation). A logo that visually duplicates
    the Quake Angel mark is treated as absent (A2) — same rule the
    dashboard header applies."""
    s = await _get_dashboard_settings()
    b64, mime = s.get("logo_b64"), s.get("logo_mime")
    if not b64 or mime != "image/png":
        return None
    import base64
    from reportlab.lib.utils import ImageReader
    try:
        raw = base64.b64decode(b64)
        if _logo_is_brand_duplicate(raw):
            return None
        return ImageReader(_io.BytesIO(raw))
    except Exception:
        return None


# Quake Angel's own mark, embedded so every report carries the product
# branding permanently (issue #128): before this, an uploaded partner
# logo was the ONLY mark on B1/B2 headers, inverting the hierarchy.
_QA_LOGO_B64 = "iVBORw0KGgoAAAANSUhEUgAAAFAAAABQCAYAAACOEfKtAAALWElEQVR4nO2ce7BdVX3HP7+19z777PO4JwnhEQnmASGTgDwKCVokWCUKoxgGyuhQHekf1qlOW2hheEynY9vR6Qz0D5RHpCKDMsZWxYggQnEUMxqLApZCE0AEwiNCQsjj3vPYr59/rHNiQu4595yz981jcr4z586de89e+7e++7d+r/XbS2q1mjLC0DAHWoBDHSMCM2JEYEa4vf5pZH+JcXAj7eEluhKoCvVwOsQ59FDwuivTpASqgu/BornTKdahAQFe3gL11uQk7kOgEat5i+bCz77kYIDDNc5RBb8AK69KWfekUi1Bmu79nd42sP05bAlkai/bk0Dd43M4op+5j8KYjBgRmBEjAjNiRGBGjAjMiBGBGTEiMCNGBGbEiMCMGBGYEdNCoB6Eud90yZQ7gYKtYKgeHER2ZPAL0zN+bgQqVtgwhhc3C56rOObAkqgKrgNGlBc3C0nau7o8DHIj0AgEY8rWHcp5V5b417s8jKOYA0SiKjgOJKpcs7rAR64JqDdTSlVFctyqyEygKngubHodPvdvBb77iIPnCjesKXP9Vzxc17K3PzlUBRFQlCu/VOC275fwXOHrD7pccWOBLdutZubxYLMTiNU+Y5Tv/cznC9+o0ggNR9SU1feWuPUeh6Cs+1RypxOqUAyUG77pcvdDJY6aqby1y+Gf76xy//oCnqPkpYSZCRQsib/aYCgWoOwrSWInUQ2EL95d4sGfC+UyJPuBxCSFUln5r4cNN327RK1iH16aQrUExsCvNprcjFfmYVIF4yqPP+vyxnYHpz2iqtXKJHG46tYSz76kBP6+ewp5Ik2hVIQnNsL1t5dwHGMfsNqH7Bjl1a0uTz7v4Hiai0PJTKARiGM4Y3FMuZiS7CFUmkLRVza94XH9V4q0orZTyXrTSdBxGjsnlGtWB7y506Xg7k1SksJYKeXUE2LSWHJxJpkITFIISvCTxwz/cEsZz5V9DHOSQK2s/Pevfe56wKFYmh57mCr4ReW2tS7rn/YZK+mkJkNE+Jubyjy2wWprVlkyESgCSQTzjlGqgRInkz/VVKFYEG6+p8gLL9ugNs94LFUIivDUc/AfPyhSCSa3tyIQxcKMivKO2Uock1kLc/PC5WJ3m2L3V5VNr3vccb+H62m+61hBRFl9b4Et2108R7uGKKpQKSpIPiJkIjBNwQ2UB35Z4P9e8Cj53QVPUygHsObhIk//Fop+PlqYphAE8D9PCWvX+VS7LF2w5JWKyqMbC/z0iQJekN2RZF7CGguLjouZUUmJk+7fVQXPUbZsd1jzYw/jdid7cDmUux/y2Dnxxyhg0u8BcQKzaykL5yRoJJnjwewEJnBkLaUcKEna27Olag33d37i89yL2LAmA4kd7Xt8I9z3C59KMIVTEEgSoVZOmTWW2u8eSBuYpmB8Zf3TBV7d4uB7vbVKFQqusnmby43/WUDJlhEYgTBSbljjs2PCwXW0p13r2OLnX3N57BkPp4fJ6VuGTBcLpKFw5uKIY2cntKKpvVqSQrWkrF1X5JHHhaCLx5wKSQrFMvxwveHBR4tUgu62rwMRaEXCgjkxp5wQkbSyx4LZCDQQteC0RSlL5sW0wv4EsuGE4abv+NSb7bLXAPftlKl27FBuucdHRPpaiiLQDOG0E2KWzFPCMHsTaeYl7Pvw1O+EJ593KfqK9qFN1iMrj/yvz7ceHjy47gTNd9zn8uiGgg2h+rheFYIC/Hqjy3OvCMUc4tHMTiSKYe6RypwjUqK4P00AQMFzhNvv9Xlzmy2J9WOP0nbP3sub4c4HfAJf+iZBsAXfuUelHDNLiZLMPiQ7gWEExxwNH1we0hpgSaQKga9seKnA7fe6FIpT2zBoL19Pufkej02ve1M6rrfLG8Xw4feEzJhpfz/gmYgxEDbh2ZedgW1ZqlAuwm1rA9b/RqhU7aS6IYqhXIUH1wt3/Sjoy3G8HSLwzCaHNAf7B3lUxdSSWKv0DiEmvVTBcZRGy+GKmwOeeUGpjlkPu6dWqbbreTXlif9Xrr61RJqaoQgQYEZFkZy2GnIp6buucPn5IeViMnB1o1Pyev6VAh//fJmH1kO5/McNqY7HLQXK935s+It/qbD5TQ+/MFga1slCapWEy1ZGCAPY6x7Ipy6rdjlE8XASpSkEReWVLR6f+kKVq77sUW/Z2qHrwPZx5bP/XuAzN1Z4c6f19sOUoRSr3a5DbsWM7CV9A60QTj5e+dDyFs1QhlpaaQpFT3Ec4cvfLfPFb3gUXAVR/vGrBe64v4RfEFskHYI86/CEj7wn5IS5SvNgsYECRAlUx4RV743aW2LDjZW2L501BmvX+fx+G/zuVeGHv/SZXbPedti4zZb0Uy4+NyQoSW77M7ksYQFIlZ0TQhhnMy2qYFAmmsL2cWHrDmi2U66sRj+MhfF6ToXANnIh0BhoNYVL359w0TlN6q3hljFgKyYqVEtKrazMGrNZS5oOH7MZA42WcNl5DS7405Rmg55lr4HGzmMQwcZo1RqcsThhvG6FHnasOIZjZqXMHIOjZipH1lLiZHi9FqDeVM5amhCUIc5xTya31g7HQFgXVp0Ts3JZa2gtFGMT/uVLIgIfZlTg9BOjru+qTQUjVvsuOqfFymUJrQnBOZhaOzropHUL5sPZ74oYbwyuhUasvVu2JOTKj0WEkaAqXPuJkJPmR7SiwctPxsBEE849LeIdx+aTvu01fn5DtbVwXLh4RcyyxSGNAbVQBHZMwMUrWsyZYx9IM4SF823+un18MNtlBOpNYcWpLc4/K6E1LrnZvt33yHMwaW+yn7gQ3rUwtsuuzzt0anV/dnqLVe9NCet2so6BqCF87P0xZy21cWa/GmSMfU31zMUx8+dB3EfBd1Dk32ApEDaEv7s0Yun8iEYfVd9OkPvOo2PuvK7OcUeze89WBKIQFs+Dr13b4IixuOv+856wS1dYviTkrz4a0RqXoR1bz/vkPWCnZHTiQmXlmSGNEDynd2wo2BRrZkWplYVWuLemiEAY2gB7rGybl6aSwTHQipQPvzvkncfZPDhv7YNp6pF2DDTGhWs/GXHJigavbDF9RdfGsRHupBMVELUNQr2I6DQTvbpF+MsLGnz2z2Pqu/K3fR1MW5e+AuWi8E+Xh3xm1QS+1z3879iqU4+PKZXb2rLH/0Xs32bU4KQFCRPN7s5EgUqQcsWlE1xzWYjvTu/JGdNGoA1J4Lij4ObrWrzv9CZv7TIUvL3J6cRpJ80PufyCmKRH15Sq8OkLQxbMifZxJiL2nIdtOw0Xnt3khqtDZs+QXDaOes5z+oZu79vGEI0bPn1hxKK5IW+8ZdqJPbsr2NVSwu1X11m6EFpdAmYj0GzCspPhlivreG6y29Y5xlZzfr/NcMrxLT75wZhwp5k2u7eXXNM7fDsMieDdpyi3/n2dj55dp1RM2T4uTDSF17Yazj+rxZ8sgYkpUkBjoD4OK05Xzj0t5LWthomm8NYuYUYl4ZL31Vl9VYNTF9uQZbrs3p7oeWZCXrATF5YvhTVnNLn7/piv3Rdw5MyUY2fH/PVFEVHUZ9Ddbs+47hMhs2vKa1tdtu0y/O0ldVZ9ICGcMNQnhs/FB4W8/fQ2I9agn7wA1uV87Emn1bZY6NxLwYE4lIFSLFV7GI7jKZoAKig2EBfyW7adY0/OG/bYk7zRaSDoTFSxHa1GBpt0J+9O31b9PhBHVe1XAjvoTFR2/xgcIuRaVRkWo7c1M2JEYEaMCMyIEYEZMSIwI0YEZkTv09v0MD+9rY+37rsSKGKj8MP6/EAFp9A7yN8nlYP26wg+LJ0vu19nPSzRbt3b8JKyq0uhY1ICwZLYaE23hIcGggJdm0e7LmEj9gXlEUDT7quwpxPZn6/pH6oYhTEZMSIwI0YEZsQfAN1poZrz4VedAAAAAElFTkSuQmCC"
_QA_LOGO_READER = None


def _get_qa_logo_reader():
    global _QA_LOGO_READER
    if _QA_LOGO_READER is None:
        import base64
        from reportlab.lib.utils import ImageReader
        _QA_LOGO_READER = ImageReader(_io.BytesIO(base64.b64decode(_QA_LOGO_B64)))
    return _QA_LOGO_READER


def _draw_header_logos(canvas_, doc_, partner_logo, top_offset_pt: float):
    """Top-right header marks: the permanent Quake Angel logo rightmost,
    with the optional partner logo to its LEFT under an explicit
    'In partnership with' caption. Hierarchy (issue #128): Quake Angel
    is ALWAYS present; a partner mark is always labelled, never a
    replacement."""
    from reportlab.lib import colors as _rl_colors
    try:
        page_w, page_h = doc_.pagesize
        lh = 26.0
        x_right = page_w - 14
        qa = _get_qa_logo_reader()
        qw, qh = qa.getSize()
        qa_w = qw * lh / float(qh or 1)
        canvas_.drawImage(
            qa, x_right - qa_w, page_h - top_offset_pt - lh,
            width=qa_w, height=lh, mask="auto", preserveAspectRatio=True,
        )
        if partner_logo is not None:
            iw, ih = partner_logo.getSize()
            lw = iw * lh / float(ih or 1)
            px_right = x_right - qa_w - 10
            canvas_.saveState()
            canvas_.setFont("Helvetica-Oblique", 5.5)
            canvas_.setFillColor(_rl_colors.HexColor("#777777"))
            canvas_.drawRightString(px_right, page_h - top_offset_pt + 2, "In partnership with")
            canvas_.restoreState()
            canvas_.drawImage(
                partner_logo, px_right - lw, page_h - top_offset_pt - lh,
                width=lw, height=lh, mask="auto", preserveAspectRatio=True,
            )
    except Exception:
        pass  # a broken logo must never block an emergency report


def _make_confidential_onpage(logo=None):
    """Banner + margin watermark + footer on every page, plus the
    permanent Quake Angel mark (and the partner logo when configured)."""
    def _onpage(canvas_, doc_):
        _pdf_confidentiality_onpage(canvas_, doc_)
        _draw_header_logos(canvas_, doc_, logo, top_offset_pt=42)
    return _onpage


def _make_public_onpage(logo=None):
    """B2 Public: Quake Angel mark + optional labelled partner logo
    top-right, no confidentiality chrome."""
    def _onpage(canvas_, doc_):
        _draw_header_logos(canvas_, doc_, logo, top_offset_pt=16)
    return _onpage


# ─── Response-over-time chart (B1 + B2) ─────────────────────────────────

def _bucket_timeline(raw_rows: list[dict], since_dt: datetime, until_dt: datetime):
    """Bucket status_events into hourly (window ≤ 48h) or daily buckets.
    Counts per bucket: trapped reports, safe check-ins, rescues."""
    hourly = (until_dt - since_dt).total_seconds() <= 48 * 3600
    step = timedelta(hours=1) if hourly else timedelta(days=1)
    start = since_dt.replace(minute=0, second=0, microsecond=0)
    if not hourly:
        start = start.replace(hour=0)
    buckets: list[dict] = []
    index: dict = {}
    t = start
    while t <= until_dt:
        label = t.strftime("%d %b %H:%M") if hourly else t.strftime("%d %b")
        index[t] = len(buckets)
        buckets.append({"t": t, "label": label, "trapped": 0, "safe": 0, "rescued": 0})
        t += step
    for row in raw_rows:
        ra = row.get("recorded_at")
        try:
            dt = datetime.fromisoformat(str(ra).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        key = dt.replace(minute=0, second=0, microsecond=0)
        if not hourly:
            key = key.replace(hour=0)
        i = index.get(key)
        if i is None:
            continue
        st = row.get("status")
        if st == "trapped":
            buckets[i]["trapped"] += 1
        elif st == "safe":
            buckets[i]["safe"] += 1
        elif st == "rescued" and not row.get("rescue_reverted"):
            buckets[i]["rescued"] += 1
    return buckets, hourly


def _timeline_chart(buckets: list[dict], width_pt: float, height_pt: float):
    """Grouped bar chart Drawing: trapped / rescued / safe per bucket,
    with a manual legend (plain words, no jargon)."""
    from reportlab.graphics.shapes import Drawing, Rect, String
    from reportlab.graphics.charts.barcharts import VerticalBarChart
    from reportlab.lib import colors as C

    d = Drawing(width_pt, height_pt)
    # Opaque white base so the page watermark never bleeds through the
    # plot area or legend (Paul, 2026-08-13 polish item).
    d.add(Rect(0, 0, width_pt, height_pt, fillColor=C.white, strokeColor=None))
    chart = VerticalBarChart()
    chart.x, chart.y = 34, 30
    chart.width, chart.height = width_pt - 44, height_pt - 52
    s_trap = [b["trapped"] for b in buckets]
    s_resc = [b["rescued"] for b in buckets]
    s_safe = [b["safe"] for b in buckets]
    chart.data = [s_trap, s_resc, s_safe]
    chart.bars[0].fillColor = C.HexColor("#C21818")
    chart.bars[1].fillColor = C.HexColor("#1F8A3A")
    chart.bars[2].fillColor = C.HexColor("#7A8CA0")
    chart.bars.strokeColor = None
    chart.groupSpacing = 5
    chart.barSpacing = 1
    max_v = max([1] + s_trap + s_resc + s_safe)
    chart.valueAxis.valueMin = 0
    chart.valueAxis.valueMax = max_v + 1
    chart.valueAxis.valueStep = max(1, (max_v + 1) // 5)
    chart.valueAxis.labels.fontSize = 6
    labels = [b["label"] for b in buckets]
    if len(labels) > 16:
        keep = max(1, len(labels) // 12)
        labels = [l if i % keep == 0 else "" for i, l in enumerate(labels)]
    chart.categoryAxis.categoryNames = labels
    chart.categoryAxis.labels.fontSize = 6
    chart.categoryAxis.labels.angle = 45
    chart.categoryAxis.labels.boxAnchor = "ne"
    d.add(chart)
    # Legend with an opaque white backing strip — the diagonal watermark
    # crosses the top of the drawing and was obscuring "Checked in safe"
    # on B1 (2026-08-13). The strip keeps legend text legible in colour
    # AND in black-and-white print.
    legend_items = (
        ("Told us they are trapped", "#C21818"),
        ("Marked found / rescued", "#1F8A3A"),
        ("Checked in safe", "#7A8CA0"),
    )
    legend_w = sum(10 + len(label) * 3.6 + 18 for label, _ in legend_items)
    d.add(Rect(30, height_pt - 14, legend_w, 13, fillColor=C.white, strokeColor=None))
    lx = 34.0
    for label, color in legend_items:
        d.add(Rect(lx, height_pt - 11, 7, 7, fillColor=C.HexColor(color), strokeColor=None))
        s = String(lx + 10, height_pt - 10, label)
        s.fontSize = 7
        d.add(s)
        lx += 10 + len(label) * 3.6 + 18
    return d


def _plural(n: int, singular: str, plural: str) -> str:
    return singular if n == 1 else plural


def _progress_figures(raw_rows: list[dict], latest_events: list[dict], counts: dict) -> dict:
    """All figures for the plain-language narrative.

    CONSISTENCY RULE (bug 2026-08-13): any narrative figure that shares a
    concept with the aggregate table MUST be computed from the SAME source
    as the table. The table once said "People rescued: 0" while the
    narrative said "1 of 1 found" — because a self-reported safe check-in
    was silently merged into "found". Two definitions of one word, a
    paragraph apart, on the report families read.

    - `rescued`       == the table's "People rescued" (operator-confirmed).
    - `still_trapped` == the table's trapped count.
    - `self_safe`     is a SEPARATE figure (self-reported, never merged).
    """
    trapped_ids = {r["device_id"] for r in raw_rows
                   if r.get("status") == "trapped" and r.get("device_id")}
    self_safe = sum(1 for e in latest_events
                    if e.get("device_id") in trapped_ids and e.get("status") == "safe")
    resolved = sum(1 for e in latest_events
                   if e.get("device_id") in trapped_ids and e.get("status") in ("rescued", "safe"))
    return {
        "total_trapped_reports": len(trapped_ids),
        "rescued": int(counts.get("rescued") or 0),
        "still_trapped": int(counts.get("trapped") or 0),
        "self_safe": self_safe,
        "resolved": resolved,
    }


def _plain_language_progress(raw_rows: list[dict], latest_events: list[dict],
                             counts: dict) -> list[str]:
    """Short lines, one idea per line, no percentages, no jargon.

    HARD LOCKS (Paul, 2026-08-12/13):
    - "found by a rescue team" and "told us themselves they are safe" are
      two separate, separately-worded figures. Never merged into a single
      "found" number.
    - No percentages anywhere: the former B1 "Overall … (N%)" line merely
      restated the split lines above it and was dropped 2026-08-13 at
      Paul's request.
    - Singular/plural handled ("1 person … has", never "1 of the 1 … have").
    """
    f = _progress_figures(raw_rows, latest_events, counts)
    total = f["total_trapped_reports"]
    if total == 0:
        return [
            "No one has told us they are trapped in this time window.",
            "This only counts people using the app.",
            "Others may be affected who we cannot see.",
        ]
    lines = [f"{total} {_plural(total, 'person', 'people')} told us they were trapped during this window."]

    resc = f["rescued"]
    if resc == 0:
        lines.append("No one has been confirmed found by a rescue team yet.")
    else:
        lines.append(f"{resc} {_plural(resc, 'person has', 'people have')} been confirmed found by a rescue team.")

    ss = f["self_safe"]
    if ss:
        lines.append(
            f"{ss} {_plural(ss, 'person who had been trapped told', 'people who had been trapped told')} "
            "us themselves that they are now safe."
        )

    still = f["still_trapped"]
    if still == 0:
        lines.append("No one is still recorded as trapped.")
    else:
        lines.append(f"{still} {_plural(still, 'person is', 'people are')} still recorded as trapped.")

    lines.append("This only counts people using the app.")
    lines.append("Others may be affected who we cannot see.")
    return lines


def _fmt_dt_plain(dt: datetime) -> str:
    """'13 August 2026, 15:02' — unambiguous between British and American
    date conventions, 24-hour clock. Timezone is stated once by the caller."""
    return dt.strftime("%d %B %Y, %H:%M").lstrip("0")


def _duration_words(td: timedelta) -> str:
    """'3 hours and 12 minutes' / '1 day and 4 hours' — whole words, never
    '3h 12m', never '0 hours'."""
    total_min = int(td.total_seconds() // 60)
    if total_min < 1:
        return "less than a minute"
    days, rem = divmod(total_min, 1440)
    hours, minutes = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days} {_plural(days, 'day', 'days')}")
    if hours:
        parts.append(f"{hours} {_plural(hours, 'hour', 'hours')}")
    if minutes and not days:   # over a day reads "1 day and 4 hours"
        parts.append(f"{minutes} {_plural(minutes, 'minute', 'minutes')}")
    if not parts:
        parts.append(f"{minutes} {_plural(minutes, 'minute', 'minutes')}")
    return parts[0] if len(parts) == 1 else " and ".join(parts)


def _covers_line(since_dt: datetime, until_dt: datetime) -> str:
    """Every export states the period it covers in absolute terms — 'Last
    7 days' is meaningless when the document is read next month or in an
    inquiry next year (1c, 2026-08-13)."""
    return (f"Covers {_fmt_dt_plain(since_dt)} to {_fmt_dt_plain(until_dt)} (UTC) — "
            f"{_duration_words(until_dt - since_dt)}.")


async def _last_alert_start() -> Optional[datetime]:
    """Start of the most recent alert = latest push_events row. There is
    no explicit end-of-incident marker in the data model, so 'active
    alert' is defined by the CALLER (dashboard uses: within 72h)."""
    rows = await db.push_events.find({}, {"_id": 0, "created_at": 1}).sort("created_at", -1).to_list(1)
    if not rows:
        return None
    try:
        dt = datetime.fromisoformat(str(rows[0].get("created_at")).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


async def _window_gap_warning(since_dt: datetime) -> Optional[str]:
    """1d (2026-08-13): if the export window starts AFTER the alert began,
    the document silently misses the first — probably worst — hours of the
    incident. Say so on the document itself, in plain words."""
    alert = await _last_alert_start()
    if alert is not None and since_dt > alert:
        return ("Warning: this window starts after the alert. It leaves out the first "
                f"{_duration_words(since_dt - alert)} of the incident.")
    return None


def _short_codes_for(device_ids) -> dict:
    """Collision-safe short codes (item 2, 2026-08-13). Rule: last 5
    alphanumeric characters of the device id, uppercased; if two ACTIVE
    devices collide, every collider is extended leftward 2 characters at
    a time until unique. Same map must be used everywhere a code is shown
    so the string matches across card / pin / report / CSV."""
    from collections import Counter
    alnum = {d: _re.sub(r"[^A-Za-z0-9]", "", str(d)) for d in device_ids if d}
    # Contract (iteration 25): ids too short to be meaningful get NO code.
    alnum = {d: a for d, a in alnum.items() if len(a) >= 3}
    codes: dict = {}
    remaining = list(alnum)
    length = 5
    while remaining:
        trial = {d: alnum[d][-min(length, len(alnum[d])):].upper() for d in remaining}
        counts = Counter(trial.values())
        nxt = []
        for d, code in trial.items():
            if counts[code] == 1 or len(code) >= len(alnum[d]):
                codes[d] = code
            else:
                nxt.append(d)
        remaining = nxt
        length += 2
    return codes


async def _trapped_since_map(device_ids) -> dict:
    """device_id -> ISO timestamp of the FIRST 'trapped' report of the
    current trapped spell (walks back until a non-trapped event breaks
    the run). Drives 'Trapped for …' on cards and the team report."""
    ids = [d for d in device_ids if d]
    if not ids:
        return {}
    rows = await db.status_events.find(
        {"device_id": {"$in": ids}},
        {"_id": 0, "device_id": 1, "status": 1, "recorded_at": 1},
    ).sort("recorded_at", -1).to_list(20000)
    out: dict = {}
    closed: set = set()
    for r in rows:   # newest → oldest
        d = r.get("device_id")
        if d in closed:
            continue
        if r.get("status") == "trapped":
            out[d] = r.get("recorded_at")
        else:
            closed.add(d)
    return out


def _low_battery_lines(latest_events: list[dict]) -> list[str]:
    """Item 4 (2026-08-13): low battery is a countdown to losing contact.
    Plain words, states the total it is out of, never a bare percentage."""
    still = [e for e in latest_events if e.get("status") == "trapped"]
    low = [e for e in still
           if isinstance(e.get("battery_pct"), (int, float)) and e["battery_pct"] < 20]
    if not low:
        return []
    n, t = len(low), len(still)
    if t == 1:
        first = "The 1 person still trapped has a phone battery below 20%."
    else:
        first = f"{n} of the {t} people still trapped {_plural(n, 'has', 'have')} a phone battery below 20%."
    return [first, "We may stop receiving updates from them."]


def _parse_iso_or_none(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        raise HTTPException(400, f"Invalid ISO 8601 datetime: {s!r}")


async def _fetch_audit_for_export(
    since_iso: Optional[str],
    until_iso: Optional[str],
    kind: Optional[str],
    limit: int,
) -> list[dict]:
    """Shared query used by both CSV and PDF exports.

    Same shape as get_audit_log() but with the notes-visibility switch
    fixed to `is_admin=True` (both callers are already admin-gated) and
    with an inclusive `until` filter.
    """
    since_dt = _parse_iso_or_none(since_iso)
    until_dt = _parse_iso_or_none(until_iso)

    # Sanity-clamp the window. Default: last 7 days.
    if since_dt is None:
        since_dt = datetime.now(timezone.utc) - timedelta(days=7)
    if until_dt is None:
        until_dt = datetime.now(timezone.utc)
    if until_dt < since_dt:
        raise HTTPException(400, "until must be >= since")
    if (until_dt - since_dt).days > MAX_EXPORT_WINDOW_DAYS:
        raise HTTPException(
            400,
            f"Window too wide (max {MAX_EXPORT_WINDOW_DAYS} days). Paginate the request.",
        )

    since_iso_norm = since_dt.isoformat()
    until_iso_norm = until_dt.isoformat()

    events: list[dict] = []

    # Trigger events
    if kind in (None, "trigger"):
        tq: dict = {"created_at": {"$gte": since_iso_norm, "$lte": until_iso_norm}}
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

    # Status / rescued / reverted events
    want_status   = kind in (None, "status")
    want_rescued  = kind in (None, "rescued")
    want_reverted = kind in (None, "rescue_reverted")
    if want_status or want_rescued or want_reverted:
        sq: dict = {"recorded_at": {"$gte": since_iso_norm, "$lte": until_iso_norm}}
        rows = await db.status_events.find(sq, {"_id": 0}).sort("recorded_at", -1).to_list(limit)
        for r in rows:
            base = {
                "at": r.get("recorded_at") or r.get("updated_at"),
                "device_id": r.get("device_id"),
                "short_code": _short_code(r.get("device_id")),
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
                if want_reverted:
                    events.append({**base, "kind": "rescue_reverted",
                                   "reverted_by": r.get("reverted_by") or "dashboard"})
            elif r.get("status") == "rescued":
                if want_rescued:
                    events.append({**base, "kind": "rescued",
                                   "rescued_by": r.get("rescued_by") or "dashboard",
                                   "notes": r.get("notes"),
                                   "prior_status": r.get("prior_status"),
                                   "prior_severity": r.get("prior_severity"),
                                   "prior_mobility": r.get("prior_mobility")})
            else:
                if want_status:
                    events.append({**base, "kind": "status"})

    events.sort(key=lambda e: e.get("at") or "", reverse=True)
    events = events[:limit]
    await _backfill_display_names(events)
    # Collision-safe short codes, consistent with /api/devices (item 2).
    _codes = _short_codes_for({e.get("device_id") for e in events if e.get("device_id")})
    for e in events:
        if e.get("device_id"):
            e["short_code"] = _codes.get(e["device_id"]) or e.get("short_code")
    # GDPR data-minimisation: 5 dp (~1 m) is already beyond device accuracy.
    for e in events:
        for k in ("latitude", "longitude"):
            if e.get(k) is not None:
                e[k] = _round5(e[k])
        # A metre reading needs one decimal, not fifteen.
        if e.get("accuracy_m") is not None:
            try:
                e["accuracy_m"] = round(float(e["accuracy_m"]), 1)
            except (TypeError, ValueError):
                pass
    return events


# Column order for CSV. Locked here so the header is stable across
# releases — analytics scripts and archival tooling can rely on it.
_CSV_COLUMNS = [
    "at", "at_simple", "kind",
    # Trigger fields
    "idempotency_key", "triggered_by", "magnitude",
    "recipients_total", "ios_count", "android_count",
    "delivered", "error",
    # Device / status fields
    "device_id", "short_code", "display_name",
    "status", "severity", "mobility",
    "latitude", "longitude", "accuracy_m",
    "battery_pct", "battery_state", "platform",
    # Rescue / revert fields
    "rescued_by", "prior_status", "prior_severity", "prior_mobility", "notes",
    "reverted_by",
]


@api_router.get("/admin/audit-log/export.csv")
async def export_audit_log_csv(
    request: Request,
    since: Optional[str] = Query(default=None, description="ISO 8601 start (inclusive). Default: 7 days ago."),
    until: Optional[str] = Query(default=None, description="ISO 8601 end (inclusive). Default: now."),
    kind: Optional[str] = Query(default=None, description="Optional filter: trigger|status|rescued|rescue_reverted"),
    pseudonymise: bool = Query(default=False, description="Replace operator emails with stable pseudonyms (operator-N)."),
    limit: int = Query(default=MAX_EXPORT_ROWS, ge=1),
):
    """Downloadable CSV of audit events in the given window. Admin-only."""
    # Silent-clamp limit — this is an operator export action, not a query
    # validation surface; getting 500 rows when you asked for 99999 is
    # the correct interpretation of "give me a lot", not a 422 error page.
    limit = min(limit, MAX_EXPORT_ROWS)

    principal = await resolve_principal(
        request,
        request.headers.get("x-admin-token"),
        ADMIN_TRIGGER_PASSWORD,
        db,
    )
    require_role(principal, "admin", "operator")

    events = await _fetch_audit_for_export(since, until, kind, limit)
    if pseudonymise:
        await _pseudonymise_events(events)

    since_dt = _parse_iso_or_none(since) or datetime.now(timezone.utc) - timedelta(days=7)
    until_dt = _parse_iso_or_none(until) or datetime.now(timezone.utc)
    generated_by = principal.get("email", "?")
    if pseudonymise and "@" in generated_by:
        generated_by = await _operator_alias(generated_by)

    # Every row — including the warning and metadata rows — is padded to
    # the full column count so strict parsers (pandas, R) don't reject
    # the file as ragged/malformed. Line endings are CRLF throughout
    # (csv module default is CRLF; the old hand-written warning row used
    # bare LF, producing a mixed-endings file).
    ncols = len(_CSV_COLUMNS)

    def _pad(row: list) -> list:
        return (row + [""] * ncols)[:ncols]

    buf = _io.StringIO()
    writer = _csv.writer(buf, lineterminator="\r\n")
    writer.writerow(_pad([CONFIDENTIALITY_TEXT]))
    # Export metadata — a CSV on its own must be identifiable (the PDF
    # states its window + generator; the CSV now does too).
    writer.writerow(_pad(["export_window_start_utc", since_dt.isoformat()]))
    writer.writerow(_pad(["export_window_end_utc", until_dt.isoformat()]))
    writer.writerow(_pad(["generated_at_utc", datetime.now(timezone.utc).isoformat()]))
    writer.writerow(_pad(["generated_by", generated_by]))
    writer.writerow(_pad(["row_count", str(len(events))]))
    # Plain-words coverage line + optional missing-start warning (1c/1d) —
    # someone opening this file next month never saw the screen.
    writer.writerow(_pad(["covers", _covers_line(since_dt, until_dt)]))
    _gap = await _window_gap_warning(since_dt)
    if _gap:
        writer.writerow(_pad(["warning", _gap]))
    writer.writerow(_CSV_COLUMNS)

    for ev in events:
        row = []
        for col in _CSV_COLUMNS:
            if col == "at_simple":
                # Plain second timestamp Excel can sort ("2026-08-06 13:51")
                # alongside the precise ISO column.
                v = ev.get("at")
                if isinstance(v, datetime):
                    v = v.isoformat()
                try:
                    dtv = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
                    row.append(dtv.strftime("%Y-%m-%d %H:%M"))
                except (ValueError, TypeError):
                    row.append("")
                continue
            v = ev.get(col)
            if isinstance(v, datetime):
                row.append(v.isoformat())
            elif isinstance(v, bool):
                # Excel understands TRUE/FALSE; Python's True/False reads as text.
                row.append("TRUE" if v else "FALSE")
            elif v is None:
                row.append("")
            else:
                row.append(v)
        writer.writerow(row)

    # UTF-8 BOM so Excel decodes the em-dash in the warning row correctly —
    # without it Excel falls back to a legacy codepage and shows mojibake.
    csv_text = "\ufeff" + buf.getvalue()
    filename = f"CONFIDENTIAL-quakeangel-audit-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.csv"
    return Response(
        content=csv_text,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Row-Count": str(len(events)),
        },
    )


@api_router.get("/admin/audit-log/export.pdf")
async def export_audit_log_pdf(
    request: Request,
    since: Optional[str] = Query(default=None),
    until: Optional[str] = Query(default=None),
    kind: Optional[str] = Query(default=None),
    pseudonymise: bool = Query(default=False, description="Replace operator emails with stable pseudonyms (operator-N)."),
    limit: int = Query(default=MAX_EXPORT_ROWS, ge=1),
):
    """Downloadable PDF of audit events. Admin-only. Uses ReportLab.

    Landscape A4, monospaced tables, no branding chrome (see design
    note #2 in the section header). Suitable for print + archival.
    """
    # Silent-clamp limit — same rationale as the CSV export.
    limit = min(limit, MAX_EXPORT_ROWS)

    principal = await resolve_principal(
        request,
        request.headers.get("x-admin-token"),
        ADMIN_TRIGGER_PASSWORD,
        db,
    )
    require_role(principal, "admin", "operator")

    events = await _fetch_audit_for_export(since, until, kind, limit)
    if pseudonymise:
        await _pseudonymise_events(events)
    generated_by = principal.get("email", "?")
    if pseudonymise and "@" in generated_by:
        generated_by = await _operator_alias(generated_by)

    # Lazy-import ReportLab so we never pay the ~200ms cold-start cost
    # on non-PDF requests.
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import (
        SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak,
    )

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "AuditTitle", parent=styles["Heading1"],
        fontSize=14, spaceAfter=6, textColor=colors.HexColor("#111111"),
    )
    meta_style = ParagraphStyle(
        "AuditMeta", parent=styles["Normal"],
        fontSize=8, textColor=colors.HexColor("#555555"), spaceAfter=10,
    )
    cell_style = ParagraphStyle(
        "AuditCell", parent=styles["Normal"],
        fontSize=7, leading=9, wordWrap="CJK",
    )

    generated_at = datetime.now(timezone.utc).isoformat()
    since_dt = _parse_iso_or_none(since) or datetime.now(timezone.utc) - timedelta(days=7)
    until_dt = _parse_iso_or_none(until) or datetime.now(timezone.utc)

    story: list = [
        Paragraph("Quake Angel — audit log export", title_style),
        # Plain-words absolute coverage (1c) — the document may be read in
        # an inquiry long after "Last 7 days" has lost all meaning.
        Paragraph(_covers_line(since_dt, until_dt), ParagraphStyle(
            "AuditCovers", parent=styles["Normal"], fontSize=9.5,
            textColor=colors.HexColor("#222222"), spaceAfter=4,
        )),
        Paragraph(
            f"Events: {len(events)} &nbsp;·&nbsp; "
            f"Generated: {generated_at} &nbsp;·&nbsp; "
            f"By: {generated_by}",
            meta_style,
        ),
    ]
    _gap = await _window_gap_warning(since_dt)
    if _gap:
        story.insert(2, Paragraph(_html.escape(_gap), ParagraphStyle(
            "AuditWarn", parent=styles["Normal"], fontSize=9,
            textColor=colors.HexColor("#B0141A"), spaceAfter=8,
            fontName="Helvetica-Bold",
        )))

    def _cell(s) -> "Paragraph":
        # Wrap every cell in a Paragraph so long strings wrap instead
        # of overflowing off the page.
        if s is None or s == "":
            return Paragraph("&nbsp;", cell_style)
        return Paragraph(_html.escape(str(s)), cell_style)

    header = ["Time (UTC)", "Kind", "Actor / Device", "Details", "Location / Meta"]

    data: list = [header]
    for e in events:
        at = e.get("at") or ""
        if isinstance(at, datetime):
            at = at.isoformat()
        kind_str = e.get("kind", "?")
        if kind_str == "trigger":
            actor = e.get("triggered_by") or "?"
            details = (
                f"M={e.get('magnitude') or '?'} · "
                f"{e.get('recipients_total') or 0} devices "
                f"(iOS {e.get('ios_count') or 0}, Android {e.get('android_count') or 0}) · "
                f"{'delivered' if e.get('delivered') else 'FAILED'}"
            )
            if e.get("error"):
                details += f"\nError: {e.get('error')}"
            meta = e.get("idempotency_key") or ""
        elif kind_str == "rescued":
            actor = f"rescued by {e.get('rescued_by') or '?'}"
            details = (
                f"Device {e.get('short_code') or '?'} "
                f"(was {e.get('prior_status') or '?'}/{e.get('prior_severity') or '?'}). "
                f"Notes: {e.get('notes') or '—'}"
            )
            meta = e.get("device_id") or ""
        elif kind_str == "rescue_reverted":
            actor = f"reverted by {e.get('reverted_by') or '?'}"
            details = f"Device {e.get('short_code') or '?'} restored to {e.get('status') or '?'}"
            meta = e.get("device_id") or ""
        else:  # status
            actor = f"{e.get('display_name') or e.get('short_code') or '?'}"
            details = (
                f"{e.get('status') or '?'} / {e.get('severity') or '?'}"
                + (f" · battery {e.get('battery_pct')}%" if e.get("battery_pct") is not None else "")
            )
            lat = e.get("latitude"); lon = e.get("longitude")
            if lat is not None and lon is not None:
                meta = f"{lat:.4f}, {lon:.4f}" + (f" ±{e.get('accuracy_m'):.0f}m" if e.get("accuracy_m") else "")
            else:
                meta = e.get("platform") or ""

        data.append([_cell(at), _cell(kind_str), _cell(actor), _cell(details), _cell(meta)])

    # Column widths: at, kind, actor, details, meta.
    # Sum = 186mm which fits PORTRAIT A4 (210mm − 24mm margins). Portrait
    # because these get printed on default printer settings — landscape
    # PDFs were scaled down to near-illegible on portrait paper (1a).
    col_widths = [28*mm, 18*mm, 34*mm, 76*mm, 30*mm]

    table = Table(data, colWidths=col_widths, repeatRows=1)
    table.setStyle(TableStyle([
        # Opaque white base so the watermark never bleeds through data rows.
        ("BACKGROUND",  (0, 0), (-1, -1), colors.white),
        ("BACKGROUND",  (0, 0), (-1, 0), colors.HexColor("#e6e6ea")),
        ("FONTNAME",    (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE",    (0, 0), (-1, 0), 8),
        ("TEXTCOLOR",   (0, 0), (-1, 0), colors.HexColor("#111111")),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 6),
        ("TOPPADDING",  (0, 0), (-1, 0), 6),
        ("GRID",        (0, 0), (-1, -1), 0.3, colors.HexColor("#c9ccd2")),
        ("VALIGN",      (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING",  (0, 1), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 1), (-1, -1), 3),
        # Alternate row shading for readability on print.
        *[
            ("BACKGROUND", (0, i), (-1, i), colors.HexColor("#fafbfc"))
            for i in range(2, len(data), 2)
        ],
    ]))
    story.append(table)

    if not events:
        story.append(Spacer(0, 20))
        story.append(Paragraph("No events in the specified window.", meta_style))

    buf = _io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,   # PORTRAIT — printer-default friendly (1a, 2026-08-13)
        leftMargin=12*mm, rightMargin=12*mm,
        # Top+bottom margins expanded to clear the 30pt confidentiality
        # banner + footer drawn by _pdf_confidentiality_onpage.
        topMargin=22*mm, bottomMargin=15*mm,
        title="Quake Angel Audit Log",
        author=principal.get("email", ""),
    )
    # A2 (2026-08-17): audit PDF carries the same header marks as the
    # casualty reports — permanent Quake Angel logo, labelled partner
    # logo when a real one is configured.
    _onpage = _make_confidential_onpage(await _get_logo_image_reader())
    doc.build(story, onFirstPage=_onpage, onLaterPages=_onpage)
    pdf_bytes = buf.getvalue()

    filename = f"CONFIDENTIAL-quakeangel-audit-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Row-Count": str(len(events)),
        },
    )


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


class NotificationPresetBody(BaseModel):
    device_id: str = Field(..., min_length=3, max_length=200)
    preset: str = Field(..., description="One of: off, significant, noticeable, everything")


@api_router.get("/devices/{device_id}/notification-preset")
async def get_notification_preset(device_id: str):
    """Current informational-notification preset for a device.

    Public endpoint — device_id IS the auth, same trust model as /api/status.
    Returns the default preset ('noticeable') if never saved, so the
    mobile UI always has a value to display.
    """
    row = await db.push_devices.find_one(
        {"user_id": device_id}, {"_id": 0, "notification_preset": 1},
    )
    stored = (row or {}).get("notification_preset")
    return {
        "device_id": device_id,
        "preset": stored or DEFAULT_PRESET,
        "default_used": stored is None,
    }


@api_router.post("/devices/notification-preset")
async def set_notification_preset(body: NotificationPresetBody):
    """Update a device's informational-notification preset.

    Governs INFORMATIONAL (preview) notifications only. Critical alerts
    fire regardless — enforced by the separation of send paths
    (send_critical_alerts bypasses preset; preview dispatch respects it).
    Mobile settings screen MUST make this clear to the user.
    """
    preset = (body.preset or "").strip().lower()
    if preset not in VALID_PRESETS:
        raise HTTPException(
            400,
            f"preset must be one of {sorted(VALID_PRESETS)} (got '{body.preset}')",
        )
    now = datetime.now(timezone.utc)
    await db.push_devices.update_one(
        {"user_id": body.device_id},
        {
            "$set": {
                "notification_preset": preset,
                "notification_preset_updated_at": now,
            },
            "$setOnInsert": {"user_id": body.device_id, "created_at": now},
        },
        upsert=True,
    )
    return {"ok": True, "device_id": body.device_id, "preset": preset}


# ---------- B8: "places I care about" (optional named places) ------------
#
# Approved 2026-08-17 as the answer to "should users set their own
# notification radius" — a radius slider was considered and REJECTED
# (task #158): distance alone is the wrong variable (M6 at 300 km matters
# more than M2 at 20 km), and a dial invites misconfiguration in both
# directions — set small and people silently miss things, set large and
# they get flooded and switch notifications off entirely.
#
# Instead: a user optionally names places they care about (family in
# Sicily, a second home). Each place is evaluated with the SAME intensity
# logic as their own location — never a raw radius.
#
# HARD CONSTRAINT (safety): places affect INFORMATIONAL tremor notices
# only. They can never filter, delay or suppress the critical alert for
# the user's own location. That is guaranteed structurally, not by a flag:
# the critical path is send_critical_alerts() via /api/trigger-alert and
# the evaluator's critical branch, neither of which reads `user_places`
# at all. Place evaluation lives exclusively in emsc/preview.py.
#
# PRIVACY (flagged for the GDPR work, #75): a saved place is location data
# about OTHER PEOPLE (where your family lives). It needs covering in the
# privacy policy, in retention, and in erasure — deleting a device must
# delete its places.
MAX_PLACES_PER_DEVICE = 5


class PlaceBody(BaseModel):
    name: str = Field(..., min_length=1, max_length=40)
    latitude: float = Field(..., ge=-90, le=90)
    longitude: float = Field(..., ge=-180, le=180)


class PlacesEnabledBody(BaseModel):
    enabled: bool


@api_router.get("/devices/{device_id}/places")
async def list_places(device_id: str):
    """A device's saved places + the whole-feature on/off switch.

    Public endpoint — device_id IS the auth, same trust model as
    /api/status and /api/devices/{id}/notification-preset.
    """
    rows = await db.user_places.find(
        {"device_id": device_id}, {"_id": 0},
    ).sort("created_at", 1).to_list(MAX_PLACES_PER_DEVICE * 2)
    dev = await db.push_devices.find_one(
        {"user_id": device_id}, {"_id": 0, "places_enabled": 1},
    )
    enabled = (dev or {}).get("places_enabled")
    return {
        "device_id": device_id,
        # Default ON so that adding a place works immediately; the feature
        # is opt-in by virtue of having no places at all, so nothing nags
        # a user who never adds one.
        "enabled": True if enabled is None else bool(enabled),
        "max_places": MAX_PLACES_PER_DEVICE,
        "places": rows,
    }


@api_router.post("/devices/{device_id}/places")
async def add_place(device_id: str, body: PlaceBody):
    """Add a named place. Capped at MAX_PLACES_PER_DEVICE."""
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(400, "name is required")
    count = await db.user_places.count_documents({"device_id": device_id})
    if count >= MAX_PLACES_PER_DEVICE:
        raise HTTPException(
            400, f"You can save up to {MAX_PLACES_PER_DEVICE} places. Remove one first.",
        )
    now = datetime.now(timezone.utc).isoformat()
    doc = {
        "place_id": str(uuid.uuid4()),
        "device_id": device_id,
        "name": name,
        "latitude": body.latitude,
        "longitude": body.longitude,
        "created_at": now,
    }
    await db.user_places.insert_one(dict(doc))
    return {"ok": True, "place": doc}


@api_router.delete("/devices/{device_id}/places/{place_id}")
async def delete_place(device_id: str, place_id: str):
    """Remove one saved place. Notices for it stop immediately — the
    dispatch path reads `user_places` fresh on every event."""
    res = await db.user_places.delete_one(
        {"device_id": device_id, "place_id": place_id},
    )
    if res.deleted_count == 0:
        raise HTTPException(404, "No such place for this device")
    return {"ok": True, "deleted": place_id}


@api_router.post("/devices/{device_id}/places/enabled")
async def set_places_enabled(device_id: str, body: PlacesEnabledBody):
    """Whole-feature switch — silences every place at once without
    deleting any of them."""
    now = datetime.now(timezone.utc)
    await db.push_devices.update_one(
        {"user_id": device_id},
        {
            "$set": {"places_enabled": bool(body.enabled), "places_enabled_updated_at": now},
            "$setOnInsert": {"user_id": device_id, "created_at": now},
        },
        upsert=True,
    )
    return {"ok": True, "device_id": device_id, "enabled": bool(body.enabled)}


# ---------- Dual casualty reports: B1 Operational / B2 Public -----------
#
# Two PDF endpoints producing reports of who checked in with what status
# during a report window. They exist as a PAIR — building them
# separately would risk drift where the same real-world event produces
# inconsistent counts on operational vs public reports.
#
# B1 (Operational) — internal dispatch document. Contains everything
#   the Civil Protection team needs to allocate resources: names,
#   short codes, exact GPS + accuracy, battery, platform, notes,
#   status timeline.
#
# B2 (Public) — external comms document. **Aggregate numbers only. No
#   names, no initials, no short codes, no per-person location, no
#   per-person status — not even for rescued people.** See
#   `/app/memory/PRD.md` "Legal / privacy locks" section: this is a
#   GDPR + next-of-kin policy, not a UI preference. Any change that
#   would make B2 identifiable requires legal review before merge.
#
# Both reports use the same underlying data query so the counts on
# B2 are always internally consistent with the detail in B1. That's
# enforced by `_gather_devices_in_report_window()` — both endpoints
# call it, neither queries the DB independently.

async def _gather_devices_in_report_window(
    since_iso: Optional[str],
    until_iso: Optional[str],
) -> tuple[list[dict], datetime, datetime]:
    """Fetch every device with any status_event in [since, until], collapsed
    to the LATEST event per device (which is what the report describes).

    Returns:
      (list_of_latest_events, resolved_since_dt, resolved_until_dt)

    Each list item has extra fields:
      - `first_event_at`: when THIS device first reported in the window
      - `event_count`: how many status_events they logged in the window
    So B1 can show "checked in N times, latest at X" per row.
    """
    since_dt = _parse_iso_or_none(since_iso)
    until_dt = _parse_iso_or_none(until_iso)
    if since_dt is None:
        since_dt = datetime.now(timezone.utc) - timedelta(hours=24)
    if until_dt is None:
        until_dt = datetime.now(timezone.utc)
    if until_dt < since_dt:
        raise HTTPException(400, "until must be >= since")
    if (until_dt - since_dt).days > MAX_EXPORT_WINDOW_DAYS:
        raise HTTPException(
            400,
            f"Window too wide (max {MAX_EXPORT_WINDOW_DAYS} days).",
        )

    # `recorded_at` is stored as an ISO string in status_events (see the
    # sample rows in mongo) so we compare against ISO strings, not
    # datetime objects. String comparison of ISO-8601 UTC values is
    # lexicographically correct.
    q = {"recorded_at": {"$gte": since_dt.isoformat(), "$lte": until_dt.isoformat()}}
    rows = await db.status_events.find(q, {"_id": 0}).sort("recorded_at", -1).to_list(5000)

    # Collapse to latest-per-device.
    by_device: dict[str, dict] = {}
    for r in rows:
        did = r.get("device_id")
        if not did:
            continue
        if did not in by_device:
            by_device[did] = {**r, "event_count": 1, "first_event_at": r.get("recorded_at")}
        else:
            by_device[did]["event_count"] += 1
            # `first_event_at` tracks the oldest event; since we're
            # sorted newest-first, every subsequent hit is older.
            by_device[did]["first_event_at"] = r.get("recorded_at")

    # Reverted rescues are marked with `rescue_reverted=True` on a
    # follow-up event. For the report we want the *effective current
    # status* — which is exactly what the latest event says, since
    # a revert reinstates the prior_status on that same event row.
    # `rows` (uncollapsed) is returned too — the response-over-time chart
    # needs every event, not just the latest per device.
    return list(by_device.values()), since_dt, until_dt, rows


def _bucket_by_status(events: list[dict]) -> dict:
    """Aggregate counts by status/severity for both reports.

    B2 exposes only the totals from here; B1 shows totals AND names.
    """
    total = len(events)
    safe = trapped = rescued = 0
    trapped_red = trapped_yellow = trapped_green = trapped_unknown = 0
    for e in events:
        st = e.get("status")
        if st == "safe":
            safe += 1
        elif st == "rescued":
            rescued += 1
        elif st == "trapped":
            trapped += 1
            sev = (e.get("severity") or "").lower()
            if sev == "red":
                trapped_red += 1
            elif sev == "yellow":
                trapped_yellow += 1
            elif sev == "green":
                trapped_green += 1
            else:
                trapped_unknown += 1
    return {
        "total_devices": total,
        "safe": safe,
        "trapped": trapped,
        "rescued": rescued,
        "trapped_red": trapped_red,
        "trapped_yellow": trapped_yellow,
        "trapped_green": trapped_green,
        "trapped_unknown": trapped_unknown,
        "awaiting_rescue": trapped,   # semantic alias — "trapped" == "needs help / awaiting rescue"
    }


def _pdf_common_setup():
    """Import ReportLab + build shared paragraph styles. Called by both
    B1 and B2 so styling stays consistent (a subtle wording gap between
    the two reports would be more suspicious than obvious)."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import (
        SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    )
    styles = getSampleStyleSheet()
    return {
        "colors": colors, "A4": A4, "landscape": landscape,
        "styles": styles, "ParagraphStyle": ParagraphStyle, "mm": mm,
        "SimpleDocTemplate": SimpleDocTemplate, "Paragraph": Paragraph,
        "Spacer": Spacer, "Table": Table, "TableStyle": TableStyle,
    }


@api_router.get("/admin/casualty-report/operational.pdf")
async def casualty_report_operational_pdf(
    request: Request,
    since: Optional[str] = Query(default=None, description="ISO 8601 start; default 24h ago."),
    until: Optional[str] = Query(default=None, description="ISO 8601 end; default now."),
    pseudonymise: bool = Query(default=False, description="Replace operator emails with stable pseudonyms (operator-N)."),
    detail: str = Query(default="summary", pattern="^(full|summary)$",
                        description="'summary' (default) is aggregate + timeline only; "
                                    "'full' adds one table row per device (issue #130: "
                                    "the multi-page table is opt-in, never the default)."),
):
    """B1 — Operational casualty report. Admin+operator gated.

    Full-detail internal document. Contains names, short codes, exact
    GPS, notes, timeline. Suitable for Civil Protection dispatch.
    """
    principal = await resolve_principal(
        request,
        request.headers.get("x-admin-token"),
        ADMIN_TRIGGER_PASSWORD,
        db,
    )
    require_role(principal, "admin", "operator")

    events, since_dt, until_dt, raw_rows = await _gather_devices_in_report_window(since, until)
    counts = _bucket_by_status(events)
    authority = await _get_authority_name()   # for the footer copy — never hard-coded
    await _backfill_display_names(events)
    generated_by = principal.get("email", "?")
    if pseudonymise and "@" in generated_by:
        generated_by = await _operator_alias(generated_by)

    r = _pdf_common_setup()
    colors, mm = r["colors"], r["mm"]
    Paragraph, Spacer, Table, TableStyle = r["Paragraph"], r["Spacer"], r["Table"], r["TableStyle"]
    styles, PS = r["styles"], r["ParagraphStyle"]

    title_style = PS("T", parent=styles["Heading1"], fontSize=14, spaceAfter=6, textColor=colors.HexColor("#111"))
    meta_style  = PS("M", parent=styles["Normal"], fontSize=8, textColor=colors.HexColor("#555"), spaceAfter=8)
    h2_style    = PS("H2", parent=styles["Heading2"], fontSize=11, spaceAfter=4, textColor=colors.HexColor("#111"))
    cell_style  = PS("C", parent=styles["Normal"], fontSize=7, leading=9, wordWrap="CJK")
    cell_bold   = PS("CB", parent=cell_style, fontName="Helvetica-Bold")
    footer_style = PS("F", parent=styles["Normal"], fontSize=7, textColor=colors.HexColor("#8a1a1a"), spaceBefore=10)

    def cell(s, style=cell_style):
        if s is None or s == "":
            return Paragraph("&nbsp;", style)
        return Paragraph(_html.escape(str(s)), style)

    story = [
        Paragraph("Team report — operational casualty report", title_style),
        Paragraph(
            f"CONFIDENTIAL — INTERNAL USE ONLY. Contains personally identifiable information. "
            f"Not for public distribution.",
            PS("CI", parent=meta_style, textColor=colors.HexColor("#8a1a1a"), fontName="Helvetica-Bold"),
        ),
        # Plain-words absolute coverage (1c).
        Paragraph(_covers_line(since_dt, until_dt), PS(
            "B1Covers", parent=styles["Normal"], fontSize=9.5,
            textColor=colors.HexColor("#222222"), spaceAfter=4,
        )),
        Paragraph(
            f"Total devices reporting: {counts['total_devices']} &nbsp;·&nbsp; "
            f"Generated: {datetime.now(timezone.utc).isoformat()} &nbsp;·&nbsp; "
            f"By: {generated_by}",
            meta_style,
        ),
        Paragraph("Aggregate", h2_style),
    ]
    _gap = await _window_gap_warning(since_dt)
    if _gap:
        story.insert(3, Paragraph(_html.escape(_gap), PS(
            "B1Warn", parent=styles["Normal"], fontSize=9,
            textColor=colors.HexColor("#B0141A"), spaceAfter=8,
            fontName="Helvetica-Bold",
        )))

    summary_data = [
        ["", "Count"],
        ["Safe", str(counts["safe"])],
        ["Trapped / needs help (red)",    str(counts["trapped_red"])],
        ["Trapped / needs help (yellow)", str(counts["trapped_yellow"])],
        ["Trapped / needs help (green)",  str(counts["trapped_green"])],
        ["Trapped — severity not set",    str(counts["trapped_unknown"])],
        ["Rescued",                       str(counts["rescued"])],
        ["Total",                         str(counts["total_devices"])],
    ]
    summary_tbl = Table(summary_data, colWidths=[70*mm, 30*mm])
    summary_tbl.setStyle(TableStyle([
        # Opaque white base so the watermark never bleeds through data rows.
        ("BACKGROUND", (0,0), (-1,-1), colors.white),
        ("BACKGROUND", (0,0), (-1,0), colors.HexColor("#e6e6ea")),
        ("FONTNAME",   (0,0), (-1,0), "Helvetica-Bold"),
        ("FONTSIZE",   (0,0), (-1,-1), 8),
        ("GRID",       (0,0), (-1,-1), 0.3, colors.HexColor("#c9ccd2")),
        ("BACKGROUND", (0,-1), (-1,-1), colors.HexColor("#f0f2f6")),
        ("FONTNAME",   (0,-1), (-1,-1), "Helvetica-Bold"),
    ]))
    story.append(summary_tbl)
    story.append(Spacer(0, 10))

    # ── Response over time ──────────────────────────────────────────
    # Chart + plain-language progress (no percentages — see
    # _plain_language_progress hard locks).
    story.append(Paragraph("Response over time", h2_style))
    plain_style = PS("PL", parent=styles["Normal"], fontSize=9, leading=13, spaceAfter=1)
    buckets, _hourly = _bucket_timeline(raw_rows, since_dt, until_dt)
    if any(b["trapped"] or b["safe"] or b["rescued"] for b in buckets):
        story.append(_timeline_chart(buckets, 182 * mm, 55 * mm))
        story.append(Spacer(0, 6))
    from reportlab.platypus import KeepTogether as _KT
    _narrative_lines = _plain_language_progress(raw_rows, events, counts)
    # Low battery = countdown to losing contact (item 4). Plain words,
    # states the total, never a bare percentage.
    _narrative_lines.extend(_low_battery_lines(events))
    story.append(_KT([
        Paragraph(_html.escape(line), plain_style)
        for line in _narrative_lines
    ]))
    story.append(Spacer(0, 10))

    if detail == "full":
        story.append(Paragraph("Per-device detail", h2_style))
        header = ["Latest status", "Sev", "Name / code", "Latest at (UTC)", "Location", "Battery", "Platform", "Notes"]

        def _sort_key(e):
            # Sort: trapped > rescued > safe; within status, red > yellow > green > unknown; then newest first.
            rank_status = {"trapped": 0, "rescued": 1, "safe": 2}.get(e.get("status"), 3)
            rank_sev = {"red": 0, "yellow": 1, "green": 2}.get((e.get("severity") or "").lower(), 3)
            # Recorded_at is an ISO 8601 string — lexicographic desc sort by
            # negating via a tuple companion is cleanest without parsing.
            return (rank_status, rank_sev, "" if e.get("recorded_at") is None else e["recorded_at"])

        # Sort by (rank_status, rank_sev, recorded_at) ascending on the ranks
        # and DESCENDING on recorded_at (newest first within each group).
        events_sorted = sorted(events, key=_sort_key)
        events_sorted.sort(key=lambda e: e.get("recorded_at") or "", reverse=True)
        events_sorted.sort(key=lambda e: (
            {"trapped": 0, "rescued": 1, "safe": 2}.get(e.get("status"), 3),
            {"red": 0, "yellow": 1, "green": 2}.get((e.get("severity") or "").lower(), 3),
        ))
        data = [header]
        _b1_codes = _short_codes_for({e.get("device_id") for e in events_sorted if e.get("device_id")})
        _b1_trapped_since = await _trapped_since_map(
            [e.get("device_id") for e in events_sorted if e.get("status") == "trapped"]
        )
        _now_utc = datetime.now(timezone.utc)
        for e in events_sorted:
            lat = e.get("latitude"); lon = e.get("longitude")
            loc = "—"
            if lat is not None and lon is not None:
                loc = f"{_round5(lat)}, {_round5(lon)}"
                if e.get("accuracy_m"):
                    loc += f" ±{e.get('accuracy_m'):.0f}m"
            batt = "—"
            if e.get("battery_pct") is not None:
                batt = f"{e.get('battery_pct')}%"
                if e.get("battery_state"):
                    batt += f" ({e.get('battery_state')})"
            name = e.get("display_name") or "(anonymous)"
            code = _b1_codes.get(e.get("device_id")) or _short_code(e.get("device_id"))
            # Escape the VALUES, not the markup — passing the composed string
            # through cell() double-escaped it and printed literal "<br/>"
            # tags on the PDF (bug #3.1, 2026-08-12).
            name_para = Paragraph(
                f"{_html.escape(str(name))}<br/>"
                f"<font size=6 color='#666'>{_html.escape(str(code or ''))}</font>",
                cell_style,
            )

            status_display = e.get("status") or "?"
            if e.get("rescue_reverted"):
                status_display = f"{status_display} (reverted)"
            # 'Trapped for …' in words (item 3) — the operationally critical
            # figure, same wording as the dashboard card.
            _ts = _b1_trapped_since.get(e.get("device_id")) if e.get("status") == "trapped" else None
            if _ts:
                try:
                    _tdt = datetime.fromisoformat(str(_ts).replace("Z", "+00:00"))
                    if _tdt.tzinfo is None:
                        _tdt = _tdt.replace(tzinfo=timezone.utc)
                    status_para = Paragraph(
                        f"<b>{_html.escape(status_display)}</b><br/>"
                        f"<font size=6 color='#666'>trapped for {_html.escape(_duration_words(_now_utc - _tdt))}</font>",
                        cell_style,
                    )
                except (ValueError, TypeError):
                    status_para = cell(status_display, cell_bold)
            else:
                status_para = cell(status_display, cell_bold)

            data.append([
                status_para,
                cell((e.get("severity") or "—")),
                name_para,
                cell(e.get("recorded_at") or ""),
                cell(loc),
                cell(batt),
                cell(e.get("platform") or "—"),
                cell(e.get("notes") or "—"),
            ])

        # Column widths sum to 186mm — PORTRAIT A4 (210mm − 24mm margins).
        col_widths = [17*mm, 10*mm, 28*mm, 27*mm, 34*mm, 17*mm, 14*mm, 39*mm]
        tbl = Table(data, colWidths=col_widths, repeatRows=1)
        tbl.setStyle(TableStyle([
            # Opaque white base so the watermark never bleeds through rows.
            ("BACKGROUND",   (0,0), (-1,-1), colors.white),
            ("BACKGROUND",   (0,0), (-1,0), colors.HexColor("#e6e6ea")),
            ("FONTNAME",     (0,0), (-1,0), "Helvetica-Bold"),
            ("FONTSIZE",     (0,0), (-1,0), 7),
            ("GRID",         (0,0), (-1,-1), 0.3, colors.HexColor("#c9ccd2")),
            ("VALIGN",       (0,0), (-1,-1), "TOP"),
            ("LEFTPADDING",  (0,0), (-1,-1), 3),
            ("RIGHTPADDING", (0,0), (-1,-1), 3),
        ]))
        story.append(tbl)
    else:
        story.append(Paragraph(
            "Per-device detail omitted — this is the summary version. "
            "Download the full version for the per-device table.",
            meta_style,
        ))

    if counts["total_devices"] == 0:
        story.append(Spacer(0, 20))
        story.append(Paragraph("No devices reported in the specified window.", meta_style))

    story.append(Paragraph(
        "END OF TEAM REPORT — For public communications, use the \u201Csafe to share\u201D public report which "
        "exposes aggregate numbers only. Do NOT share this document publicly, with press, or with "
        f"next-of-kin before {authority} has completed formal notification.",
        footer_style,
    ))

    buf = _io.BytesIO()
    doc = r["SimpleDocTemplate"](
        buf,
        pagesize=r["A4"],   # PORTRAIT — printer-default friendly (1a, 2026-08-13)
        leftMargin=12*mm, rightMargin=12*mm,
        # Expanded top+bottom margins to clear the 30pt confidentiality
        # banner + footer drawn by _pdf_confidentiality_onpage.
        topMargin=22*mm, bottomMargin=15*mm,
        title="Quake Angel Team Report (operational casualty report)",
        author=principal.get("email", ""),
    )
    _onpage = _make_confidential_onpage(await _get_logo_image_reader())
    doc.build(story, onFirstPage=_onpage, onLaterPages=_onpage)
    _suffix = "-summary" if detail == "summary" else ""
    return Response(
        content=buf.getvalue(),
        media_type="application/pdf",
        headers={
            # Plain-language filename (issue #133) — "team-report", not "B1".
            "Content-Disposition": f'attachment; filename="CONFIDENTIAL-quakeangel-team-report{_suffix}-{datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")}.pdf"',
            "X-Row-Count": str(counts["total_devices"]),
            # X-Report-Kind lets the dashboard sanity-check that clicking "B1"
            # actually returned a B1 (defense-in-depth against endpoint mixup).
            "X-Report-Kind": "B1-operational",
        },
    )


@api_router.get("/admin/casualty-report/public.pdf")
async def casualty_report_public_pdf(
    request: Request,
    since: Optional[str] = Query(default=None, description="ISO 8601 start; default 24h ago."),
    until: Optional[str] = Query(default=None, description="ISO 8601 end; default now."),
):
    """B2 — Public casualty report. Admin+operator gated.

    **Aggregate counts only.** This PDF is safe to share externally
    (press briefings, family info line, public dashboards). It contains:
        - How many people checked in safe
        - How many people are awaiting rescue (with severity breakdown)
        - How many people have been rescued
        - No names, no initials, no codes, no per-person location, no
          per-person status. Not for rescued people either.

    Legal + next-of-kin policy locked with Paul 2026-08-07 (see PRD
    "Legal / privacy locks" section). Any change to identifiability
    requires legal review, in writing, before merge.
    """
    principal = await resolve_principal(
        request,
        request.headers.get("x-admin-token"),
        ADMIN_TRIGGER_PASSWORD,
        db,
    )
    require_role(principal, "admin", "operator")

    events, since_dt, until_dt, raw_rows = await _gather_devices_in_report_window(since, until)
    counts = _bucket_by_status(events)
    authority = await _get_authority_name()   # configurable, never hard-coded

    # Belt-and-braces assertion. If a future refactor accidentally left
    # a per-person field in `counts`, this assertion refuses to render
    # the report. Fail-loud is the correct behavior — the legal lock
    # matters more than uptime of this specific endpoint.
    # Whitelist of keys expected to be safe (all integer counts):
    _B2_SAFE_KEYS = {
        "total_devices", "safe", "trapped", "rescued",
        "trapped_red", "trapped_yellow", "trapped_green", "trapped_unknown",
        "awaiting_rescue",
    }
    _leaked = [k for k in counts if k not in _B2_SAFE_KEYS]
    if _leaked:
        raise HTTPException(
            500,
            "B2 aggregate structure changed unexpectedly. Refusing to render "
            "for privacy safety. See PRD 'Legal / privacy locks' section. "
            f"Unexpected keys: {_leaked}",
        )

    r = _pdf_common_setup()
    colors, mm = r["colors"], r["mm"]
    Paragraph, Spacer, Table, TableStyle = r["Paragraph"], r["Spacer"], r["Table"], r["TableStyle"]
    styles, PS = r["styles"], r["ParagraphStyle"]

    title_style = PS("T", parent=styles["Heading1"], fontSize=16, spaceAfter=8, textColor=colors.HexColor("#111"))
    meta_style  = PS("M", parent=styles["Normal"], fontSize=9, textColor=colors.HexColor("#444"), spaceAfter=12)
    h2_style    = PS("H2", parent=styles["Heading2"], fontSize=12, spaceAfter=6, textColor=colors.HexColor("#111"))
    body_style  = PS("B", parent=styles["Normal"], fontSize=10, leading=14, spaceAfter=6)
    footer_style = PS("F", parent=styles["Normal"], fontSize=8, textColor=colors.HexColor("#666"), spaceBefore=14)

    story = [
        Paragraph("Public status report", title_style),
        # Plain-words absolute coverage (1c) — press and families read this
        # long after "the last 24 hours" has lost its meaning.
        Paragraph(_covers_line(since_dt, until_dt), PS(
            "B2Covers", parent=body_style, fontSize=10, spaceAfter=4,
        )),
        Paragraph(
            f"Issued: {datetime.now(timezone.utc).strftime('%d %B %Y, %H:%M').lstrip('0')} (UTC)",
            meta_style,
        ),
        # Issuer line (3a, 2026-08-13): names the SYSTEM and authority so
        # the document is attributable to Quake Angel's official output —
        # never the individual operator (no personal email on a document
        # going to press and families).
        Paragraph(
            f"Issued by the Quake Angel emergency response system, "
            f"in cooperation with {authority}.",
            meta_style,
        ),
        Paragraph("Current status of people using the Quake Angel app in the affected area:", body_style),
    ]
    _gap = await _window_gap_warning(since_dt)
    if _gap:
        story.insert(3, Paragraph(_html.escape(_gap), PS(
            "B2Warn", parent=body_style, fontSize=10,
            textColor=colors.HexColor("#B0141A"), fontName="Helvetica-Bold", spaceAfter=6,
        )))

    # Aggregate table — ONLY counts. Deliberately no "who" column, no
    # region column, no timestamp-of-latest-event column.
    summary_data = [
        ["", ""],
        ["People checked in as safe",                str(counts["safe"])],
        ["People rescued",                           str(counts["rescued"])],
        ["People awaiting rescue",                   str(counts["awaiting_rescue"])],
        ["  — of which reporting critical injury",   str(counts["trapped_red"])],
        ["  — of which reporting moderate injury",   str(counts["trapped_yellow"])],
        ["  — of which reporting minor injury",      str(counts["trapped_green"])],
        ["Total people accounted for",               str(counts["total_devices"])],
    ]
    summary_tbl = Table(summary_data, colWidths=[110*mm, 30*mm])
    summary_tbl.setStyle(TableStyle([
        # Opaque white base so nothing bleeds through on print.
        ("BACKGROUND", (0,0), (-1,-1), colors.white),
        ("FONTNAME",   (0,0), (-1,-1), "Helvetica"),
        ("FONTSIZE",   (0,0), (-1,-1), 10),
        ("BACKGROUND", (0,-1), (-1,-1), colors.HexColor("#f0f2f6")),
        ("FONTNAME",   (0,-1), (-1,-1), "Helvetica-Bold"),
        ("BOTTOMPADDING", (0,0), (-1,-1), 5),
        ("TOPPADDING",    (0,0), (-1,-1), 5),
        ("LINEABOVE",  (0,1), (-1,1), 0.5, colors.HexColor("#666")),
        ("LINEABOVE",  (0,-1), (-1,-1), 0.5, colors.HexColor("#666")),
        ("ALIGN",      (1,0), (1,-1), "RIGHT"),
    ]))
    story.append(summary_tbl)

    # ── How the situation has changed over time ─────────────────────
    # Counts only — NO percentages on B2. A bare "68% rescued" would be
    # quoted by press as 68% of everyone caught in the earthquake, when
    # the denominator is only app users who checked in. Locked with
    # Paul 2026-08-12; see PRD "Legal / privacy locks".
    #
    # The heading, chart AND its plain-language explanation are ONE
    # KeepTogether block (3b, 2026-08-13): a reader must never have to
    # turn the page to find out what the chart means.
    story.append(Spacer(0, 12))
    buckets, _hourly = _bucket_timeline(raw_rows, since_dt, until_dt)
    from reportlab.platypus import KeepTogether as _KT
    timeline_block = [Paragraph("How the situation has changed over time", h2_style)]
    if any(b["trapped"] or b["safe"] or b["rescued"] for b in buckets):
        timeline_block.append(_timeline_chart(buckets, 170 * mm, 50 * mm))
        timeline_block.append(Spacer(0, 6))
    timeline_block.extend(
        Paragraph(_html.escape(line), body_style)
        for line in _plain_language_progress(raw_rows, events, counts)
    )
    story.append(_KT(timeline_block))

    story.append(Spacer(0, 14))
    story.append(Paragraph(
        "Notes: These counts reflect app users who have checked in via Quake Angel during the "
        "window shown. They do not represent the total affected population. Individual "
        "identities are not disclosed in this report to protect privacy and to preserve formal "
        f"next-of-kin notification procedures conducted by {authority}.",
        footer_style,
    ))

    buf = _io.BytesIO()
    doc = r["SimpleDocTemplate"](
        buf,
        pagesize=r["A4"],   # PORTRAIT — printer-default friendly (1a, 2026-08-13)
        leftMargin=18*mm, rightMargin=18*mm,
        topMargin=15*mm, bottomMargin=15*mm,
        title="Quake Angel Public Status Report",
        author=principal.get("email", ""),
    )
    _onpage = _make_public_onpage(await _get_logo_image_reader())
    doc.build(story, onFirstPage=_onpage, onLaterPages=_onpage)
    return Response(
        content=buf.getvalue(),
        media_type="application/pdf",
        headers={
            # Plain-language filename (issue #133) — "public-report", not "B2".
            "Content-Disposition": f'attachment; filename="quakeangel-public-report-{datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")}.pdf"',
            # No X-Row-Count on B2 — the aggregate table IS the counts, and
            # exposing "N devices" in a header could be considered
            # identifiability-adjacent for very small N. Erring cautiously.
            "X-Report-Kind": "B2-public",
        },
    )







# ---------- Dashboard settings (org logo + authority name) --------------
#
# Public-readable, admin-writeable single-document config.
#
# GET is public because the logo needs to render on the anonymous
# dashboard-load path (no JWT yet at render time); operators viewing
# the dashboard before signing in shouldn't see a broken image. The
# authority name is not sensitive either — it's meant to appear on
# publishable PDFs.
#
# POST /logo accepts JSON: {"logo_b64": "<base64>", "mime": "image/png"|"image/svg+xml"}
# POST /authority-name accepts JSON: {"authority_name": "<string>"|null}
#
# Size caps (enforced at API):
#   PNG ≤ 200 KB decoded — a header logo bigger than this is a design
#     mistake in the source file, not a legitimate use case.
#   SVG ≤ 100 KB source — SVG scales natively; anything bigger is
#     almost certainly an unoptimised export from Illustrator/Figma.

class DashboardLogoBody(BaseModel):
    logo_b64: str = Field(..., min_length=32, description="base64-encoded logo bytes (data-URI stripped)")
    mime: str = Field(..., description="'image/png' or 'image/svg+xml'")


class DashboardAuthorityBody(BaseModel):
    # Optional string. Empty/whitespace/None clears the setting and the
    # PDFs revert to the generic "the responsible authorities" phrasing.
    authority_name: Optional[str] = Field(default=None, max_length=120)


_LOGO_ALLOWED_MIME = {"image/png", "image/svg+xml"}
_LOGO_MAX_BYTES_PNG = 200 * 1024
_LOGO_MAX_BYTES_SVG = 100 * 1024


@api_router.get("/dashboard-settings")
async def dashboard_settings_get():
    """Public read of dashboard settings. Returns logo (base64) if set,
    authority name if set, and last-updated metadata. Called on every
    dashboard load — kept small and fast (single-doc find)."""
    s = await _get_dashboard_settings()
    # Deliberately DON'T include _id or updated_by metadata in the
    # anonymous public response — that's operator identity.
    return {
        "authority_name": s.get("authority_name") or None,
        "logo_b64": s.get("logo_b64") or None,
        "logo_mime": s.get("logo_mime") or None,
    }


@api_router.post("/admin/dashboard-settings/logo")
async def dashboard_settings_set_logo(
    request: Request,
    body: DashboardLogoBody,
):
    """Upload/replace the org logo. Admin-only."""
    principal = await resolve_principal(
        request,
        request.headers.get("x-admin-token"),
        ADMIN_TRIGGER_PASSWORD,
        db,
    )
    require_role(principal, "admin")

    mime = body.mime.strip().lower()
    if mime not in _LOGO_ALLOWED_MIME:
        raise HTTPException(400, f"Unsupported logo type: {mime}. Allowed: {sorted(_LOGO_ALLOWED_MIME)}")

    # Strip any accidental data-URI prefix client-side operators might paste
    b64 = body.logo_b64
    if b64.startswith("data:"):
        try:
            b64 = b64.split(",", 1)[1]
        except IndexError:
            raise HTTPException(400, "Malformed data URI — expected 'data:...;base64,<payload>'")

    import base64
    try:
        raw = base64.b64decode(b64, validate=True)
    except Exception as e:
        raise HTTPException(400, f"Not valid base64: {e}")

    # Size cap by mime — see rationale in section header above.
    cap = _LOGO_MAX_BYTES_PNG if mime == "image/png" else _LOGO_MAX_BYTES_SVG
    if len(raw) > cap:
        raise HTTPException(
            413,
            f"Logo too large: {len(raw)} bytes; max {cap} bytes for {mime}. "
            f"Optimise the source file (tinypng.com / SVGO) and re-upload."
        )

    # Minimal validation:
    #   PNG must start with the 8-byte magic \x89PNG\r\n\x1a\n
    #   SVG must contain "<svg" case-insensitively in the first 500 bytes
    # This catches "operator pastes a JPG renamed to .png" without needing
    # a full image-parsing dependency.
    if mime == "image/png":
        if not raw.startswith(b"\x89PNG\r\n\x1a\n"):
            raise HTTPException(400, "Payload doesn't look like a PNG (magic-byte check failed).")
    else:  # SVG
        head = raw[:500].lower()
        if b"<svg" not in head:
            raise HTTPException(400, "Payload doesn't look like SVG (missing <svg root element).")

    now = datetime.now(timezone.utc)
    await db.dashboard_settings.update_one(
        {"_id": DASHBOARD_SETTINGS_ID},
        {"$set": {
            "logo_b64": b64,
            "logo_mime": mime,
            "logo_updated_at": now,
            "logo_updated_by": principal.get("email", "unknown"),
        }},
        upsert=True,
    )
    return {"ok": True, "bytes": len(raw), "mime": mime}


@api_router.delete("/admin/dashboard-settings/logo")
async def dashboard_settings_clear_logo(request: Request):
    """Remove the uploaded logo — dashboard reverts to Quake Angel branding
    only. Admin-only."""
    principal = await resolve_principal(
        request,
        request.headers.get("x-admin-token"),
        ADMIN_TRIGGER_PASSWORD,
        db,
    )
    require_role(principal, "admin")
    await db.dashboard_settings.update_one(
        {"_id": DASHBOARD_SETTINGS_ID},
        {"$unset": {"logo_b64": "", "logo_mime": "", "logo_updated_at": "", "logo_updated_by": ""}},
    )
    return {"ok": True, "cleared": True}


@api_router.post("/admin/dashboard-settings/authority-name")
async def dashboard_settings_set_authority(
    request: Request,
    body: DashboardAuthorityBody,
):
    """Set/clear the authority name shown on B1/B2 footers. Admin-only.
    Passing an empty string or null clears — reports fall back to the
    generic "the responsible authorities" wording."""
    principal = await resolve_principal(
        request,
        request.headers.get("x-admin-token"),
        ADMIN_TRIGGER_PASSWORD,
        db,
    )
    require_role(principal, "admin")

    name = (body.authority_name or "").strip()
    now = datetime.now(timezone.utc)
    if not name:
        await db.dashboard_settings.update_one(
            {"_id": DASHBOARD_SETTINGS_ID},
            {"$unset": {"authority_name": ""},
             "$set": {"authority_updated_at": now, "authority_updated_by": principal.get("email", "unknown")}},
            upsert=True,
        )
        return {"ok": True, "authority_name": None}
    await db.dashboard_settings.update_one(
        {"_id": DASHBOARD_SETTINGS_ID},
        {"$set": {
            "authority_name": name,
            "authority_updated_at": now,
            "authority_updated_by": principal.get("email", "unknown"),
        }},
        upsert=True,
    )
    return {"ok": True, "authority_name": name}


# ---------- Subscription entitlement (Phase A of subscription-lapse work) ----
#
# Public GET: any device may read its own entitlement state — same trust
# model as /api/status and /api/devices/{id}/notification-preset. No PII
# returned. Response shape is defined in entitlements.public_entitlement_view.
#
# Admin POST /entitlement/test-override: lets the dashboard flip any
# device into any state for QA of banner copy on real hardware. This
# exists BECAUSE we don't have real StoreKit integration yet — Phase C
# will add App Store Server Notification v2 as the real state driver
# and this override remains only as an escape hatch (with an audit
# trail via the entitlement doc's `history` array).
#
# What is NOT here (deliberately):
# - No purchase-flow endpoint. StoreKit purchase happens client-side.
# - No receipt validation. Deferred to Phase C with Apple's signed ASN2.
# - No feature-gating logic. The mobile client asks whether to show a
#   banner, not whether to enable individual features. Feature gating
#   (when it lands) checks `plan` on this same doc.

class EntitlementTestOverrideBody(BaseModel):
    device_id: str = Field(..., min_length=3, max_length=200)
    state: str = Field(..., description="One of: active, grace, lapsed, never_subscribed")
    expiration_reason: Optional[str] = Field(
        None, description="One of: voluntary, billing_issue, price_increase_declined, product_not_available",
    )
    grace_days_from_now: Optional[int] = Field(
        None, description="If state='grace', number of days until grace_ends_at (default 7).", ge=0, le=60,
    )


@api_router.get("/entitlement")
async def get_entitlement(device_id: str):
    """Current subscription entitlement + banner spec for a device.

    Query param `device_id` (not path param) so the client can hit this
    with the same identity it uses for /api/notification-preset.

    Always safe to call. Never returns 404 — a never-seen device just
    gets the `never_subscribed` state with no banner.

    INVARIANT: `critical_alerts_active` is always True. This field is
    the API's promise to the mobile client that the siren stays on
    regardless of subscription state.
    """
    if not device_id or len(device_id) < 3:
        raise HTTPException(400, "device_id required")
    doc = await db.entitlements.find_one({"user_id": device_id}, {"_id": 0})
    return public_entitlement_view(doc)


@api_router.post("/entitlement/test-override")
async def set_entitlement_test_override(body: EntitlementTestOverrideBody, request: Request):
    """Admin-only: flip a device's entitlement to any state for QA.

    Persists into `test_state_override` on the entitlement doc, which
    is what `compute_current_state` consults first. Cleared via
    /entitlement/test-override/clear.

    Restricted to admin because misuse could show alarming banners to
    real users. Operators (dispatch role) don't need this.
    """
    principal = await resolve_principal(
        request,
        request.headers.get("x-admin-token"),
        ADMIN_TRIGGER_PASSWORD,
        db,
    )
    require_role(principal, "admin")

    if body.state not in ENTITLEMENT_VALID_STATES:
        raise HTTPException(400, f"state must be one of {sorted(ENTITLEMENT_VALID_STATES)}")
    if body.expiration_reason is not None and body.expiration_reason not in ENTITLEMENT_VALID_REASONS:
        raise HTTPException(400, f"expiration_reason must be one of {sorted(ENTITLEMENT_VALID_REASONS)}")

    grace_ends_at = None
    if body.state == "grace":
        days = body.grace_days_from_now if body.grace_days_from_now is not None else GRACE_PERIOD_DAYS
        grace_ends_at = datetime.now(timezone.utc) + timedelta(days=days)

    override = {
        "state": body.state,
        "expiration_reason": body.expiration_reason,
        "grace_ends_at": grace_ends_at,
        "set_by": (principal.get("email") if principal else "unknown"),
        "set_at": datetime.now(timezone.utc),
    }

    # Also write the "real" fields so a subsequent read without override
    # still lands in a sensible place. The override is what the
    # mobile client sees; the underlying state matches so history is
    # readable.
    doc = await upsert_entitlement(
        db,
        user_id=body.device_id,
        state=body.state,
        expiration_reason=body.expiration_reason,
        grace_ends_at=grace_ends_at,
        source=f"admin_override:{principal.get('email') if principal else 'unknown'}",
        test_state_override=override,
    )
    return {
        "ok": True,
        "device_id": body.device_id,
        "entitlement": public_entitlement_view(doc),
    }


@api_router.post("/entitlement/test-override/clear")
async def clear_entitlement_test_override(device_id: str, request: Request):
    """Admin-only: remove any test override for a device. Returns the
    device to its real (or default) entitlement state.
    """
    principal = await resolve_principal(
        request,
        request.headers.get("x-admin-token"),
        ADMIN_TRIGGER_PASSWORD,
        db,
    )
    require_role(principal, "admin")
    if not device_id or len(device_id) < 3:
        raise HTTPException(400, "device_id required")
    await clear_test_override(db, device_id)
    doc = await db.entitlements.find_one({"user_id": device_id}, {"_id": 0})
    return {
        "ok": True,
        "device_id": device_id,
        "entitlement": public_entitlement_view(doc),
    }



# ---------- Public seismic-map endpoint (mobile in-app map) ----------
#
# The mobile app shows a Mediterranean-wide informational map of recent
# seismic activity. This is INFORMATIONAL only — the same class of public
# post-event data EMSC and USGS publish on their own websites.
#
# Deliberately unauthenticated:
# - Same trust model as /api/status: device_id is the identity, no PII returned.
# - Rate-limiting is upstream (proxy) — this endpoint just serves cached
#   data already in Mongo, no re-fetch from EMSC/USGS per request.
#
# NOT an early-warning feed. The mobile UI states this explicitly. The
# `observed_at` timestamps are strictly historical.

# Mediterranean bbox — deliberately wide. PRD: "Map always shows full
# Mediterranean regardless of notification preset." User preset only
# governs the indicative-radius circle overlay, not the query.
MED_BBOX_LAT_MIN = 30.0
MED_BBOX_LAT_MAX = 47.0
MED_BBOX_LON_MIN = -6.0
MED_BBOX_LON_MAX = 37.0

# Hard caps so a misbehaving client can't ask for millions of rows.
MAP_WINDOW_HOURS_MAX = 24 * 30       # 30 days
MAP_WINDOW_HOURS_DEFAULT = 24 * 7    # 7 days
MAP_LIMIT_MAX = 500                  # display + cognitive cap; the map isn't a data dump

@api_router.get("/seismic-map/events")
async def seismic_map_events(
    window_hours: int = MAP_WINDOW_HOURS_DEFAULT,
    limit: int = MAP_LIMIT_MAX,
):
    """Recent Mediterranean seismic activity for the in-app map.

    Query:
      - window_hours: how far back to look. Clamped to [1, 720].
      - limit: max rows returned. Clamped to [1, 500].

    Returns events sorted newest-first, deduplicated across providers
    (EMSC + USGS often report the same event with slightly different
    magnitudes — for a map, one dot per real event is what the user
    expects). Dedup key is (rounded lat, rounded lon, rounded minute).

    Response shape is a flat list — no nested provider grouping — so
    the mobile client can render markers straight from it.
    """
    # Clamp inputs. Silent clamp (not 400) — map is a passive read;
    # a bad query shouldn't blank the UI.
    # Explicit `is None` check instead of `x or DEFAULT` because 0 is a
    # valid (if silly) integer input we want to clamp to the minimum,
    # not silently swap for the default. Same reasoning for limit.
    if window_hours is None:
        window_hours = MAP_WINDOW_HOURS_DEFAULT
    window_hours = max(1, min(int(window_hours), MAP_WINDOW_HOURS_MAX))
    if limit is None:
        limit = MAP_LIMIT_MAX
    limit = max(1, min(int(limit), MAP_LIMIT_MAX))

    cutoff = datetime.now(timezone.utc) - timedelta(hours=window_hours)

    query = {
        "observed_at": {"$gte": cutoff},
        "latitude":  {"$gte": MED_BBOX_LAT_MIN, "$lte": MED_BBOX_LAT_MAX},
        "longitude": {"$gte": MED_BBOX_LON_MIN, "$lte": MED_BBOX_LON_MAX},
    }
    # Fetch a bit more than `limit` so post-dedup we can still meet it.
    cursor = db.emsc_events.find(
        query,
        {
            "_id": 0,
            "provider": 1,
            "external_id": 1,
            "revision": 1,
            "observed_at": 1,
            "magnitude": 1,
            "magnitude_type": 1,
            "latitude": 1,
            "longitude": 1,
            "depth_km": 1,
            "region": 1,
        },
    ).sort("observed_at", -1).limit(limit * 2)
    rows = await cursor.to_list(limit * 2)

    # Cross-provider dedup — same event reported by EMSC & USGS shows as
    # two rows with matching origin time (to the minute) and location
    # (to ~0.05°, i.e. ~5 km). Keep whichever has the larger magnitude
    # (USGS sometimes revises upward) but expose both provider IDs so
    # the client can link to either bulletin.
    # Two-pass dedup:
    #   Pass 1 (same provider, same external_id = a REVISION):
    #     keep only the highest revision number. Guarantees no doubles
    #     from provider-side re-analysis (which shifts lat/lon slightly
    #     and can straddle bucket boundaries).
    #   Pass 2 (different providers, same real event):
    #     bucket by (rounded lat, rounded lon, rounded minute) and merge.
    latest_by_key: dict = {}
    for r in rows:
        k = (r.get("provider"), r.get("external_id"))
        prev = latest_by_key.get(k)
        if prev is None or (r.get("revision") or 0) > (prev.get("revision") or 0):
            latest_by_key[k] = r
    deduped_rows = list(latest_by_key.values())

    def _dedup_key(r: dict) -> tuple:
        obs = r.get("observed_at")
        # Minute-precision bucket:
        minute_bucket = obs.replace(second=0, microsecond=0) if isinstance(obs, datetime) else obs
        return (
            round(float(r.get("latitude", 0.0)),  1),   # ~11 km bucket
            round(float(r.get("longitude", 0.0)), 1),
            minute_bucket,
        )

    merged: dict = {}
    for r in deduped_rows:
        k = _dedup_key(r)
        prev = merged.get(k)
        if prev is None:
            merged[k] = {**r, "providers": [r["provider"]]}
            continue
        # Same real event — merge.
        if r["provider"] not in prev["providers"]:
            prev["providers"].append(r["provider"])
        if float(r.get("magnitude") or 0) > float(prev.get("magnitude") or 0):
            # Prefer the higher-magnitude report (typically the revised one).
            for f in ("magnitude", "magnitude_type", "depth_km", "region", "external_id", "provider"):
                if r.get(f) is not None:
                    prev[f] = r.get(f)

    events = list(merged.values())
    # Re-sort by observed_at desc (dedup may have shuffled order).
    events.sort(key=lambda r: r.get("observed_at") or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    events = events[:limit]

    # Serialize datetimes.
    for e in events:
        obs = e.get("observed_at")
        if isinstance(obs, datetime):
            e["observed_at"] = obs.isoformat()

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "window_hours": window_hours,
        "bbox": {
            "lat_min": MED_BBOX_LAT_MIN, "lat_max": MED_BBOX_LAT_MAX,
            "lon_min": MED_BBOX_LON_MIN, "lon_max": MED_BBOX_LON_MAX,
        },
        "count": len(events),
        "events": events,
        # Attribution required in the mobile UI — but also sent here so
        # any future third-party consumer (dashboard, alt clients) can't
        # accidentally omit it.
        "attribution": "Data: EMSC (emsc-csem.org) & USGS (earthquake.usgs.gov)",
    }




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
    request: Request = None,
    x_admin_token: Optional[str] = Header(default=None, alias="X-Admin-Token"),
):
    """Dashboard operator marks a trapped case as physically found & safe.

    Distinct from a `safe` self-report — this is a responder attestation.
    The prior triage state (status/severity/mobility) is snapshotted onto
    the device doc as `pre_rescue_*` so an Undo can restore the exact
    situation the operator saw before clicking.

    Auth (either):
      - Preferred: `Authorization: Bearer <jwt>` (from /api/auth/google flow).
        Role required: admin OR operator. The user's email is captured
        into `rescued_by` for the audit trail.
      - Legacy (deprecated): `X-Admin-Token` matching ADMIN_TRIGGER_PASSWORD.
        Attributed as `legacy@dashboard` in the audit trail. Disable by
        setting `LEGACY_TOKEN_ENABLED=false` once dashboards are cut over.

    Body: `{"deviceId": "qg-...", "notes": "optional freeform text"}`
    """
    principal = await resolve_principal(request, x_admin_token, ADMIN_TRIGGER_PASSWORD, db)
    require_role(principal, "admin", "operator")

    device_id = str(payload.get("deviceId") or payload.get("device_id") or "").strip()
    if not device_id:
        raise HTTPException(400, "deviceId is required")
    notes = payload.get("notes")
    # Server-side credential gate — an admin password leaked into this
    # field once before (urgent rotation + audit redaction). Reject
    # anything credential-shaped BEFORE it reaches the audit trail.
    if notes:
        _cred_reason = _looks_like_credential(str(notes))
        if _cred_reason:
            raise HTTPException(
                422,
                f"Note not saved — it {_cred_reason}. Notes are stored in the "
                "audit log and appear in exports, so passwords and keys must "
                "never go here. Remove the secret and describe the rescue in "
                "plain words. If a real credential was typed here, rotate it now.",
            )
    # The body-supplied `rescued_by` is IGNORED — we trust the authenticated
    # principal, not client input. This is a deliberate change from the
    # pre-#9 behavior where a caller could put anything in `rescued_by`.
    rescued_by = audit_attribution(principal)

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
    request: Request = None,
    x_admin_token: Optional[str] = Header(default=None, alias="X-Admin-Token"),
):
    """Undo a mark-rescued — restores the exact prior triage state from
    `pre_rescue_*` snapshot. Use this to recover from a mis-click.

    Auth: JWT (Authorization: Bearer) OR legacy X-Admin-Token. Role
    required: admin OR operator. See mark-rescued for full auth notes.
    """
    principal = await resolve_principal(request, x_admin_token, ADMIN_TRIGGER_PASSWORD, db)
    require_role(principal, "admin", "operator")

    device_id = str(payload.get("deviceId") or payload.get("device_id") or "").strip()
    if not device_id:
        raise HTTPException(400, "deviceId is required")
    # Body-supplied reverted_by is ignored — attribution comes from the
    # authenticated principal only.
    reverted_by = audit_attribution(principal)

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


@api_router.post("/admin/redact-notes")
async def redact_notes(
    payload: dict = Body(...),
    request: Request = None,
    x_admin_token: Optional[str] = Header(default=None, alias="X-Admin-Token"),
):
    """Blank the `notes` field on rescued / rescue_reverted audit rows for
    one or more devices. Purpose: allow an operator to purge sensitive text
    that was accidentally typed into the notes field (which is currently
    rendered in the public dashboard Recent-Activity feed).

    ADMIN-ONLY (operators can't redact — this is deliberately a higher-bar
    action per the task-#9 role split). Idempotent — re-running on an
    already-redacted row is a no-op. Also blanks any `notes` field on the
    corresponding device_status row as defence-in-depth.

    Auth: JWT (Bearer) OR legacy X-Admin-Token. Requires admin role.

    Payload:
        {
          "device_ids": ["qg-...", "qg-..."],   // required, 1..50
          "kinds":      ["rescued", "rescue_reverted"],  // optional, default both
          "reason":     "incident-2026-08-04"    // optional, appears in redaction marker
        }

    Response:
        {
          "redacted_status_events": N,
          "redacted_device_status": M,
          "matched_devices": [...],
          "unknown_devices": [...]
        }

    Never echoes the redacted content back — that would defeat the point.
    """
    principal = await resolve_principal(request, x_admin_token, ADMIN_TRIGGER_PASSWORD, db)
    require_role(principal, "admin")
    redacted_by = audit_attribution(principal)

    raw_ids = payload.get("device_ids") or payload.get("deviceIds")
    if not isinstance(raw_ids, list) or not raw_ids:
        raise HTTPException(400, "device_ids must be a non-empty list")
    if len(raw_ids) > 50:
        raise HTTPException(400, "device_ids capped at 50 per request")
    device_ids = [str(x).strip() for x in raw_ids if str(x).strip()]
    if not device_ids:
        raise HTTPException(400, "device_ids must contain at least one non-empty id")

    # `kinds` is kept in the API surface for forward-compat, but the current
    # storage layer doesn't persist a `kind` field on status_events — kind
    # is derived on read in /api/audit from `status`, `rescue_reverted`, etc.
    # So instead of filtering by a non-existent DB field, we redact any row
    # that has a non-empty `notes` field for the listed device_ids. Notes
    # are only ever written by mark-rescued today, so in practice this only
    # touches rescued / rescue_reverted rows regardless — which is the
    # intent. We still validate `kinds` for input hygiene.
    kinds = payload.get("kinds")
    if kinds is None:
        kinds = ["rescued", "rescue_reverted"]
    if not isinstance(kinds, list) or not all(
        k in {"rescued", "rescue_reverted"} for k in kinds
    ):
        raise HTTPException(400, "kinds must be a list of 'rescued' | 'rescue_reverted'")

    reason = str(payload.get("reason") or "").strip()
    reason_suffix = f"; reason={reason}" if reason else ""
    marker = (
        f"[REDACTED — notes purged by {redacted_by}{reason_suffix}; "
        f"see /api/admin/redact-notes]"
    )

    # Which of the requested device_ids actually exist? Report unknowns so
    # the caller notices typos rather than silently no-op-ing.
    existing = await db.status_events.distinct(
        "device_id", {"device_id": {"$in": device_ids}}
    )
    unknown = [d for d in device_ids if d not in existing]

    # Redact any status_events row that has non-empty notes for one of the
    # listed device_ids. Idempotent — we exclude rows already carrying the
    # exact marker, so re-runs are a true no-op instead of resetting rows
    # to a fresh marker string.
    ev_res = await db.status_events.update_many(
        {
            "device_id": {"$in": device_ids},
            "notes": {"$exists": True, "$nin": [None, "", marker]},
        },
        {"$set": {"notes": marker}},
    )

    # Defence-in-depth: same on device_status.
    ds_res = await db.device_status.update_many(
        {
            "device_id": {"$in": device_ids},
            "notes": {"$exists": True, "$nin": [None, "", marker]},
        },
        {"$set": {"notes": marker}},
    )

    return {
        "redacted_status_events": ev_res.modified_count,
        "redacted_device_status": ds_res.modified_count,
        "matched_devices": sorted(existing),
        "unknown_devices": unknown,
        "kinds_requested": kinds,
        "marker": marker,
    }




@api_router.post("/trigger-alert")
async def trigger_alert(
    body: TriggerAlertBody,
    request: Request = None,
    x_admin_token: Optional[str] = Header(default=None, alias="X-Admin-Token"),
):
    """Broadcast a Quake Angel alert to every registered device (except the
    device that triggered it, if provided). Push delivery failure is logged
    but never blocks the response.

    Auth: JWT (Bearer) OR legacy X-Admin-Token. Role: admin OR operator.
    The authenticated user's email is written into push_events.triggered_by
    for the audit trail — replacing the pre-#9 hardcoded "dashboard" value.
    """
    principal = await resolve_principal(request, x_admin_token, ADMIN_TRIGGER_PASSWORD, db)
    require_role(principal, "admin", "operator")
    triggered_by_user = audit_attribution(principal)

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
            # Forward the actual event details so /alert renders REAL
            # values on the mobile screen, not the hardcoded placeholders
            # 6.4 / 12 / VII. Fields left as None (from body defaults)
            # render as "—" on the mobile side.
            magnitude=body.magnitude,
            distance_km=body.distance_km,
            intensity=body.intensity,
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
            # Post-#9: triggered_by holds the AUTHENTICATED USER's email (or
            # "legacy@dashboard" during migration). The body.triggeredBy
            # field, if provided, now carries the CALLER'S device_id and is
            # only used to exclude that device from the broadcast — it no
            # longer determines who gets credited in the audit trail.
            "triggered_by": triggered_by_user,
            "excluded_device_id": body.triggeredBy,
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


# ---------- False-alarm recovery: cancel every pending check-in reminder ----
@api_router.post("/admin/reminders/cancel")
async def cancel_check_in_reminders(request: Request):
    """STOP THE NAGGING — cancel pending check-in reminders on every phone.

    Why this exists (batch 5, B1): manual triggering is a primary detection
    path (#105) and humans make mistakes. Before this endpoint, a false alarm
    meant every user got the alert plus 8 local reminder sirens over 11½
    minutes, with no way for an operator to stop it — the reminders are
    scheduled ON the device, so nothing server-side could reach them.

    How it stops them without adding noise: a SILENT background push
    (content-available, no alert, no sound — see
    apns.send_silent_cancel_reminders) which the app handles in a registered
    background task and answers by cancelling every scheduled reminder plus
    dismissing any already on screen. The user hears nothing; the nagging
    just stops.

    Scope: every registered iOS device. A phone with no pending reminders
    treats it as a no-op, so broadcasting is safe and needs no bookkeeping
    about who received which alert.

    Android note: Android reminders are local notifications too, but the
    Android relay has no silent data-push channel, so this endpoint covers
    iOS only. Android users' reminders still stop the moment they answer.
    """
    principal = await resolve_principal(
        request, request.headers.get("x-admin-token"),
        ADMIN_TRIGGER_PASSWORD, db,
    )
    require_role(principal, "admin", "operator")

    ios_devices = await db.push_devices.find(
        {
            "platform": {"$in": ["ios", "iOS"]},
            "device_token": {"$exists": True, "$ne": None},
        },
        {"_id": 0, "user_id": 1, "device_token": 1},
    ).to_list(5000)

    idem = f"cancel-reminders-{uuid.uuid4()}"
    result = await send_silent_cancel_reminders(
        db=db,
        devices=ios_devices,
        idempotency_key=idem,
        reason="operator_cancel",
    )
    events = result.get("events") or []
    delivered = sum(1 for e in events if e.get("delivered"))
    now = datetime.now(timezone.utc).isoformat()

    await db.emsc_audit_log.insert_one({
        "timestamp": now,
        "event_type": "reminders_cancelled",
        "context": {
            "requested_by": principal.get("email"),
            "targeted": len(ios_devices),
            "delivered": delivered,
            "idempotency_key": idem,
            "silent": True,
        },
    })

    return {
        "ok": True,
        "targeted": len(ios_devices),
        "delivered": delivered,
        "silent": True,
        "requested_by": principal.get("email"),
        "requested_at": now,
        "idempotency_key": idem,
        "apns_events": events,
    }


# ---------- Maintenance: purge leftover test / diagnostic rows ----------
class MarkTestBody(BaseModel):
    is_test: bool = True


@api_router.post("/admin/devices/{device_id}/mark-test")
async def mark_device_as_test(device_id: str, body: MarkTestBody, request: Request):
    """Tag (or untag) one device as a test entry — #146.

    Why an operator needs this: our own test check-ins come from a real
    phone with a real device_id, so no naming pattern can spot them. Once
    tagged, the row still exists (nothing is deleted, the audit trail is
    intact) but the dashboard's trapped list hides it by default.

    Reversible: post {"is_test": false} to put it back. Both directions
    are written to the audit log, because hiding a person from a rescue
    list is a decision someone must be able to account for.
    """
    principal = await resolve_principal(
        request, request.headers.get("x-admin-token"), ADMIN_TRIGGER_PASSWORD, db
    )
    require_role(principal, "admin", "operator")

    res = await db.device_status.update_one(
        {"device_id": device_id},
        {"$set": {"synthetic": bool(body.is_test)}},
    )
    if res.matched_count == 0:
        raise HTTPException(404, "No such device")

    now = datetime.now(timezone.utc).isoformat()
    await db.emsc_audit_log.insert_one({
        "timestamp": now,
        "event_type": "device_marked_test" if body.is_test else "device_unmarked_test",
        "device_id": device_id,
        "context": {"by": principal.get("email"), "is_test": bool(body.is_test)},
    })
    return {"ok": True, "device_id": device_id, "is_test": bool(body.is_test)}


@api_router.get("/admin/test-entries")
async def list_test_entries(request: Request):
    """Preview what a purge would remove, across ALL three collections."""
    principal = await resolve_principal(
        request, request.headers.get("x-admin-token"), ADMIN_TRIGGER_PASSWORD, db
    )
    require_role(principal, "admin", "operator")

    rows = await db.device_status.find({}, {"_id": 0}).to_list(5000)
    test_rows = [r for r in rows if _is_test_device(r)]
    ids = [r.get("device_id") for r in test_rows]
    return {
        "count": len(test_rows),
        "device_status": len(test_rows),
        "status_events": await db.status_events.count_documents({"device_id": {"$in": ids}}) if ids else 0,
        "push_devices": await db.push_devices.count_documents({"user_id": {"$in": ids}}) if ids else 0,
        "devices": [
            {
                "device_id": r.get("device_id"),
                "status": r.get("status"),
                "severity": r.get("severity"),
                "display_name": r.get("display_name"),
                "updated_at": r.get("updated_at"),
                "flagged_by_operator": r.get("synthetic") is True,
            }
            for r in test_rows
        ],
    }


@api_router.post("/admin/purge-test-entries")
async def purge_test_entries(request: Request):
    """Delete every tagged/recognised test entry from the live surfaces.

    Unlike the older /admin/purge-test-devices (which only ever touched
    `push_devices`, and so left the trapped list exactly as cluttered as
    before — the actual cause of #146), this clears `device_status`,
    `status_events` and `push_devices` together.

    ADMIN ONLY, and audited with the full id list: this deletes rows from
    what is, for real people, a legal record. Operators can hide entries
    (mark-test); only an admin can destroy them.
    """
    principal = await resolve_principal(
        request, request.headers.get("x-admin-token"), ADMIN_TRIGGER_PASSWORD, db
    )
    require_role(principal, "admin")

    rows = await db.device_status.find({}, {"_id": 0, "device_id": 1, "synthetic": 1}).to_list(5000)
    ids = [r.get("device_id") for r in rows if _is_test_device(r)]
    if not ids:
        return {"ok": True, "deleted": {"device_status": 0, "status_events": 0, "push_devices": 0}}

    d1 = await db.device_status.delete_many({"device_id": {"$in": ids}})
    d2 = await db.status_events.delete_many({"device_id": {"$in": ids}})
    d3 = await db.push_devices.delete_many({"user_id": {"$in": ids}})

    now = datetime.now(timezone.utc).isoformat()
    await db.emsc_audit_log.insert_one({
        "timestamp": now,
        "event_type": "test_entries_purged",
        "context": {
            "by": principal.get("email"),
            "device_ids": ids,
            "deleted": {
                "device_status": d1.deleted_count,
                "status_events": d2.deleted_count,
                "push_devices": d3.deleted_count,
            },
        },
    })
    return {
        "ok": True,
        "purged_device_ids": ids,
        "deleted": {
            "device_status": d1.deleted_count,
            "status_events": d2.deleted_count,
            "push_devices": d3.deleted_count,
        },
    }


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




# ═════════════════════════════════════════════════════════════════════════
# Per-user Google sign-in (task #9)
# ═════════════════════════════════════════════════════════════════════════

class GoogleLoginPayload(BaseModel):
    """Request body for POST /api/auth/google.

    id_token: the JWT Google Identity Services returns to the browser after
    a successful sign-in. We verify it server-side (signature, audience,
    issuer, expiry, email_verified) via the google-auth library, then check
    the resulting email against our own users collection.
    """
    id_token: str = Field(..., min_length=20, max_length=4096)


class UserCreatePayload(BaseModel):
    email: str = Field(..., min_length=3, max_length=200)
    role: str = Field(..., pattern=r"^(admin|operator)$")
    display_name: Optional[str] = Field(default=None, max_length=80)


def _user_public(u: dict) -> dict:
    """Shape a users doc for API responses. Never returns fields that
    would be sensitive if the response were misrouted (session_version,
    google_sub, timestamps of the LAST login, are all fine — no secrets)."""
    exp = u.get("expires_at")
    now = datetime.now(timezone.utc)
    # A user is "expired" iff expires_at is set AND in the past. A null
    # expires_at means "never expires" (used for the primary admin).
    is_expired = False
    if exp is not None:
        exp_aware = exp if getattr(exp, "tzinfo", None) else exp.replace(tzinfo=timezone.utc) if exp else None
        is_expired = exp_aware is not None and exp_aware < now
    return {
        "email": u.get("email_normalized"),
        "display_name": u.get("display_name") or u.get("email_normalized"),
        "role": u.get("role"),
        "allowed": bool(u.get("allowed")),
        "disabled": bool(u.get("disabled")),
        "google_linked": bool(u.get("google_sub")),
        "last_login_at": u.get("last_login_at"),
        "created_at": u.get("created_at"),
        "created_by": u.get("created_by"),
        "disabled_at": u.get("disabled_at"),
        "disabled_by": u.get("disabled_by"),
        "expires_at": u.get("expires_at"),
        "is_expired": is_expired,
    }


# Default account lifetime — 90 days. Locked with Paul 2026-08-06.
# Rationale: an operator who hasn't been onboarded to the current
# dispatch protocol for 90 days shouldn't retain access without an
# admin re-confirmation. Set to None on the doc to mean "never expires"
# (used for the primary admin so a bad expiry policy can never lock out
# the last admin).
DEFAULT_ACCOUNT_LIFETIME_DAYS = 90


@api_router.post("/auth/google")
async def auth_google(payload: GoogleLoginPayload):
    """Exchange a Google-signed ID token for our own dashboard JWT.

    Flow:
      1. Verify the Google ID token (signature, aud, iss, exp, email_verified).
      2. Look up the email in our users allowlist.
      3. Reject if not allowlisted, or disabled, or explicitly not allowed.
      4. First-successful-sign-in for an allowlisted email links the account
         to Google's stable `sub` identifier so future logins are matched
         on `sub` (email can change; sub cannot).
      5. Issue our own 15-min JWT and return it.

    Never accepts an email as authoritative from the client — only from
    Google's signed token.
    """
    info = verify_google_id_token(payload.id_token)
    google_sub = info["sub"]
    email = str(info.get("email") or "").strip()
    if not email:
        raise AuthError("Google token missing email", status_code=400)
    email_norm = email.lower()

    # Match by google_sub first (stable), then fall back to email for the
    # first sign-in of a pre-created allowlist entry.
    user = await db.users.find_one({"google_sub": google_sub})
    if not user:
        user = await db.users.find_one({"email_normalized": email_norm})

    if not user or not user.get("allowed") or user.get("disabled"):
        # Log the denial so an admin can see failed sign-in attempts, but
        # don't reveal WHY (existence-of-account vs disabled) — same
        # 403 for both to avoid enumeration.
        logger.info("Sign-in denied for %s (google_sub=%s...)", email_norm, google_sub[:8])
        raise AuthError("This Google account is not authorized for the dashboard", status_code=403)

    # Link/refresh: bind sub if this is the first login, record last_login_at.
    now = datetime.now(timezone.utc)
    updates: dict = {
        "last_login_at": now,
        "last_login_email": email,           # so we see the display-form email in logs
    }
    if not user.get("google_sub"):
        updates["google_sub"] = google_sub
        updates["google_linked_at"] = now
    if user.get("display_name") is None and info.get("name"):
        updates["display_name"] = info["name"][:80]
    await db.users.update_one({"_id": user["_id"]}, {"$set": updates})

    # Re-fetch so the issued JWT reflects the freshest fields (in
    # particular the newly-set google_sub on first login).
    user = await db.users.find_one({"_id": user["_id"]})
    token, exp = issue_app_jwt(user)
    return {
        "token": token,
        "expires_at": exp.isoformat(),
        "user": _user_public(user),
    }


@api_router.get("/auth/me")
async def auth_me(request: Request):
    """Return the current caller's identity + role.

    Used by the dashboard on load to (a) decide whether to show a sign-in
    button or the operator UI, and (b) refresh cached role info so
    role-gated buttons update without a full re-login.
    """
    principal = await resolve_principal(
        request,
        request.headers.get("x-admin-token"),
        ADMIN_TRIGGER_PASSWORD,
        db,
    )
    # For real users, resolve to the users doc so we can return created_at etc.
    if not principal.get("is_legacy"):
        u = await db.users.find_one({"email_normalized": principal["email"]})
        if u:
            return {"authenticated": True, "user": _user_public(u), "is_legacy": False}
    return {
        "authenticated": True,
        "user": {
            "email": principal["email"],
            "display_name": principal["display_name"],
            "role": principal["role"],
        },
        "is_legacy": principal.get("is_legacy", False),
    }


@api_router.post("/auth/logout")
async def auth_logout(request: Request):
    """Client-side logout — nothing to invalidate server-side unless we
    bump session_version. Returns 200 unconditionally so the client can
    always clear its stored token.

    Rationale: a Bearer JWT logout is inherently client-side (the client
    forgets the token). For hard-revocation, an admin should disable the
    user OR the user should hit /api/auth/revoke-me (below) which bumps
    their session_version and invalidates every issued JWT for them.
    """
    return {"ok": True}


@api_router.post("/auth/revoke-me")
async def auth_revoke_me(request: Request):
    """Bump the caller's session_version, invalidating every issued JWT
    for them. Use when you suspect your device was compromised.
    """
    principal = await resolve_principal(
        request,
        request.headers.get("x-admin-token"),
        ADMIN_TRIGGER_PASSWORD,
        db,
    )
    if principal.get("is_legacy"):
        raise AuthError("Legacy principal cannot self-revoke", status_code=400)
    await db.users.update_one(
        {"email_normalized": principal["email"]},
        {"$inc": {"session_version": 1}},
    )
    return {"ok": True}


# ── User management (admin-only) ────────────────────────────────────────
@api_router.get("/admin/users")
async def list_users(request: Request):
    principal = await resolve_principal(
        request,
        request.headers.get("x-admin-token"),
        ADMIN_TRIGGER_PASSWORD,
        db,
    )
    require_role(principal, "admin")
    users = await db.users.find({}, {"_id": 0}).sort("created_at", 1).to_list(200)
    return {"count": len(users), "users": [_user_public(u) for u in users]}


@api_router.post("/admin/users")
async def create_user(payload: UserCreatePayload, request: Request):
    """Add a new user to the allowlist. They can sign in from that moment
    on with their Google account — no invite email, no temp password."""
    principal = await resolve_principal(
        request,
        request.headers.get("x-admin-token"),
        ADMIN_TRIGGER_PASSWORD,
        db,
    )
    require_role(principal, "admin")

    email_norm = payload.email.strip().lower()
    if "@" not in email_norm or " " in email_norm:
        raise HTTPException(400, "Invalid email")
    # Prevent creation of a user whose email collides with the sentinel
    # principal used by the legacy X-Admin-Token path. A real user with
    # that email would let self-* guards misattribute legacy-token
    # actions as "self", producing subtly wrong lockout behavior.
    if email_norm == "legacy@dashboard":
        raise HTTPException(400, "Reserved email address")

    existing = await db.users.find_one({"email_normalized": email_norm})
    if existing:
        raise HTTPException(409, f"User {email_norm} already exists")

    doc = {
        "email": payload.email.strip(),
        "email_normalized": email_norm,
        "display_name": (payload.display_name or email_norm.split("@", 1)[0]).strip(),
        "role": payload.role,
        "allowed": True,
        "disabled": False,
        "session_version": 1,
        "google_sub": None,
        "created_at": datetime.now(timezone.utc),
        "created_by": principal["email"],
        # Default account expiry: 90 days from creation. Admins can override
        # via PATCH or POST /extend after creation. The primary/bootstrap
        # admin doc has this set to null so their access never expires.
        "expires_at": datetime.now(timezone.utc) + timedelta(days=DEFAULT_ACCOUNT_LIFETIME_DAYS),
    }
    await db.users.insert_one(doc)
    return {"ok": True, "user": _user_public(doc)}


@api_router.post("/admin/users/{email}/disable")
async def disable_user(email: str, request: Request):
    """Disable a user AND bump session_version so their in-flight JWTs
    are immediately invalidated. Cannot disable yourself (safety rail
    against accidental self-lockout by the last admin)."""
    principal = await resolve_principal(
        request,
        request.headers.get("x-admin-token"),
        ADMIN_TRIGGER_PASSWORD,
        db,
    )
    require_role(principal, "admin")
    email_norm = email.strip().lower()
    if email_norm == principal["email"]:
        raise HTTPException(400, "Cannot disable your own account")
    res = await db.users.update_one(
        {"email_normalized": email_norm},
        {
            "$set": {
                "disabled": True,
                "disabled_at": datetime.now(timezone.utc),
                "disabled_by": principal["email"],
            },
            "$inc": {"session_version": 1},
        },
    )
    if res.matched_count == 0:
        raise HTTPException(404, f"User {email_norm} not found")
    return {"ok": True}


@api_router.post("/admin/users/{email}/enable")
async def enable_user(email: str, request: Request):
    principal = await resolve_principal(
        request,
        request.headers.get("x-admin-token"),
        ADMIN_TRIGGER_PASSWORD,
        db,
    )
    require_role(principal, "admin")
    email_norm = email.strip().lower()
    res = await db.users.update_one(
        {"email_normalized": email_norm},
        {
            "$set": {"disabled": False},
            "$unset": {"disabled_at": "", "disabled_by": ""},
        },
    )
    if res.matched_count == 0:
        raise HTTPException(404, f"User {email_norm} not found")
    return {"ok": True}


# ---------- User update / expiry / delete (Task 4: User Management dashboard) ----

class UserPatchBody(BaseModel):
    """Partial-update payload for PATCH /admin/users/{email}.

    All fields optional; only supplied ones are applied. Empty patch is a
    no-op (returns 400 to prevent accidental empty PATCHes that look like
    a UI bug at the caller).
    """
    role: Optional[str] = Field(None, description="'admin' or 'operator'")
    # expires_at accepts ISO8601 string OR the sentinel "never" (which
    # sets the field to null on the doc). Mobile-app JSON serialization
    # is quirky about nullable datetimes, so this two-mode API is safer
    # than trying to distinguish 'null' from 'field absent'.
    expires_at: Optional[str] = Field(
        None,
        description="ISO 8601 datetime, or the string 'never' to remove expiry.",
    )


@api_router.patch("/admin/users/{email}")
async def patch_user(email: str, body: UserPatchBody, request: Request):
    """Update role and/or expiry on an existing user.

    Safety rails (all enforced server-side, not just in the dashboard):
      - Admin cannot demote their own account (would lock themselves out
        of user management).
      - Admin cannot set their own expiry into the past.
      - Session version is bumped on role change so any active JWTs for
        the updated user are invalidated within one request cycle (they
        get a 401 on next call and re-authenticate — new JWT reflects
        new role).

    Deliberately does NOT allow email or display_name changes here —
    those would require a Google-sub re-link. Delete + re-add if needed.
    """
    principal = await resolve_principal(
        request,
        request.headers.get("x-admin-token"),
        ADMIN_TRIGGER_PASSWORD,
        db,
    )
    require_role(principal, "admin")

    email_norm = email.strip().lower()
    target = await db.users.find_one({"email_normalized": email_norm})
    if not target:
        raise HTTPException(404, f"User {email_norm} not found")

    is_self = email_norm == (principal.get("email") or "").lower()

    set_fields: dict = {}
    bump_session = False

    if body.role is not None:
        if body.role not in {"admin", "operator"}:
            raise HTTPException(400, "role must be 'admin' or 'operator'")
        if is_self and body.role != "admin":
            # Self-demotion lockout guard.
            raise HTTPException(400, "Cannot demote your own admin account")
        if body.role != target.get("role"):
            set_fields["role"] = body.role
            bump_session = True

    if body.expires_at is not None:
        raw = body.expires_at.strip()
        if raw.lower() == "never":
            set_fields["expires_at"] = None
        else:
            # Parse ISO 8601. Accept both 'Z' and '+00:00' suffixes.
            try:
                new_exp = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                if new_exp.tzinfo is None:
                    new_exp = new_exp.replace(tzinfo=timezone.utc)
            except ValueError:
                raise HTTPException(400, f"expires_at is not a valid ISO 8601 datetime: {raw!r}")
            if is_self and new_exp <= datetime.now(timezone.utc):
                # Self-expire lockout guard. Using <= (not <) so setting
                # expiry to exactly-now still counts as "self-brick" —
                # otherwise the guard passes and the account expires
                # one tick later, defeating the purpose.
                raise HTTPException(400, "Cannot set your own expiry to a past date")
            set_fields["expires_at"] = new_exp

    if not set_fields:
        raise HTTPException(400, "No fields to update")

    update: dict = {"$set": set_fields}
    if bump_session:
        update["$inc"] = {"session_version": 1}

    await db.users.update_one({"email_normalized": email_norm}, update)
    doc = await db.users.find_one({"email_normalized": email_norm})
    return {"ok": True, "user": _user_public(doc)}


@api_router.post("/admin/users/{email}/extend")
async def extend_user_expiry(email: str, request: Request):
    """One-click 'extend by 90 days from now' action.

    Convenience wrapper around PATCH — the dashboard's most common
    account-renewal flow ("Karen's account expires next week, extend
    it another 90 days") shouldn't require the admin to compute an
    ISO date string.

    Sets expires_at = now + 90 days regardless of the previous value.
    Deliberately not "now + 90 from previous expiry" because that would
    let admins accumulate arbitrarily long lifetimes by clicking
    Extend a few times.
    """
    principal = await resolve_principal(
        request,
        request.headers.get("x-admin-token"),
        ADMIN_TRIGGER_PASSWORD,
        db,
    )
    require_role(principal, "admin")

    email_norm = email.strip().lower()
    new_exp = datetime.now(timezone.utc) + timedelta(days=DEFAULT_ACCOUNT_LIFETIME_DAYS)
    res = await db.users.update_one(
        {"email_normalized": email_norm},
        {"$set": {"expires_at": new_exp, "extended_at": datetime.now(timezone.utc), "extended_by": principal["email"]}},
    )
    if res.matched_count == 0:
        raise HTTPException(404, f"User {email_norm} not found")
    doc = await db.users.find_one({"email_normalized": email_norm})
    return {"ok": True, "user": _user_public(doc)}


@api_router.delete("/admin/users/{email}")
async def delete_user(email: str, request: Request):
    """Permanently remove a user.

    In most cases, DISABLE is safer than DELETE — a disabled account
    keeps its audit trail (which rows they triggered, which they
    rescued) intact. Delete only when a user was created by mistake
    or must be scrubbed for GDPR/data-subject-request reasons.

    Safety rails:
      - Cannot delete yourself.
      - Cannot delete the LAST admin account (would leave nobody
        able to manage users going forward — recovery would require
        direct Mongo access).
    """
    principal = await resolve_principal(
        request,
        request.headers.get("x-admin-token"),
        ADMIN_TRIGGER_PASSWORD,
        db,
    )
    require_role(principal, "admin")

    email_norm = email.strip().lower()
    if email_norm == (principal.get("email") or "").lower():
        raise HTTPException(400, "Cannot delete your own account")

    target = await db.users.find_one({"email_normalized": email_norm})
    if not target:
        raise HTTPException(404, f"User {email_norm} not found")

    # Last-admin guard. Exclude disabled AND expired admins from the
    # count — an expired admin can't sign in either, so they can't
    # replace the one being deleted.
    if target.get("role") == "admin":
        now = datetime.now(timezone.utc)
        remaining_admins = await db.users.count_documents({
            "role": "admin",
            "email_normalized": {"$ne": email_norm},
            "disabled": {"$ne": True},
            # `expires_at` null OR in the future
            "$or": [
                {"expires_at": None},
                {"expires_at": {"$gt": now}},
            ],
        })
        if remaining_admins == 0:
            raise HTTPException(400, "Cannot delete the last remaining admin")

    await db.users.delete_one({"email_normalized": email_norm})
    return {"ok": True, "deleted": email_norm}



# ── EMSC/USGS shadow-mode monitoring (Phase 1) ──────────────────────────
# Three admin-only JSON endpoints for inspecting the poll data during the
# 1-2 week soak. No HTML dashboard — the JSON is consumed by scripts and
# by summarised reports we write for Paul.
#
# Auth: admin OR operator (both can inspect); admin is not required
# because a shift operator may want to eyeball recent seismic activity
# without needing admin escalation. If we later want to restrict to
# admin only, tighten `require_role` on each endpoint.

@api_router.get("/admin/emsc/health")
async def emsc_health(request: Request):
    """Per-provider poller health. Answers: 'is the poller alive right now?'

    Returns one document per provider with last_poll_attempt_at,
    last_success_at, consecutive_failures, last_error, and counters.
    A silently-dead poller during soak means two wasted weeks — this
    endpoint is how we (and any external uptime monitor) confirm it's
    still doing its job.
    """
    principal = await resolve_principal(
        request,
        request.headers.get("x-admin-token"),
        ADMIN_TRIGGER_PASSWORD,
        db,
    )
    require_role(principal, "admin", "operator")

    rows = await db.emsc_poller_health.find({}, {"_id": 1, "last_poll_attempt_at": 1,
        "last_success_at": 1, "last_error": 1, "consecutive_failures": 1,
        "total_polls": 1, "total_failures": 1, "total_events_fetched": 1,
        "total_new_rows": 1, "last_fetched_count": 1, "last_new_rows_count": 1,
        "last_poll_duration_ms": 1, "poller_started_at": 1, "poll_interval_sec": 1,
    }).to_list(20)
    now = datetime.now(timezone.utc)

    def _healthy(row: dict) -> bool:
        # Healthy = successful poll within the last 3 intervals AND no
        # currently-outstanding consecutive failures.
        last = row.get("last_success_at")
        if not last:
            return False
        if isinstance(last, datetime) and last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        interval = row.get("poll_interval_sec") or 60
        return (now - last).total_seconds() < (interval * 3) and \
               (row.get("consecutive_failures") or 0) == 0

    return {
        "checked_at": now.isoformat(),
        "poller_task_running": bool(emsc_poller.task and not emsc_poller.task.done()),
        "poller_started_at": emsc_poller.started_at.isoformat() if emsc_poller.started_at else None,
        "providers": [
            {
                "name": r["_id"],
                "healthy": _healthy(r),
                "last_poll_attempt_at": _iso(r.get("last_poll_attempt_at")),
                "last_success_at": _iso(r.get("last_success_at")),
                "last_error": r.get("last_error"),
                "consecutive_failures": r.get("consecutive_failures") or 0,
                "total_polls": r.get("total_polls") or 0,
                "total_failures": r.get("total_failures") or 0,
                "total_events_fetched": r.get("total_events_fetched") or 0,
                "total_new_rows": r.get("total_new_rows") or 0,
                "last_fetched_count": r.get("last_fetched_count"),
                "last_new_rows_count": r.get("last_new_rows_count"),
                "last_poll_duration_ms": r.get("last_poll_duration_ms"),
                "poll_interval_sec": r.get("poll_interval_sec") or 60,
            }
            for r in rows
        ],
    }


@api_router.get("/admin/emsc/recent")
async def emsc_recent(
    request: Request,
    limit: int = Query(50, ge=1, le=500),
    since: Optional[str] = Query(None, description="ISO-8601 UTC. Only return events ingested at-or-after this time."),
    would_have_fired: Optional[bool] = Query(None, description="Filter to rows where at least one evaluation would_have_fired=this."),
    threshold_set: Optional[str] = Query(None, description="Filter to rows whose evaluations include this threshold_set name."),
    provider: Optional[str] = Query(None, description="Filter to a single provider (EMSC or USGS)."),
    country_code: Optional[str] = Query(None, description="Filter to a single country_code."),
):
    """Query recent EMSC/USGS events with soak-relevant filters.

    All filters combine with AND. Ordering: newest ingested first.
    """
    principal = await resolve_principal(
        request,
        request.headers.get("x-admin-token"),
        ADMIN_TRIGGER_PASSWORD,
        db,
    )
    require_role(principal, "admin", "operator")

    query: dict = {}
    if since:
        try:
            since_dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
            if since_dt.tzinfo is None:
                since_dt = since_dt.replace(tzinfo=timezone.utc)
            query["ingested_at"] = {"$gte": since_dt}
        except ValueError:
            raise HTTPException(400, f"Invalid `since` format (expected ISO-8601): {since}")
    if provider:
        query["provider"] = provider

    # threshold_set / country_code / would_have_fired are all filters on
    # elements of the `evaluations` array. If more than one is set, we
    # combine into a single $elemMatch so they all apply to the SAME
    # evaluation entry — otherwise a row could match on evaluation A for
    # one filter and evaluation B for another, which is misleading.
    eval_match: dict = {}
    if threshold_set:
        eval_match["threshold_set"] = threshold_set
    if country_code:
        eval_match["country_code"] = country_code
    if would_have_fired is not None:
        eval_match["would_have_fired"] = would_have_fired
    if eval_match:
        query["evaluations"] = {"$elemMatch": eval_match}

    rows = await db.emsc_events.find(
        query,
        {"_id": 0, "raw": 0},   # strip raw payload to keep responses small
    ).sort("ingested_at", -1).limit(limit).to_list(limit)

    return {
        "count": len(rows),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "filters": {
            "limit": limit, "since": since, "would_have_fired": would_have_fired,
            "threshold_set": threshold_set, "provider": provider, "country_code": country_code,
        },
        "events": [_serialize_emsc_row(r) for r in rows],
    }


@api_router.get("/admin/emsc/config/{country_code}")
async def emsc_get_config(country_code: str, request: Request):
    """Return the country_config for a given ISO-2 code. Useful during
    soak to confirm exactly what thresholds the poller is applying.
    """
    principal = await resolve_principal(
        request,
        request.headers.get("x-admin-token"),
        ADMIN_TRIGGER_PASSWORD,
        db,
    )
    require_role(principal, "admin", "operator")
    doc = await db.country_configs.find_one({"country_code": country_code.upper()}, {"_id": 0})
    if not doc:
        raise HTTPException(404, f"No country_config for {country_code}")
    return _serialize_country_config(doc)


@api_router.get("/admin/emsc/continuity")
async def emsc_continuity(request: Request):
    """Authoritative soak-continuity report.

    Answers: how much of the claimed soak wall-time did the poller
    ACTUALLY run for? Without this, silent gaps (pod suspension,
    credit exhaustion, deploy pauses) hide inside `total_polls` and
    contaminate the threshold-tuning data at the end of soak.

    Returns:
      - soak_started_at: authoritative start (only resets on explicit admin action)
      - wall_seconds: seconds elapsed since soak_started_at
      - dead_seconds: total across all recorded gaps
      - coverage_pct: 100 * (wall_seconds - dead_seconds) / wall_seconds
      - gaps: list of recorded gap rows, newest first
      - reset_history: audit trail of every deliberate soak-clock reset
    """
    principal = await resolve_principal(
        request,
        request.headers.get("x-admin-token"),
        ADMIN_TRIGGER_PASSWORD,
        db,
    )
    require_role(principal, "admin", "operator")

    meta = await db.emsc_soak_meta.find_one({"_id": "singleton"})
    if not meta:
        # Should have been created on startup. If missing (e.g., admin
        # reset while poller down), return an honest empty state.
        return {
            "soak_started_at": None,
            "wall_seconds": 0,
            "dead_seconds": 0,
            "coverage_pct": None,
            "gaps": [],
            "reset_history": [],
            "warning": "emsc_soak_meta not initialized — no active soak clock",
        }

    soak_start = meta.get("soak_started_at")
    if isinstance(soak_start, datetime) and soak_start.tzinfo is None:
        soak_start = soak_start.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    wall_seconds = int((now - soak_start).total_seconds()) if soak_start else 0

    # Gaps recorded since soak_started_at only. Older gaps (pre-reset)
    # are historical, kept in the collection but not counted against
    # the current soak's coverage percentage.
    gap_rows = await db.emsc_poller_gaps.find(
        {"gap_end": {"$gte": soak_start}} if soak_start else {},
    ).sort("gap_end", -1).to_list(200)

    # Dead time is the union of per-provider gaps, but for Phase 1 we
    # simply sum: a gap in one provider IS soak-relevant even if the
    # other kept polling — evaluation depends on BOTH feeds. If we
    # later want the more forgiving "at least one provider was alive"
    # interpretation we can refine here.
    dead_seconds = sum(int(g.get("gap_seconds") or 0) for g in gap_rows)
    coverage_pct = None
    if wall_seconds > 0:
        coverage_pct = round(100.0 * max(0, wall_seconds - dead_seconds) / wall_seconds, 2)

    return {
        "soak_started_at": _iso(soak_start),
        "checked_at": now.isoformat(),
        "wall_seconds": wall_seconds,
        "wall_hours": round(wall_seconds / 3600.0, 2),
        "dead_seconds": dead_seconds,
        "dead_hours": round(dead_seconds / 3600.0, 2),
        "coverage_pct": coverage_pct,
        "gap_count": len(gap_rows),
        "gaps": [
            {
                "provider": g.get("provider"),
                "gap_start": _iso(g.get("gap_start")),
                "gap_end": _iso(g.get("gap_end")),
                "gap_seconds": g.get("gap_seconds"),
                "gap_hours": round((g.get("gap_seconds") or 0) / 3600.0, 2),
                "detection_reason": g.get("detection_reason"),
            }
            for g in gap_rows
        ],
        "reset_history": [
            {
                "at": _iso(r.get("at")),
                "by": r.get("by"),
                "previous_soak_started_at": _iso(r.get("previous_soak_started_at")),
                "reason": r.get("reason"),
            }
            for r in (meta.get("reset_history") or [])
        ],
    }


class ResetSoakBody(BaseModel):
    reason: str = Field(..., min_length=3, max_length=500)
    confirm: bool = Field(..., description="Must be true to proceed — safety rail.")


class PreviewConfigBody(BaseModel):
    """Payload for POST /api/admin/emsc/preview/config.

    Every field is optional — omitted fields keep their existing value.
    This lets an admin toggle `enabled: true` without needing to re-send
    device_ids etc."""
    enabled: Optional[bool] = None
    device_ids: Optional[List[str]] = None
    trigger_tier: Optional[str] = None    # "all_ingested" | threshold_set name
    rate_limit_minutes: Optional[int] = Field(default=None, ge=1, le=1440)
    # ── Preview-only radius override (2026-08-07) ─────────────────────
    # Widens the preview radius for enrolled test devices ONLY. The real-
    # alert path is physically untouchable by this field — see
    # emsc/preview.py::dispatch_preview_if_needed. Bounded 100..5000km:
    #   - lower bound prevents accidentally disabling previews entirely
    #   - upper bound prevents regressing to the "worldwide previews" bug
    # Auto-clears server-side 7 days after being set (see set endpoint).
    # To CLEAR explicitly, send clear_preview_radius_override=true.
    preview_radius_km_override: Optional[float] = Field(default=None, ge=100, le=5000)
    clear_preview_radius_override: Optional[bool] = None


class PreviewAddDeviceBody(BaseModel):
    device_id: str = Field(..., min_length=3, max_length=200)


@api_router.post("/admin/emsc/reset-soak-clock")
async def emsc_reset_soak_clock(body: ResetSoakBody, request: Request):
    """Reset the authoritative soak_started_at to now.

    Use ONLY when a genuine break in soak continuity means the previous
    clock is untrustworthy (e.g., 18-hour pod suspension during credit
    exhaustion). This is a deliberate destructive-ish action — the
    previous soak_started_at is preserved in reset_history so the
    action is auditable, but the effective clock everyone quotes from
    that point on starts from `now`.

    Admin-only. Requires an explicit `confirm: true` payload to prevent
    accidental resets via a partially-crafted request.
    """
    principal = await resolve_principal(
        request,
        request.headers.get("x-admin-token"),
        ADMIN_TRIGGER_PASSWORD,
        db,
    )
    require_role(principal, "admin")
    if not body.confirm:
        raise HTTPException(400, "confirm must be true to reset the soak clock")

    now = datetime.now(timezone.utc)
    prev = await db.emsc_soak_meta.find_one({"_id": "singleton"})
    prev_started = prev.get("soak_started_at") if prev else None

    reset_entry = {
        "at": now,
        "by": principal.get("email", "unknown"),
        "previous_soak_started_at": prev_started,
        "reason": body.reason,
    }
    await db.emsc_soak_meta.update_one(
        {"_id": "singleton"},
        {
            "$set": {"soak_started_at": now},
            "$push": {"reset_history": reset_entry},
            "$setOnInsert": {"created_at": now},
        },
        upsert=True,
    )
    return {
        "ok": True,
        "new_soak_started_at": now.isoformat(),
        "previous_soak_started_at": _iso(prev_started),
        "reset_by": principal.get("email"),
        "reason": body.reason,
    }


# ── EMSC Preview mode (P2.5) ────────────────────────────────────────────
# Sends REAL (non-critical) notifications to an allowlisted device for
# EMSC/USGS events. See emsc/preview.py for the design write-up and the
# non-negotiable constraints. All endpoints require admin role.

VALID_PREVIEW_TIERS = {
    "all_ingested", "quiet_tier", "critical_tier", "neo_original",
    # Part 1a intensity tiers (2026-08-06). Once soak data confirms
    # these calibrate correctly, they become the production alert tiers.
    "intensity_informational", "intensity_standard", "intensity_critical",
}


@api_router.get("/admin/emsc/preview/config")
async def emsc_preview_get_config(request: Request, country_code: str = Query("MT")):
    """Return the current preview_mode sub-document for a country."""
    principal = await resolve_principal(
        request, request.headers.get("x-admin-token"),
        ADMIN_TRIGGER_PASSWORD, db,
    )
    require_role(principal, "admin", "operator")
    doc = await db.country_configs.find_one(
        {"country_code": country_code.upper()},
        {"_id": 0, "country_code": 1, "country_name": 1, "preview_mode": 1},
    )
    if not doc:
        raise HTTPException(404, f"No country_config for {country_code}")
    return doc


@api_router.post("/admin/emsc/preview/config")
async def emsc_preview_set_config(
    body: PreviewConfigBody, request: Request, country_code: str = Query("MT"),
):
    """Update the preview_mode config for a country. Admin-only. Any
    field omitted from the payload keeps its current value (partial update)."""
    principal = await resolve_principal(
        request, request.headers.get("x-admin-token"),
        ADMIN_TRIGGER_PASSWORD, db,
    )
    require_role(principal, "admin")

    updates: dict = {}
    unsets: dict = {}
    if body.enabled is not None:
        updates["preview_mode.enabled"] = body.enabled
    if body.device_ids is not None:
        # Dedupe + strip whitespace defensively.
        cleaned = list({(d or "").strip() for d in body.device_ids if (d or "").strip()})
        updates["preview_mode.device_ids"] = cleaned
    if body.trigger_tier is not None:
        if body.trigger_tier not in VALID_PREVIEW_TIERS:
            raise HTTPException(
                400,
                f"trigger_tier must be one of {sorted(VALID_PREVIEW_TIERS)} "
                f"(got '{body.trigger_tier}')",
            )
        updates["preview_mode.trigger_tier"] = body.trigger_tier
    if body.rate_limit_minutes is not None:
        updates["preview_mode.rate_limit_minutes"] = body.rate_limit_minutes

    # ── Preview radius override with 7-day auto-expiry ────────────────
    # Server-stamps the expiry — client CAN'T extend it by sending a
    # longer expiry. Every set/clear is logged in emsc_audit_log so
    # "why did the override disappear?" is answerable from history alone.
    if body.clear_preview_radius_override:
        unsets["preview_mode.preview_radius_km_override"] = ""
        unsets["preview_mode.preview_radius_km_override_expires_at"] = ""
        unsets["preview_mode.preview_radius_km_override_set_by"] = ""
        unsets["preview_mode.preview_radius_km_override_set_at"] = ""
    elif body.preview_radius_km_override is not None:
        now_utc = datetime.now(timezone.utc)
        expires_at = now_utc + timedelta(days=7)
        updates["preview_mode.preview_radius_km_override"] = float(body.preview_radius_km_override)
        updates["preview_mode.preview_radius_km_override_expires_at"] = expires_at
        updates["preview_mode.preview_radius_km_override_set_by"] = principal.get("email", "unknown")
        updates["preview_mode.preview_radius_km_override_set_at"] = now_utc

    if not updates and not unsets:
        raise HTTPException(400, "No fields to update — provide at least one.")

    updates["preview_mode.updated_at"] = datetime.now(timezone.utc)
    updates["preview_mode.updated_by"] = principal.get("email", "unknown")

    # Snapshot BEFORE so we can compute a from→to audit entry.
    before = await db.country_configs.find_one(
        {"country_code": country_code.upper()},
        {"_id": 0, "preview_mode": 1},
    ) or {}
    before_pm = before.get("preview_mode") or {}

    mongo_op: dict = {"$set": updates} if updates else {}
    if unsets:
        mongo_op["$unset"] = unsets

    res = await db.country_configs.update_one(
        {"country_code": country_code.upper()},
        mongo_op,
    )
    if res.matched_count == 0:
        raise HTTPException(404, f"No country_config for {country_code}")

    # Audit-log override changes specifically — these are the highest-
    # blast-radius edits an operator can make to the preview pipeline
    # ("your notifications will now include a 1500km ring around Malta")
    # and deserve a dedicated log line beyond the generic updated_by stamp.
    if body.clear_preview_radius_override or body.preview_radius_km_override is not None:
        await db.emsc_audit_log.insert_one({
            "at": datetime.now(timezone.utc),
            "kind": "preview_radius_override_change",
            "actor": principal.get("email", "unknown"),
            "country_code": country_code.upper(),
            "from_km": before_pm.get("preview_radius_km_override"),
            "from_expires_at": before_pm.get("preview_radius_km_override_expires_at"),
            "to_km": (None if body.clear_preview_radius_override
                      else float(body.preview_radius_km_override)),
            "to_expires_at": (None if body.clear_preview_radius_override
                              else updates.get("preview_mode.preview_radius_km_override_expires_at")),
            "cleared": bool(body.clear_preview_radius_override),
        })

    doc = await db.country_configs.find_one(
        {"country_code": country_code.upper()},
        {"_id": 0, "preview_mode": 1},
    )
    return {"ok": True, "preview_mode": doc.get("preview_mode")}


@api_router.post("/admin/emsc/preview/add-device")
async def emsc_preview_add_device(
    body: PreviewAddDeviceBody, request: Request, country_code: str = Query("MT"),
):
    """Convenience endpoint — appends a single device_id to the allowlist
    without needing to send the whole list. Idempotent (no-ops if already
    present). Does NOT auto-enable preview mode — that's a separate flip
    to prevent accidental activation."""
    principal = await resolve_principal(
        request, request.headers.get("x-admin-token"),
        ADMIN_TRIGGER_PASSWORD, db,
    )
    require_role(principal, "admin")
    now = datetime.now(timezone.utc)
    res = await db.country_configs.update_one(
        {"country_code": country_code.upper()},
        {
            "$addToSet": {"preview_mode.device_ids": body.device_id.strip()},
            "$set": {
                "preview_mode.updated_at": now,
                "preview_mode.updated_by": principal.get("email", "unknown"),
            },
        },
    )
    if res.matched_count == 0:
        raise HTTPException(404, f"No country_config for {country_code}")
    doc = await db.country_configs.find_one(
        {"country_code": country_code.upper()},
        {"_id": 0, "preview_mode": 1},
    )
    return {"ok": True, "preview_mode": doc.get("preview_mode")}


@api_router.post("/admin/emsc/preview/kill")
async def emsc_preview_kill(request: Request):
    """PANIC STOP — disable preview_mode on EVERY country_config immediately.

    Designed for the "3am, this is intolerable, kill it now" scenario.
    Sets `preview_mode.enabled = false` across all country_configs. Does
    NOT clear device_ids or trigger_tier — those are preserved so a
    later re-enable is one flip. Admin-only, single POST, no body needed."""
    principal = await resolve_principal(
        request, request.headers.get("x-admin-token"),
        ADMIN_TRIGGER_PASSWORD, db,
    )
    require_role(principal, "admin")
    now = datetime.now(timezone.utc)
    res = await db.country_configs.update_many(
        {"preview_mode.enabled": True},
        {"$set": {
            "preview_mode.enabled": False,
            "preview_mode.killed_at": now,
            "preview_mode.killed_by": principal.get("email", "unknown"),
        }},
    )
    return {
        "ok": True,
        "countries_affected": res.modified_count,
        "killed_by": principal.get("email"),
        "killed_at": now.isoformat(),
    }


@api_router.get("/admin/emsc/preview/candidates")
async def emsc_preview_candidates(request: Request, limit: int = Query(20, ge=1, le=100)):
    """List recent push-registered iOS devices. Useful for finding your
    own device_id to add to the allowlist without needing to fish it out
    of the audit log or the phone."""
    principal = await resolve_principal(
        request, request.headers.get("x-admin-token"),
        ADMIN_TRIGGER_PASSWORD, db,
    )
    require_role(principal, "admin")
    rows = await db.push_devices.find(
        {"platform": {"$in": ["ios", "iOS"]}},
        {"_id": 0, "user_id": 1, "platform": 1, "device_token": 1,
         "created_at": 1, "updated_at": 1},
    ).sort("updated_at", -1).limit(limit).to_list(limit)
    return {
        "count": len(rows),
        "candidates": [
            {
                "device_id": r.get("user_id"),
                "platform": r.get("platform"),
                "device_token_fingerprint": (
                    (r.get("device_token") or "")[:8] + "…" +
                    (r.get("device_token") or "")[-8:]
                ) if r.get("device_token") else None,
                "created_at": _iso(r.get("created_at")),
                "updated_at": _iso(r.get("updated_at")),
            }
            for r in rows
        ],
    }


@api_router.get("/admin/emsc/preview/recent")
async def emsc_preview_recent(
    request: Request,
    limit: int = Query(50, ge=1, le=500),
    delivered: Optional[bool] = Query(None, description="Filter to delivered=this."),
    device_id: Optional[str] = Query(None, description="Filter to a single device."),
):
    """Recent preview-notification attempts. Includes rate-limited skips
    so operators can see the honest volume the pipeline WOULD have
    produced (essential calibration signal)."""
    principal = await resolve_principal(
        request, request.headers.get("x-admin-token"),
        ADMIN_TRIGGER_PASSWORD, db,
    )
    require_role(principal, "admin", "operator")
    query: dict = {}
    if delivered is not None:
        query["delivered"] = delivered
    if device_id:
        query["device_id"] = device_id
    rows = await db.emsc_preview_notifications.find(
        query, {"_id": 0},
    ).sort("sent_at", -1).limit(limit).to_list(limit)
    return {
        "count": len(rows),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "notifications": [
            {**r, "sent_at": _iso(r.get("sent_at"))} for r in rows
        ],
    }


def _iso(v):
    if not v:
        return None
    if isinstance(v, datetime):
        if v.tzinfo is None:
            v = v.replace(tzinfo=timezone.utc)
        return v.astimezone(timezone.utc).isoformat()
    return v


def _serialize_emsc_row(r: dict) -> dict:
    """Convert datetimes to ISO strings for JSON transport."""
    out = dict(r)
    for k in ("observed_at", "ingested_at"):
        out[k] = _iso(r.get(k))
    return out


def _serialize_country_config(r: dict) -> dict:
    out = dict(r)
    for k in ("created_at", "updated_at"):
        if k in out:
            out[k] = _iso(out.get(k))
    return out




# ---------- Wire up ----------
app.include_router(api_router)

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=CORS_ALLOWED_ORIGINS,
    allow_origin_regex=CORS_ALLOWED_ORIGIN_REGEX,
    # PATCH + DELETE were missing — needed by the User Management panel
    # (role changes / user deletion). GET+POST+OPTIONS was enough for the
    # trigger-alert + preview-config flows but would silently break user-
    # mgmt once the Operators & Access panel became reachable.
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["*"],
    # Cross-origin dashboards (safequake.onrender.com → this backend) can't
    # read custom response headers unless we explicitly expose them. Missing
    # this list was blocking B1/B2 casualty reports client-side: the JS
    # `r.headers.get("X-Report-Kind")` returned null, tripping the guard
    # that verifies "the report I got is the report I asked for". Same
    # invisibility hit X-Row-Count (audit export "Downloaded N rows" toast
    # showed "?") and Content-Disposition (filename fell back to a client-
    # computed name). All three now visible to dashboard JS.
    expose_headers=[
        "X-Report-Kind",
        "X-Row-Count",
        "Content-Disposition",
    ],
)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
)
logger = logging.getLogger(__name__)

@app.on_event("startup")
async def bootstrap_users_and_indexes():
    """Ensure the users collection has the right indexes AND the first
    admin exists. Idempotent — safe to run on every startup.

    The first-admin bootstrap is baked into startup (not a one-shot script)
    so that:
      (a) A fresh deploy of the backend can never come up "empty" with no
          way for anyone to sign in — the admin whose email is baked in
          via BOOTSTRAP_ADMIN_EMAIL is always usable.
      (b) Rotating that email later is a .env change + redeploy, not a
          manual DB edit.
      (c) The first-admin email is version-controlled alongside the code
          that trusts it, so history is auditable.

    Note: bootstrap ONLY inserts. It never modifies an existing user's role
    or `allowed` state — so an admin who was disabled by another admin
    can't be silently re-enabled by a redeploy.
    """
    try:
        # Idempotent index creation.
        # partialFilterExpression (NOT sparse) so pre-linked allowlist
        # entries (email-only, google_sub=null) don't violate uniqueness
        # against each other. `sparse` only excludes MISSING fields,
        # not explicit-null values — a footgun the original code hit
        # when adding a second operator.
        await db.users.create_index(
            "google_sub",
            unique=True,
            partialFilterExpression={"google_sub": {"$type": "string"}},
            name="google_sub_unique_when_set",
        )
        await db.users.create_index("email_normalized", unique=True)
    except Exception as e:
        logger.warning("users index creation failed: %s", e)

    bootstrap_email = os.environ.get("BOOTSTRAP_ADMIN_EMAIL", "pmvincenti@gmail.com").strip().lower()
    if not bootstrap_email:
        return
    existing = await db.users.find_one({"email_normalized": bootstrap_email})
    if existing:
        return  # never touch existing rows
    await db.users.insert_one({
        "email": bootstrap_email,
        "email_normalized": bootstrap_email,
        "display_name": bootstrap_email.split("@", 1)[0],
        "role": "admin",
        "allowed": True,
        "disabled": False,
        "session_version": 1,
        "google_sub": None,        # populated on first successful sign-in
        "created_at": datetime.now(timezone.utc),
        "created_by": "bootstrap",
        "last_login_at": None,
    })
    logger.info("Bootstrapped first admin user: %s", bootstrap_email)


@app.on_event("startup")
async def start_emsc_poller():
    """Seed the country_configs collection (idempotent) and start the
    in-process EMSC/USGS shadow-mode poll loop. Separated from the users
    bootstrap so a failure in one doesn't block the other.

    The poller runs as an asyncio task tied to the FastAPI event loop —
    it stops cleanly when the app shuts down. Failures inside the poll
    loop are logged and recoverable; a bug in the evaluator cannot
    crash the API surface.
    """
    try:
        await seed_country_configs(db)
    except Exception as e:
        logger.warning("EMSC country_config seed failed: %s", e)
    try:
        await emsc_poller.start()
    except Exception as e:
        logger.warning("EMSC poller start failed: %s", e)
    try:
        await emsc_testimonies.start()
    except Exception as e:
        logger.warning("EMSC testimonies sweeper start failed: %s", e)


@app.on_event("shutdown")
async def shutdown_db_client():
    try:
        await emsc_poller.stop()
    except Exception as e:
        logger.warning("EMSC poller stop failed: %s", e)
    try:
        await emsc_testimonies.stop()
    except Exception as e:
        logger.warning("EMSC testimonies sweeper stop failed: %s", e)
    client.close()
    await _push_client.aclose()
    await apns_aclose()
