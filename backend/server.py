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


# MongoDB connection, admin secret, CORS allowlist and the rescue short-code
# helper all live in deps.py since the 2026-06-18 module split. Imported (not
# redefined) so there is exactly one Mongo client and one source of truth.
from deps import (
    ADMIN_TRIGGER_PASSWORD,
    is_test_device as _is_test_device,
    CORS_ALLOWED_ORIGIN_REGEX,
    CORS_ALLOWED_ORIGINS,
    client,
    db,
    short_code as _short_code,
)

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

# Audit-log exports + dual casualty reports live in reports_export.py since
# the 2026-06-18 split. Its endpoints were declared on this same api_router,
# so including its router keeps every path byte-identical.
from reports_export import (
    router as reports_export_router,
    DASHBOARD_SETTINGS_ID,
    _gather_devices_in_report_window,
    _get_dashboard_settings,
    _last_alert_start,
    _looks_like_credential,
    _short_codes_for,
    _trapped_since_map,
)

api_router.include_router(reports_export_router)

# Auth + user management, and the EMSC admin/preview endpoints. Both were
# declared on this api_router before the 2026-06-18 split, so including their
# routers keeps every path and method identical.
from routes_auth_users import router as auth_users_router
from routes_emsc_admin import router as emsc_admin_router

from routes_diagnostics import router as diagnostics_router
from routes_recheck import router as recheck_router

api_router.include_router(diagnostics_router)
api_router.include_router(recheck_router)
api_router.include_router(auth_users_router)
api_router.include_router(emsc_admin_router)

# EMSC/USGS poller — instantiated at import time, started in the
# `startup` handler. Held as a module global so admin endpoints
# further down can read `.started_at` etc.
# Preview APNs sender is injected here (rather than imported inside
# emsc/) so the poller subpackage stays transport-agnostic and testable
# in isolation.
from deps import emsc_poller, emsc_testimonies, iso_utc as _iso, recheck_sweeper
from recheckin import silence_state
from push_relay import PUSH_KEY, push_client as _push_client, send_push

# EMSC testimonies sweeper — Part 1a validation channel. Every 15 min,
# fetches EMS-98 felt-report intensities for recent events and updates
# `intensity_estimates.from_emsc_testimonies` in place. Separate task
# from the poller because of the different cadence (15min vs 60sec)
# and the different failure semantics — testimonies data being late
# by hours is fine; missing a poll is not.

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
    # Egress is NOT mobility (2026-06-18): mobility describes the body, egress
    # describes the building. Asked of GREEN trapped reports only — they have
    # just told us they can walk, and "minor injury but cannot get out" was
    # otherwise invisible to the operator, so nobody was dispatched with
    # cutting gear.
    egress: Optional[str] = Field(default=None, pattern=r"^(can_exit|cannot_exit)$")

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
    egress: Optional[str] = p.egress
    mobility: Optional[str] = p.mobility
    if p.status != "trapped":
        mobility = None
        egress = None

    return {
        "device_id": device_id,
        "status": p.status,
        "severity": severity,
        "mobility": mobility,
        "egress": egress,
        # Severity is a medical axis; being unable to get out is a structural
        # one. Kept as a separate flag rather than folded into severity, which
        # would either overstate the injury or hide the extraction need.
        "needs_extraction": egress == "cannot_exit",
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
        # Derive effective_status the SAME way people_counts does, so
        # the dashboard's map marker layer reads one authoritative field
        # and can never draw a rescued tick under a trapped triangle
        # (Batch 7 A2). Import lazily to keep the endpoint's cold-start
        # cost identical.
        from people_counts import effective_status as _eff
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
            # Batch 7 A2: single source of truth for what to DISPLAY. If
            # `rescued_at` is set, effective_status is "rescued" no matter
            # what `status` says. Dashboards must read effective_status,
            # not status, for markers/chips/counts.
            "effective_status": _eff(r),
            "severity": r.get("severity"),
            "mobility": r.get("mobility"),
            "egress": r.get("egress"),
            "needs_extraction": bool(r.get("needs_extraction")),
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
            # ── C1: silence is information, and its two kinds must look
            # different on the dashboard. `silent_alive` = not answering
            # re-checks but the phone is still reporting (possibly
            # unconscious, possibly can't reach the phone). `dark` = nothing
            # at all for over 45 minutes — no contact possible, so the last
            # known status and location stay pinned rather than blanking.
            # Neither reduces priority.
            "silence_state": silence_state(r),
            "recheck": r.get("recheck"),
            "deteriorating": bool(r.get("deteriorating")),
            "reports_improving": bool(r.get("reports_improving")),
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
        # C1 re-check rows carry their own kind and, for answers, the DEVICE
        # TAP TIME as `at` — an answer tapped offline and delivered 45 minutes
        # later is rendered at the time it was tapped, never the time it
        # arrived, with the arrival time kept alongside it (Paul, 2026-08-17).
        recheck_kind = r.get("kind") if r.get("kind", "").startswith("recheck") else None
        ev = {
            "kind": (recheck_kind
                     or ("rescue_reverted" if r.get("rescue_reverted")
                         else "rescued" if r.get("status") == "rescued"
                         else "status")),
            "at": (r.get("answered_at") if recheck_kind == "recheck_answered"
                   else r.get("at") if recheck_kind
                   else r.get("recorded_at") or r.get("updated_at")),
            "status": r.get("status"),
            "severity": r.get("severity"),
            "mobility": r.get("mobility"),
            "egress": r.get("egress"),
            "needs_extraction": bool(r.get("needs_extraction")),
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
        if recheck_kind:
            for f in ("answer", "check_id", "prior_severity", "deteriorating",
                      "reports_improving", "answered_at", "received_at",
                      "queued_offline", "device_clock_suspect"):
                if r.get(f) is not None:
                    ev[f] = r.get(f)
            # A re-check is never a "reconfirmation" of a status report — it is
            # an answer to a question we asked.
            ev["reconfirmation"] = False
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
    This is everything a signed-out dashboard is allowed to show.

    Reads through `people_counts.compute_counts` — the same function the
    signed-in dashboard, the re-check panel, and the PDF aggregate table
    read from. Before Batch 7 A2 this endpoint had its own inline loop
    that skipped the test-entry filter, so the public number was higher
    than the operator number for the same moment (Paul, 2026-08-19).
    """
    from people_counts import compute_counts
    c = await compute_counts(db, include_test=False)
    alert_dt = await _last_alert_start()
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total": c.total,
        # Field names preserved for backwards-compat with the deployed
        # dashboard JS (which reads counts.safe / counts.trapped / etc.).
        "counts": {
            "safe": c.safe,
            "trapped": c.trapped,
            "rescued": c.rescued,
            "not_responding": c.not_responding,
            "unknown": c.unknown,
        },
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

    Interleaves event kinds by timestamp, most recent first:
      - `trigger`:          an alert was broadcast (from push_events)
      - `status`:           a device self-reported a status change (from
                            status_events with status ∈ safe/trapped/not_responding)
      - `rescued`:          a dashboard operator marked a trapped case as
                            found & safe (from status_events with status='rescued')
      - `rescue_reverted`:  the rescued mark was undone (from status_events
                            with rescue_reverted=True)
      - `recheck_sent` / `recheck_answered` / `recheck_missed`: the C1 ladder
                            asked, was answered, or was not answered. Emitted
                            under their own kinds since 2026-06-18 — they used
                            to arrive labelled "status", which made an
                            automatic re-check answer look identical to a
                            person opening the app and reporting themselves.

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
                "egress": r.get("egress"),
                "needs_extraction": bool(r.get("needs_extraction")),
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
                # C1 re-check rows live in the same ledger (kind =
                # recheck_sent / recheck_answered / recheck_missed). Until
                # 2026-06-18 they came out of here labelled "status", so an
                # automated re-check answer was indistinguishable from a
                # person opening the app and self-reporting. The feed has to
                # be able to say which one happened.
                row_kind = r.get("kind")
                if isinstance(row_kind, str) and row_kind.startswith("recheck"):
                    events.append({
                        **base,
                        "kind": row_kind,
                        "check_id": r.get("check_id"),
                        "answer": r.get("answer"),
                        "prior_severity": r.get("prior_severity"),
                        "deteriorating": bool(r.get("deteriorating")),
                        "reports_improving": bool(r.get("reports_improving")),
                        # Tap time is authoritative for anything a human reads.
                        "answered_at": r.get("answered_at"),
                        "queued_offline": bool(r.get("queued_offline")),
                        "delivered": r.get("delivered"),
                    })
                    continue
                events.append({**base, "kind": "status"})

    # Merge and clip.
    events.sort(key=lambda e: e.get("at") or "", reverse=True)
    events = events[:limit]

    # Batch 7 C6: backfill display_name onto any event still missing it.
    # Old recheck_sent / recheck_missed rows (pre-2026-08-19) were
    # written without the name snapshot, so the activity feed showed
    # "🔁 RE-CHECK · CODE" while every other row rendered
    # "🔁 UPDATE · NAME · CODE". Look up current device_status names in
    # a single query and stamp them onto any event still missing one —
    # never overwrite a real snapshot, so a person who was renamed
    # after the event still sees their historical name on old rows.
    _missing = {e.get("device_id") for e in events
                if e.get("device_id") and not e.get("display_name")}
    if _missing:
        _name_rows = await db.device_status.find(
            {"device_id": {"$in": list(_missing)}},
            {"_id": 0, "device_id": 1, "display_name": 1},
        ).to_list(len(_missing))
        _names = {r["device_id"]: r.get("display_name") for r in _name_rows if r.get("display_name")}
        for e in events:
            if not e.get("display_name") and e.get("device_id") in _names:
                e["display_name"] = _names[e["device_id"]]

    return {
        "count": len(events),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "events": events,
    }


@api_router.get("/admin/audit-log", response_class=HTMLResponse)
async def audit_log_browser(
    request: Request,
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
    # get_audit_log gained a `request` first positional when /api/audit was put
    # behind operator auth (2026-08-13 signed-out privacy fix). This is a direct
    # Python call, so FastAPI's dependency injection does not fill it in and the
    # page 500'd until it was forwarded (found by the iteration-34 review).
    feed = await get_audit_log(
        request,
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
                # GDPR (2026-06): a trapped person's exact coordinates must
                # never leave in a URL to a third party. This used to link to
                # google.com/maps/place/<lat>,<lon>, which disclosed the
                # position of a casualty to Google on every operator click.
                # Rendered as plain text instead, rounded to 5 dp (~1 m,
                # matching the export data-minimisation rule).
                try:
                    _lat = f'{float(e.get("latitude")):.5f}'
                    _lon = f'{float(e.get("longitude")):.5f}'
                except (TypeError, ValueError):
                    _lat, _lon = str(e.get("latitude")), str(e.get("longitude"))
                loc = f' · 📍 <code>{_html.escape(_lat)}, {_html.escape(_lon)}</code>'
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

    # Serialize datetimes. MUST carry the UTC offset — a naive ISO string is
    # parsed as LOCAL time by JS/RN, which rendered an 08:07 UTC quake as
    # 08:07 on a Malta (UTC+2) phone: two hours early, on the exact
    # timestamp users compare against when a notification arrived.
    for e in events:
        obs = e.get("observed_at")
        if isinstance(obs, datetime):
            e["observed_at"] = _iso(obs)

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
                    # #169 follow-up — THIS FIELD WAS MISSING, and its absence
                    # was silently downgrading every Android earthquake alert.
                    # The app's tap handler routes by `kind` and treats a
                    # missing kind as INFORMATIONAL (a deliberate fail-safe
                    # against BUG-2026-08-06-preview-tap-siren). With no kind,
                    # an Android user tapping a REAL alert landed on the
                    # informational event screen instead of the check-in
                    # screen, and the in-app siren never armed.
                    "kind": "critical_alert",
                    # Forwarded so the alert screen renders the real event
                    # instead of dashes, exactly as on iOS.
                    "magnitude": body.magnitude,
                    "distance_km": body.distance_km,
                    "intensity": body.intensity,
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


@api_router.get("/admin/tremor-diagnostics")
async def tremor_diagnostics(request: Request):
    """"Tremor notifications — what's been sent."

    Batch 7 #225 (2026-08-19): after weeks in which Paul believed the app
    was sending tremor notices while the shipped code disagreed, this is
    the one place a human can go to see the actual truth. If the system
    is deliberately switched off, this endpoint says so — plainly — and
    the dashboard renders a persistent top-strip warning that cannot be
    dismissed.

    Design invariant this establishes:
      A feature that is switched off MUST say so somewhere a human will
      see it. This endpoint is the machine-readable half; the dashboard
      strip + panel are the human-readable half.

    Returns a shape optimised for the dashboard renderer — every field
    is either a number, a plain-English string, or a list of already-
    formatted rows. NO enums (`would_have_fired`, `shadow_mode`,
    `trigger_tier`) reach the caller. Pattern 5 discipline.
    """
    principal = await resolve_principal(
        request, request.headers.get("x-admin-token"), ADMIN_TRIGGER_PASSWORD, db,
    )
    require_role(principal, "admin", "operator")

    now = datetime.now(timezone.utc)
    cfg = await db.country_configs.find_one({"country_code": "MT"}) or {}
    pm = cfg.get("preview_mode") or {}
    preview_on = bool(pm.get("enabled"))
    allowlist = list(pm.get("device_ids") or [])

    # Pull the most recent sends from BOTH tremor collections. Both use
    # the same fields (device_id, outcome, kind, region, magnitude,
    # delivered) so we can merge into one dated list.
    prev_rows = await db.emsc_preview_notifications.find(
        {"outcome": {"$in": ["sent", "delivered"]}},
        {"_id": 0}
    ).sort("at", -1).to_list(50)
    place_rows = await db.emsc_place_notifications.find(
        {"outcome": {"$in": ["sent", "delivered"]}},
        {"_id": 0}
    ).sort("at", -1).to_list(50)

    def _row(r, source):
        return {
            "at": _iso(r.get("at")) or _iso(r.get("created_at")),
            "kind": ("Own-location tremor" if source == "preview"
                     else "Saved-place notice"),
            "region": r.get("region") or r.get("place_name") or "—",
            "magnitude": r.get("magnitude"),
            "delivered": int(r.get("delivered") or 0),
            "targeted": int(r.get("targeted") or 1),
            "device_id_tail": (r.get("device_id") or "")[-6:].upper(),
        }
    merged = ([_row(r, "preview") for r in prev_rows] +
              [_row(r, "place")   for r in place_rows])
    merged.sort(key=lambda r: r["at"] or "", reverse=True)
    recent_sends = merged[:20]
    last_send = recent_sends[0] if recent_sends else None

    # Ingestion health for context — if events are flowing but nothing is
    # sending, the human_state distinguishes "off" from "on but silent".
    n24 = await db.emsc_events.count_documents(
        {"ingested_at": {"$gte": now - timedelta(hours=24)}}
    )
    n7 = await db.emsc_events.count_documents(
        {"ingested_at": {"$gte": now - timedelta(days=7)}}
    )
    poller = await db.emsc_poller_health.find_one({}, sort=[("last_success_at", -1)]) or {}

    # ── Plain-language state string ──────────────────────────────────────
    # This is what the persistent strip and the panel headline both read.
    # Never render an enum here — the caller is a human, not a machine.
    # See Pattern 5. Two-line max: one for state, one for what to do next.
    def _human_state_line() -> str:
        if not preview_on:
            return (
                "This system is not sending tremor notifications to anyone at the moment."
            )
        if not allowlist:
            return (
                "Tremor notifications are switched on, but nobody is on the list to receive them."
            )
        if not last_send:
            return (
                f"Tremor notifications are switched on for {len(allowlist)} phone"
                f"{'' if len(allowlist)==1 else 's'}, but nothing has been sent yet."
            )
        # We have at least one send. Report the most recent in plain words.
        last_dt = datetime.fromisoformat(last_send["at"]) if last_send["at"] else None
        gap_days = (now - last_dt).days if last_dt else None
        if gap_days is not None and gap_days >= 3:
            return (
                f"Nothing has been sent for {gap_days} days. "
                f"The last one went out on {last_dt.strftime('%d %B at %H:%M')} UTC, "
                f"to {last_send['delivered']} phone"
                f"{'' if last_send['delivered']==1 else 's'}."
            )
        return (
            f"Last tremor notice sent {last_dt.strftime('%d %B at %H:%M')} UTC, "
            f"to {last_send['delivered']} phone"
            f"{'' if last_send['delivered']==1 else 's'}."
        )

    def _how_to_turn_on_line() -> Optional[str]:
        # Only shown when preview_on is False. Never explains ENUM values,
        # only actions.
        if preview_on:
            return None
        return (
            "To turn it back on: an admin must enable tremor previews and "
            "add at least one phone to the list, in Admin settings."
        )

    return {
        "at": _iso(now),
        # Human-facing fields — these are what the dashboard renders.
        "is_sending": bool(preview_on and allowlist),
        "headline": _human_state_line(),
        "how_to_turn_on": _how_to_turn_on_line(),
        "phones_on_list": len(allowlist),
        "last_send": last_send,
        "recent_sends": recent_sends,      # already plain-formatted
        # Ingestion side (kept plain-worded too).
        "events_ingested_24h": n24,
        "events_ingested_7d": n7,
        "poller_last_success_at": _iso(poller.get("last_success_at")),
        "poller_last_error": poller.get("last_error"),
        # Provenance — admin sees when the config was last touched, so
        # "changed on 12 August" answers "what changed" without exposing
        # the change itself.
        "config_last_updated_at": _iso(cfg.get("updated_at")),
    }


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
    try:
        # C1 re-check ladder. Only ever prompts devices whose CURRENT status
        # is `trapped` — see recheckin._eligible_devices.
        await recheck_sweeper.start()
    except Exception as e:
        logger.warning("Re-check sweeper start failed: %s", e)


@app.on_event("shutdown")
async def shutdown_db_client():
    try:
        await recheck_sweeper.stop()
    except Exception as e:
        logger.warning("Re-check sweeper stop failed: %s", e)
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
