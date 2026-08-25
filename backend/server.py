from fastapi import FastAPI, APIRouter, HTTPException, Header, Query, Body, Request
from fastapi.responses import HTMLResponse, Response
from dotenv import load_dotenv, dotenv_values
from starlette.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import ReturnDocument
import os
import logging
import httpx
import html as _html
from pathlib import Path
from pydantic import BaseModel, Field
from typing import Any, Dict, List, Optional
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

# #245 (Batch 7 R4, 2026-08-19 night): the phrase the operator must
# #267 (Neo, 2026-08-20 — Paul):
#   "Do not use CONFIRM — it is generic enough to become reflex, which
#    defeats the purpose of typed confirmation. Use a word that names
#    the consequence, and make the two words different enough that
#    muscle memory from one cannot carry into the other."
#
# SIREN names exactly what the operator is about to do — light up every
# phone. Letter-distinct from WIPE (device purge) and STANDDOWN (recall);
# no shared prefix, no shared shape, no accidental cross-over from a
# hand mid-flow.
#
# Case-insensitive + strip() on both sides so an operator typing under
# stress does not fail on shift-key differences or a stray leading space.
TRIGGER_ALERT_CONFIRMATION = "SIREN"

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

# #229 (Batch 7): 33 hand-designed test people. Wired here so the two
# admin endpoints (`/api/admin/test-people/seed` and `/clear`) appear
# on the same router as the rest of the admin surface. See
# `/app/memory/test-people-spec.md` for the rules that matter most:
# visibly fake names, Z-prefixed codes, never queues real pushes.
from test_people_seed import register_test_people_routes as _reg_tp
_reg_tp(api_router, db)

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
    # #289 (2026-08-24 — Paul): `not_answered` is a real answer. The phone
    # sends it when someone chose a severity and then left the "can you get
    # out?" question, so the board can say "we do not know" instead of
    # quietly filing them as walking wounded — the lowest priority there is.
    egress: Optional[str] = Field(
        default=None, pattern=r"^(can_exit|cannot_exit|not_answered)$")

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
    # #268: if this record had been resolved off the working board and the
    # phone is now reporting again, it comes straight back on. Software may
    # only ever move a record TOWARDS the working board on its own — the
    # other direction always needs a human with a reason. The return is
    # recorded so the board's history explains itself.
    prior = await db.device_status.find_one(
        {"device_id": doc["device_id"]},
        {"_id": 0, "resolved_at": 1, "resolved_reason": 1, "asks": 1,
         # #296: the annunciator needs to know what this person was BEFORE
         # this report, because "worse" is a comparison, not a state.
         "status": 1, "severity": 1, "mobility": 1, "egress": 1,
         "needs_extraction": 1, "synthetic": 1},
    )
    returning = bool((prior or {}).get("resolved_at"))
    # Upsert latest state.
    await db.device_status.update_one(
        {"device_id": doc["device_id"]},
        {"$set": doc, "$setOnInsert": {"created_at": now},
         # #268: a check-in proves the app is installed and the person is
         # answering, so both the operator's resolution and the durable
         # "app removed" stamp are cleared. This is the ONLY place either
         # is cleared, and it is the safe direction — software may move a
         # record towards the working board, never away from it.
         "$unset": {"resolved_at": "", "resolved_by": "",
                    "resolved_reason": "", "resolved_as": "",
                    "app_removed_at": "", "app_removed_source": ""}},
        upsert=True,
    )
    # #271: they answered. The unanswered-ask counter resets — a fresh
    # answer is a fresh conversation, not a third ask — and the audit row
    # for the last ask is marked answered, so "we asked and heard nothing"
    # can never be claimed about a person who did answer.
    if (prior or {}).get("asks", {}).get("unanswered"):
        await db.device_status.update_one(
            {"device_id": doc["device_id"]},
            {"$set": {"asks.unanswered": 0, "asks.answered_at": now}},
        )
        await db.record_decisions.update_many(
            {"device_id": doc["device_id"], "kind": "asked_to_check_in",
             "answered": False},
            {"$set": {"answered": True, "answered_at": now}},
        )
    # #268: a check-in is positive evidence that the app exists on that
    # phone, so a stale "Unregistered" mark on its registration is cleared
    # too — otherwise the record stays set aside even though the person is
    # visibly answering. If the token really is dead, the next push will
    # mark it again.
    await db.push_devices.update_one(
        {"user_id": doc["device_id"], "dead_token": True},
        {"$unset": {"dead_token": "", "dead_token_reason": "",
                    "dead_token_at": ""}},
    )
    if returning:
        try:
            await db.record_decisions.insert_one({
                "device_id": doc["device_id"],
                "kind": "record_returned_by_check_in",
                "reason": (
                    "This phone reported again, so the record went back on the "
                    "working board. It had been resolved: "
                    + str((prior or {}).get("resolved_reason") or "no reason recorded")
                ),
                "decided_by": "the phone reported again",
                "decided_at": now,
            })
        except Exception as e:
            logging.warning(f"Failed to log #268 board return: {e}")
    # Append immutable history row for the audit log. `device_status` only
    # holds the LATEST state; `status_events` is the append-only ledger.
    try:
        await db.status_events.insert_one({
            **doc,
            "recorded_at": now,
        })
    except Exception as e:
        logging.warning(f"Failed to append status_events: {e}")
    # #296: a report that requires an operator to act becomes an alarm on
    # the board — and one that does not (someone reporting safe, a battery
    # reading) deliberately does not. The decision lives in one place:
    # board_alarms.on_status_change.
    try:
        import board_alarms
        await board_alarms.on_status_change(db, prior, doc)
    except Exception as e:
        logging.warning(f"Failed to raise board alarm: {e}")
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

    # ── #268 (Neo, 2026-08-21 — Paul) ────────────────────────────────
    # `devices` is now the WORKING BOARD only, and `off_board` is the
    # labelled area beside it. The split is computed in
    # people_counts.load_board so the board, the counts, the map, the
    # PDFs and the CSVs cannot disagree about who is a person to search
    # for. A record is only ever moved off the working board when the
    # phone reported a positive fact (the app was removed), or it has
    # never been used at all, or a human resolved it with a reason.
    # Nothing is deleted, nothing is hidden, and anybody who has ever
    # reported needing help stays on the board whatever their phone does.
    from people_counts import load_board
    board = await load_board(db, include_test=True)

    rows = board.board
    if since:
        rows = [r for r in rows if str(r.get("updated_at") or "") >= since]
    rows.sort(key=lambda r: str(r.get("updated_at") or ""), reverse=True)
    rows = rows[:limit]

    # #296: silence is measured by the clock, so nothing arrives to
    # announce it — it has to be noticed while the board is being read.
    # Uses the same classifier output the cards use, so an alarm and a
    # card can never disagree about who has gone quiet.
    try:
        import board_alarms
        await board_alarms.sweep_silence(db, board.board)
    except Exception as e:
        logging.warning(f"Board alarm silence sweep failed: {e}")

    # 'trapped since' timestamps for the current trapped spell (item 3).
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
            "short_code": r.get("short_code") or _short_code(r.get("device_id")),
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
            # ── #268: the four kinds of silence, in plain English ────────
            # `record_state.state` is the wire value; `label` and `detail`
            # are what an operator reads and what gets spoken over a radio.
            # `held_reason` is present when a device signal was overridden
            # (someone who reported needing help, or a live alert) —
            # visible on the card so the decision is never silent.
            "record_state": r.get("record_state"),
            "ever_needed_help": bool(r.get("ever_needed_help")),
            "possible_duplicate": r.get("possible_duplicate"),
            # #271: the ask history the operator reads BEFORE they ask
            # again, plus whether they may ask now and, if not, why not.
            "ask_state": _ask_state(r),
        }

    def off(r: dict) -> dict:
        """A record NOT on the working board. Enough to identify the person
        and read back what was moved, when and why — never enough to be
        mistaken for a live casualty needing a team."""
        from people_counts import moved_by_words
        st = r.get("record_state") or {}
        return {
            "device_id": r.get("device_id"),
            "short_code": r.get("short_code") or _short_code(r.get("device_id")),
            "display_name": r.get("display_name"),
            "state": st.get("state"),
            "label": st.get("label"),
            "detail": st.get("detail"),
            "off_board_reason": st.get("off_board_reason"),
            "moved_at": r.get("resolved_at") or st.get("app_removed_at"),
            "moved_by": moved_by_words(r),
            "resolved_reason": r.get("resolved_reason"),
            "last_status": r.get("status"),
            "last_updated": r.get("updated_at"),
            "latitude": r.get("latitude"),
            "longitude": r.get("longitude"),
            "ever_needed_help": bool(r.get("ever_needed_help")),
            "platform": r.get("platform"),
            "is_test": bool(r.get("is_test")),
            "possible_duplicate": r.get("possible_duplicate"),
        }

    return {
        "count": len(rows),
        "test_count": sum(1 for r in rows if _is_test_device(r)),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "devices": [clean(r) for r in rows],
        # #268: visible rather than silent. Everything moved off the
        # working board, with when and why, for anyone to open.
        "off_board": [off(r) for r in board.off_board],
        "off_board_count": len(board.off_board),
        # Board-level notices (e.g. many phones going dark together being
        # a network failure rather than many missing people).
        "notices": board.notices,
        # "What this counts and what it leaves out", in words.
        # #283: BOTH populations, counted server-side by the same
        # function. `counts` includes test entries (it matches `devices`,
        # which also carries them, each flagged `is_test`);
        # `counts_without_test` is what the operator sees by default.
        # The dashboard picks one — it no longer sums anything itself.
        "counts": board.counts.to_dict(),
        "count_notes": board.notes,
        "counts_without_test": (board.counts_without_test.to_dict()
                                if board.counts_without_test else None),
        "count_notes_without_test": board.notes_without_test,
    }


# ---------- #296: the board's alarms (ISA-18.1 annunciation) ----------
@api_router.get("/admin/alarms")
async def get_board_alarms(
    request: Request,
    include_test: int = 0,
    x_admin_token: Optional[str] = Header(default=None, alias="X-Admin-Token"),
):
    """Every alarm that is not yet resolved, grouped, with the count of
    those nobody has acknowledged.

    Server-side on purpose: two operators looking at the same incident must
    see the same alarms and the same acknowledgements, and an acknowledgement
    has to survive a page reload — it will be read back in an inquiry.

    #301: `include_test=1` mirrors the board's "Show test entries" tick, so
    the alarm panel can be rehearsed with test people instead of only ever
    being used for real for the first time during an incident.
    """
    principal = await resolve_principal(request, x_admin_token, ADMIN_TRIGGER_PASSWORD, db)
    require_role(principal, "admin", "operator")
    import board_alarms
    return await board_alarms.list_open(db, include_test=bool(include_test))


@api_router.post("/admin/alarms/ack")
async def ack_board_alarms(
    payload: dict = Body(...),
    request: Request = None,
    x_admin_token: Optional[str] = Header(default=None, alias="X-Admin-Token"),
):
    """Acknowledge one alarm or one group of them.

    Acknowledging stops the sound and the flashing. It does NOT clear the
    alarm: the highlight stays until the person is rescued or deliberately
    taken off the board. Who and when are recorded.
    """
    principal = await resolve_principal(request, x_admin_token, ADMIN_TRIGGER_PASSWORD, db)
    require_role(principal, "admin", "operator")
    import board_alarms

    ids = payload.get("ids") or []
    group_key = str(payload.get("group_key") or "").strip()
    # #286 (Paul, 2026-08-24): "47 individual acknowledge buttons is not
    # usable." At mass-casualty scale the operator needs one action that
    # silences the board so they can start working the list. `all: true`
    # acknowledges every alarm that is currently open and unacknowledged.
    # It changes nothing else: every alarm stays on the board, highlighted,
    # and every row still records WHO acknowledged it and WHEN, so a bulk
    # acknowledgement reads back in an inquiry exactly like 47 individual
    # ones would.
    ack_all = bool(payload.get("all"))
    if ack_all and not ids:
        q: dict = {"resolved_at": None, "ack_at": None}
        # #301: acknowledge what the operator can actually SEE. If test
        # entries are hidden, "acknowledge all" must not quietly silence
        # test alarms the operator was never shown — and if they are
        # shown, it must include them, because they are part of the
        # rehearsal they are running.
        if not bool(payload.get("include_test")):
            q["is_test"] = {"$ne": True}
        rows = await db.board_alarms.find(q, {"_id": 0, "id": 1}).to_list(1000)
        ids = [r.get("id") for r in rows if r.get("id")]
        if not ids:
            return {"status": "ok", "acknowledged": 0,
                    "acknowledged_by": (principal.get("email") if principal else None) or "unknown"}
    elif group_key and not ids:
        rows = await db.board_alarms.find(
            {"group_key": group_key, "resolved_at": None}, {"_id": 0, "id": 1},
        ).to_list(500)
        ids = [r.get("id") for r in rows if r.get("id")]
    if not ids:
        raise HTTPException(400, "Nothing to acknowledge.")
    who = (principal.get("email") if principal else None) or "unknown"
    n = await board_alarms.ack(db, [str(i) for i in ids], who)
    return {"status": "ok", "acknowledged": n, "acknowledged_by": who}


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
    from people_counts import compute_counts, counts_notes
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
            # #268: the four kinds of silence. Added rather than folded
            # into the five above, so the deployed dashboard's existing
            # reads keep working while the new wording rolls out.
            "waiting_for_answer": c.waiting_for_answer,
            "phone_went_dark": c.phone_went_dark,
            # #276: asked, their phone confirmed it arrived, still no answer.
            "no_answer": c.no_answer,
            "app_removed": c.app_removed,
            "never_used": c.never_used,
            "resolved_by_operator": c.resolved_by_operator,
            "off_board_total": c.off_board_total,
        },
        # "Every number must say what it counts and what it leaves out."
        "count_notes": counts_notes(c),
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
        # #299 (Paul, 2026-08-25): "a TRIGGER FAILED · M? · 0 people entry
        # appeared around the time of a stand-down." It was the stand-down.
        # `push_events` holds several kinds of row — the alert itself, the
        # stand-down, the reminder-cancelling push — and this query used to
        # read all of them and stamp TRIGGER on every one. A stand-down has
        # no magnitude and no recipients, so it rendered as a failed
        # trigger: the feed was inventing a failure that never happened.
        # Trigger rows only from here on (rows written before the `kind`
        # field existed are triggers, hence the $exists arm).
        tq: dict = {"$or": [{"kind": "trigger"}, {"kind": {"$exists": False}}]}
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

    # ---- Stand-downs (#299) ----
    # Calling an alert off is a decision an operator made, and it belongs in
    # the feed under its own name. It used to be shown as a failed trigger.
    if kind in (None, "stand_down"):
        sq: dict = {"kind": "alert_stood_down"}
        if since:
            sq["created_at"] = {"$gte": since}
        for r in await db.push_events.find(sq, {"_id": 0}).sort(
            "created_at", -1,
        ).to_list(limit):
            events.append({
                "kind": "stand_down",
                "at": r.get("created_at"),
                "stood_down_by": (r.get("stood_down_by") or r.get("triggered_by")
                                  or "dashboard"),
                "recipients_total": (r.get("recipients_total")
                                     or r.get("recipients") or 0),
                "reason": r.get("reason"),
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

    # ---- #296: who acknowledged which alarm, and when ----
    # An inquiry will ask "who saw this, and when did they see it". The
    # acknowledgement is the answer, so it belongs in the same feed as
    # everything else a human did.
    if kind in (None, "alarm_acknowledged"):
        aq: dict = {"ack_at": {"$ne": None}}
        if since:
            aq["ack_at"]["$gte"] = since
        rows = await db.board_alarms.find(aq, {"_id": 0}).sort("ack_at", -1).to_list(limit)
        for r in rows:
            events.append({
                "kind": "alarm_acknowledged",
                "at": r.get("ack_at"),
                "device_id": r.get("device_id"),
                "short_code": r.get("short_code"),
                "display_name": r.get("display_name"),
                "alarm_kind": r.get("kind"),
                "alarm_word": r.get("word"),
                "alarm_headline": r.get("headline"),
                "alarm_raised_at": r.get("created_at"),
                "acknowledged_by": r.get("ack_by"),
                "resolved_at": r.get("resolved_at"),
                "resolved_reason": r.get("resolved_reason"),
            })

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


# #262 (Neo, 2026-08-20 — Paul): "reject invalid device tokens, rate-limit
# registrations per IP." Two independent, narrow safeguards on
# /register-push — the one endpoint in this app with no login in front
# of it (by necessity: a phone must be able to register before anyone
# has signed in anywhere).
#
# 1. Format validation — rejects garbage BEFORE it ever reaches
#    push_devices, so it never inflates the count Paul reads on the
#    trigger confirm dialog or the device registry. Deliberately loose
#    on Android: FCM/native token length and charset aren't a single
#    fixed spec the way APNs' is, and rejecting a real device over an
#    over-strict guess would be worse than letting a little garbage
#    through. iOS tokens ARE a fixed, well-documented format (raw APNs
#    device tokens are hex), so that check is tight.
_IOS_TOKEN_RE = _re.compile(r"^[0-9a-fA-F]{32,200}$")
_ANDROID_TOKEN_MIN_LEN = 32
_ANDROID_TOKEN_RE = _re.compile(r"^[A-Za-z0-9_\-:.]+$")

def _validate_register_push_body(body: "RegisterPushBody") -> None:
    platform = (body.platform or "").strip().lower()
    if platform not in ("ios", "android"):
        raise HTTPException(
            400, "platform must be 'ios' or 'android'.",
        )
    token = (body.device_token or "").strip()
    if not token:
        raise HTTPException(400, "device_token is required.")
    if platform == "ios" and not _IOS_TOKEN_RE.match(token):
        raise HTTPException(
            400,
            "That doesn't look like a real iOS push token "
            "(expected a hex string). Registration refused.",
        )
    if platform == "android" and (
        len(token) < _ANDROID_TOKEN_MIN_LEN or not _ANDROID_TOKEN_RE.match(token)
    ):
        raise HTTPException(
            400,
            "That doesn't look like a real Android push token "
            "(too short or contains unexpected characters). Registration refused.",
        )


# 2. Per-IP rate limit — a real phone registers once per install / token
# refresh; a script trying to inflate the device count would not.
# Generous on purpose (a household or a day of test-device reinstalls
# behind one NAT'd IP must never be the thing that trips this) — this
# is aimed at scripted abuse, not normal use. Bucketed by hour in Mongo
# so it survives a backend restart and works the same across however
# many backend replicas are running; a TTL index (see bootstrap_users_
# and_indexes) expires old buckets automatically.
REGISTER_RATE_LIMIT_PER_HOUR = 20

def _client_ip(request: Optional[Request]) -> Optional[str]:
    if request is None:
        return None
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else None

async def _enforce_register_rate_limit(request: Optional[Request]) -> None:
    ip = _client_ip(request)
    if not ip:
        # Can't identify the caller — fail OPEN rather than block a real
        # device because of a proxy/test-client quirk that hides the IP.
        return
    now = datetime.now(timezone.utc)
    bucket = now.strftime("%Y-%m-%dT%H")
    doc = await db.push_register_rate_limit.find_one_and_update(
        {"_id": f"{ip}:{bucket}"},
        {
            "$inc": {"count": 1},
            "$setOnInsert": {
                "ip": ip,
                "bucket": bucket,
                "created_at": now,
                "expires_at": now + timedelta(hours=2),
            },
        },
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    count = (doc or {}).get("count", 1)
    if count > REGISTER_RATE_LIMIT_PER_HOUR:
        raise HTTPException(
            429,
            "Too many device registrations from this network in the last "
            "hour. If this is a real device having trouble, wait a bit "
            "and it will register automatically on next launch.",
        )


class TriggerAlertBody(BaseModel):
    triggeredBy: Optional[str] = None
    magnitude: Optional[float] = None
    distance_km: Optional[float] = None
    intensity: Optional[str] = None
    # #245 (Batch 7 R4, 2026-08-19 night — Paul):
    #   "A real alert cannot be sent without an explicit confirmation
    #    naming the consequence, plus a fresh password, and the audit
    #    log afterwards shows all of it."
    #
    # Google auth in this product carries no local password, so
    # "fresh password" maps to the same TYPE-TO-CONFIRM pattern that
    # the dashboard already uses for delete-user and change-role:
    # the operator types a phrase naming the consequence.
    #
    # Case-insensitive comparison against TRIGGER_ALERT_CONFIRMATION.
    # A blank or wrong phrase produces a plain-language 400 — never a
    # bare "unauthorised". This field is REQUIRED for the endpoint to
    # send anything; it is checked before ANY push is queued and its
    # presence + value are written to the audit log.
    confirmation_phrase: Optional[str] = None


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
    from reports_export import looks_like_a_placeholder
    saved = (s.get("authority_name") or "").strip() or None
    # #297: a name already saved before the check existed is still in the
    # database. The reports refuse to print it, so the settings panel has
    # to say so — otherwise the operator sees it listed as current and
    # assumes it is on their documents.
    ignored = bool(saved and looks_like_a_placeholder(saved))
    # Deliberately DON'T include _id or updated_by metadata in the
    # anonymous public response — that's operator identity.
    return {
        "authority_name": saved,
        "authority_name_ignored": ignored,
        "authority_name_printed": (
            "the responsible authorities" if (ignored or not saved) else saved
        ),
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
    # #297 (2026-08-24 — Paul): "Emergency test name" reached a real
    # downloadable public report and stayed there. This name is printed on
    # documents read by families, journalists and possibly a court, and
    # none of them can tell a placeholder from a real agency. So a
    # test-looking name is refused here, in plain words, rather than
    # published.
    from reports_export import looks_like_a_placeholder
    if looks_like_a_placeholder(name):
        raise HTTPException(
            status_code=400,
            detail=(
                f"\u201c{name}\u201d reads like a test entry, and this name is "
                "printed on reports that go outside this room. Type the real "
                "name of the authority, or leave it empty \u2014 the reports "
                "then say \u201cthe responsible authorities\u201d, which is "
                "always true."
            ),
        )
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
async def register_push(body: RegisterPushBody, request: Request = None):
    """Register a device's native push token with the Emergent push relay,
    and remember it locally so we can broadcast alerts to every device.

    #266 / #260 (Neo, 2026-08-20 — Paul): "Please make sure that when
    registration is refused for that reason, the app says so in plain
    English on the Is this working? screen, and the dashboard's
    Registered devices panel makes it obvious that registrations are
    being refused rather than just showing an empty list that looks
    normal."

    Ordering (this is the fix — previously Mongo was written FIRST and
    then the relay call could 500, leaving a row nobody could actually
    push to):

      1. Validate token format → 400 if bad.
      2. Rate-limit by IP → 429 if exceeded.
      3. Call the Emergent push relay:
         - 2xx: relay accepted → upsert into push_devices and return 201.
         - 4xx (except 429): relay refused the token/creds → DO NOT
           write into push_devices (a row here would be a lie: the
           relay wouldn't deliver to this device). Return 502 with a
           plain-English detail so the mobile app can say so.
         - 5xx: relay is transient-down → still upsert into
           push_devices (best-effort: better to have the device on
           file for when the relay comes back than lose it), return
           502 with a "will retry" message.
      4. Always log the attempt to push_registrations_log — that log is
         the source of truth the dashboard's relay-health panel and
         /admin/relay-health read to tell an operator that "any 0 count
         below is misleading, registrations are being refused".
    """
    _validate_register_push_body(body)
    await _enforce_register_rate_limit(request)
    now = datetime.now(timezone.utc).isoformat()

    relay_status: Optional[int] = None
    relay_body = None
    relay_error: Optional[str] = None
    # Human-readable reason surfaced in the error detail. This string is
    # displayed to the user by the mobile app (Is this working? screen)
    # and by the dashboard (Registered devices panel), so it must be
    # plain English — no "HTTP", no code numbers.
    friendly_reason: Optional[str] = None

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
    except HTTPException:
        raise
    except Exception as e:
        # Network error reaching the relay. Treat as transient — same
        # bucket as 5xx: keep the row so a retry can push through.
        relay_error = f"network: {e}"
        friendly_reason = (
            "We couldn't reach the push provider right now. Your phone "
            "will try again automatically."
        )
        logging.warning(f"Push register relay unreachable: {e}")

    # Decide the outcome based on relay_status.
    persisted = False
    try:
        if relay_status is not None and 200 <= relay_status < 300:
            # Happy path — relay accepted the device.
            await db.push_devices.update_one(
                {"user_id": body.user_id},
                {"$set": {
                    "user_id": body.user_id,
                    "platform": body.platform,
                    "device_token": body.device_token,
                    "updated_at": now,
                },
                 "$setOnInsert": {"created_at": now},
                 # A successful re-register clears any earlier dead-token
                 # mark from _prune_dead_devices — the phone is alive again.
                 "$unset": {
                     "dead_token": "", "dead_token_reason": "", "dead_token_at": "",
                 }},
                upsert=True,
            )
            persisted = True
            # #268: the app is demonstrably installed again on this phone,
            # so clear the durable "app removed" stamp on the rescue record
            # too. Same safe direction as a check-in: towards the board.
            await db.device_status.update_one(
                {"device_id": body.user_id},
                {"$unset": {"app_removed_at": "", "app_removed_source": ""}},
            )
        elif relay_status is None:
            # Network-error path: best-effort persist so retries work.
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
            persisted = True
        elif relay_status >= 500:
            # Transient relay failure — same bucket as network error.
            relay_error = f"Push provider {relay_status}"
            friendly_reason = (
                "The push provider is having trouble right now. Your phone "
                "will try again automatically."
            )
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
            persisted = True
        else:
            # 4xx from the relay — auth or bad token. DO NOT persist.
            # This is the single change that stops the "row on file but
            # push undeliverable" false promise from #266.
            if relay_status == 401:
                relay_error = "EMERGENT_PUSH_KEY missing or invalid"
                friendly_reason = (
                    "Registrations are being refused by our push provider "
                    "(server credentials issue). Your phone will register "
                    "automatically once this is fixed on the server."
                )
            else:
                relay_error = f"Relay HTTP {relay_status}"
                friendly_reason = (
                    "The push provider refused this device's registration. "
                    "Try again in a moment; if that doesn't work, contact "
                    "support."
                )
            logging.warning(
                f"Push register relay {relay_status}: {str(relay_body)[:500]}"
            )
    except Exception as e:
        # Mongo write itself failed — treat as 5xx.
        relay_error = f"db: {e}"
        friendly_reason = (
            "The server couldn't save this device's registration. Try "
            "again in a moment."
        )
        logging.warning(f"Push register DB write failed: {e}")

    # Always persist a diagnostic row — this is what /admin/relay-health
    # and the dashboard's relay-health banner read to decide whether to
    # tell the operator "the 0 count is misleading, registrations are
    # being refused".
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
            "persisted": persisted,
        })
    except Exception as e:
        logging.warning(f"Failed to persist push_registrations_log: {e}")

    # If we didn't persist to push_devices, tell the caller — the mobile
    # app uses this signal to keep the "on the alert list" row red and
    # show the friendly_reason to the operator.
    if not persisted:
        raise HTTPException(
            502,
            friendly_reason
            or "The server couldn't complete this device's registration.",
        )
    if relay_status is not None and relay_status >= 500 or relay_status is None:
        # Persisted best-effort, but still tell the client the relay
        # didn't confirm. This keeps the app honest about "the server
        # can't currently reach your phone" without losing the row.
        raise HTTPException(
            502,
            friendly_reason
            or "The push provider is having trouble right now.",
        )
    return {"status": "registered"}


@api_router.get("/register-push/status/{user_id}")
async def register_push_status(user_id: str):
    """#266 / #260 (Neo, 2026-08-20 — Paul): a phone can ask the server
    "do you actually hold my registration?" without an admin token.

    This is the read-back the "Is this working?" screen uses to compute
    a single, truthful "Your phone is on the server's alert list" row —
    replacing two separate local-only checks (has-a-token + last-status-
    was-2xx) that could disagree with the server. The row goes green
    only when this endpoint returns `registered: true` and `dead_token:
    false`.

    No auth: the phone only asks about ITS OWN id (which it generated
    itself and is not a secret — same value already exposed under
    /admin/emsc/preview/candidates). Returns nothing that could be
    exploited if guessed — a boolean plus timestamps plus the relay's
    recent health.
    """
    uid = (user_id or "").strip()
    if not uid:
        raise HTTPException(400, "user_id is required.")

    row = await db.push_devices.find_one(
        {"user_id": uid},
        {"_id": 0, "platform": 1, "device_token": 1, "created_at": 1,
         "updated_at": 1, "dead_token": 1, "dead_token_reason": 1,
         "dead_token_at": 1},
    )

    # Read the last registration attempt (successful OR refused) so the
    # phone can show a plain-English reason line.
    last_attempt = await db.push_registrations_log.find_one(
        {"user_id": uid},
        {"_id": 0, "created_at": 1, "relay_status": 1, "relay_error": 1,
         "persisted": 1},
        sort=[("created_at", -1)],
    )

    # Relay health across the entire population (not just this device):
    # if none of the last 5 attempts across all devices persisted, the
    # relay is refusing registrations globally and this phone's failure
    # is not user-fixable. This is the same signal the dashboard's
    # relay-health banner uses.
    relay_healthy = await _relay_recent_healthy()

    return {
        "registered": bool(row and row.get("device_token") and not row.get("dead_token")),
        "platform": (row or {}).get("platform"),
        "last_seen_at": _iso((row or {}).get("updated_at")) if row else None,
        "dead_token": bool((row or {}).get("dead_token")),
        "dead_token_reason": (row or {}).get("dead_token_reason"),
        "last_attempt": ({
            "at": _iso(last_attempt.get("created_at")),
            "relay_status": last_attempt.get("relay_status"),
            "relay_error": last_attempt.get("relay_error"),
            "persisted": bool(last_attempt.get("persisted")),
        } if last_attempt else None),
        "relay_healthy": relay_healthy,
    }


async def _relay_recent_healthy() -> Optional[bool]:
    """Global signal: is the Emergent push relay currently accepting
    registrations at all? Reads the last 5 push_registrations_log rows
    (across all devices, most-recent-first) and returns:
      True  — at least one relay 2xx in the sample.
      False — sample non-empty AND every relay_status is None or non-2xx.
      None  — no sample (fresh install, nobody has tried yet).

    Kept small on purpose — a bigger window would need a "recent" bound
    and wouldn't reflect the CURRENT state, which is exactly what the
    banner needs to say."""
    try:
        rows = await db.push_registrations_log.find(
            {}, {"_id": 0, "relay_status": 1},
        ).sort("created_at", -1).to_list(5)
    except Exception:
        return None
    if not rows:
        return None
    for r in rows:
        rs = r.get("relay_status")
        if rs is not None and 200 <= int(rs) < 300:
            return True
    return False


@api_router.get("/admin/relay-health")
async def admin_relay_health(
    request: Request,
    x_admin_token: Optional[str] = Header(default=None, alias="X-Admin-Token"),
):
    """#266 / #260 (Neo, 2026-08-20 — Paul): dashboard-visible signal
    that "registrations are being refused" so an empty Registered
    Devices panel doesn't LOOK LIKE "no devices yet, all is well".

    Returns the same `healthy` boolean as the per-device status endpoint
    plus the last 5 attempts (status codes + reasons) so an operator can
    see WHY at a glance without opening a database console.

    Same auth as the read-only /admin/device-registry endpoint (admin
    OR operator) — this is diagnostic information for the same audience.
    """
    principal = await resolve_principal(request, x_admin_token, ADMIN_TRIGGER_PASSWORD, db)
    require_role(principal, "admin", "operator")

    healthy = await _relay_recent_healthy()
    rows = await db.push_registrations_log.find(
        {}, {"_id": 0, "created_at": 1, "relay_status": 1,
             "relay_error": 1, "persisted": 1, "user_id": 1,
             "platform": 1, "token_fingerprint": 1},
    ).sort("created_at", -1).to_list(5)

    reason: Optional[str] = None
    if healthy is False:
        # Find the most-recent explicit relay_error to name the cause.
        for r in rows:
            err = r.get("relay_error")
            if err:
                if "EMERGENT_PUSH_KEY" in err:
                    reason = (
                        "The push provider is rejecting our credentials "
                        "(EMERGENT_PUSH_KEY appears to be missing or "
                        "invalid on this deployment). Any 0 count in "
                        "Registered devices below is misleading."
                    )
                elif "network" in err:
                    reason = (
                        "We couldn't reach the push provider on the last "
                        "few attempts. Any 0 count below may be misleading."
                    )
                else:
                    reason = (
                        f"The push provider refused the last few "
                        f"registrations ({err}). Any 0 count below may "
                        f"be misleading."
                    )
                break
        if not reason:
            reason = (
                "The last few registration attempts were refused. Any 0 "
                "count in Registered devices below may be misleading."
            )
    elif healthy is None:
        reason = (
            "No registration attempts have been recorded yet on this "
            "deployment. The 0 count below is expected until a phone "
            "registers."
        )

    return {
        "healthy": healthy,
        "reason": reason,
        "recent_attempts": [
            {
                "at": _iso(r.get("created_at")),
                "user_id": r.get("user_id"),
                "platform": r.get("platform"),
                "token_fingerprint": r.get("token_fingerprint"),
                "relay_status": r.get("relay_status"),
                "relay_error": r.get("relay_error"),
                "persisted": bool(r.get("persisted")),
            }
            for r in rows
        ],
        "generated_at": _iso(datetime.now(timezone.utc)),
    }

# ---------- #268: taking a record off the working board, by a human ----
# Doctrine (Paul, 2026-08-21): "Never delete a person from a rescue board.
# Records get resolved by a human with a reason recorded, never removed
# silently by software. This will be read back in an inquiry."
#
# So: no delete, ever. `resolved_at` / `resolved_by` / `resolved_reason`
# are set on the device_status row, an immutable row is appended to
# `status_events` AND to `record_decisions`, and the record then appears
# in the dashboard's labelled off-board area with who moved it, when and
# why. Reversible by `/records/{id}/unresolve`, which is also recorded.
#
# A reason is REQUIRED. An unexplained removal from a rescue board is
# exactly the thing an inquiry would ask about, so the API refuses it.
RESOLVE_REASONS = {
    "duplicate": "Same person as another record",
    "app_removed": "The app was removed from this phone",
    "never_used": "Registered but never used the app",
    "test_entry": "Our own test entry",
    "accounted_for": "Accounted for by another means (radio, in person)",
    "other": "Other (explained in the note)",
}


async def _record_decision(
    device_id: str, kind: str, principal, *,
    other_device_id: Optional[str] = None,
    reason_code: Optional[str] = None,
    reason: Optional[str] = None,
) -> str:
    now = _iso(datetime.now(timezone.utc))
    who = audit_attribution(principal)
    doc = {
        "device_id": device_id,
        "kind": kind,
        "other_device_id": other_device_id,
        "reason_code": reason_code,
        "reason": reason,
        "decided_by": who,
        "decided_at": now,
    }
    await db.record_decisions.insert_one(dict(doc))
    try:
        await db.status_events.insert_one({**doc, "recorded_at": now,
                                           "status": None})
    except Exception as e:
        logging.warning(f"Failed to append record_decisions to status_events: {e}")
    logging.info(f"[#268] {kind} on {device_id} by {who}: {reason_code} {reason}")
    return now


@api_router.post("/admin/records/{device_id}/resolve")
async def resolve_record(
    device_id: str,
    payload: dict = Body(...),
    request: Request = None,
    x_admin_token: Optional[str] = Header(default=None, alias="X-Admin-Token"),
):
    """Move ONE record off the working board, by a named human, with a
    reason. Nothing is deleted and the record stays fully readable in the
    off-board area and in every export."""
    principal = await resolve_principal(request, x_admin_token, ADMIN_TRIGGER_PASSWORD, db)
    require_role(principal, "admin", "operator")

    code = str(payload.get("reason_code") or "").strip()
    note = str(payload.get("reason") or "").strip()
    if code not in RESOLVE_REASONS:
        raise HTTPException(
            400,
            "Choose a reason for taking this record off the working board: "
            + ", ".join(sorted(RESOLVE_REASONS)),
        )
    if code == "other" and len(note) < 3:
        raise HTTPException(
            400,
            "Say in a few words why. The record has to explain itself later.",
        )
    if note:
        _cred = _looks_like_credential(note)
        if _cred:
            raise HTTPException(422, f"Note not saved — it {_cred}.")

    row = await db.device_status.find_one({"device_id": device_id}, {"_id": 0})
    if not row:
        raise HTTPException(404, "No record with that id.")

    # #268 follow-up: status outranks device state, so taking a person who
    # has EVER reported needing help off the working board is allowed —
    # a human may know they are accounted for — but never by accident. It
    # takes a second, explicit acknowledgement, and the refusal says why.
    import record_state as _rs
    ever_helped = (device_id in await _rs.help_history_ids(db)
                   or _rs.ever_needed_help_row(row))
    if ever_helped and not payload.get("acknowledge_help_history"):
        raise HTTPException(
            409,
            f"{_short_code(device_id)} asked for help at some point. "
            "Taking that record off the working board needs a second yes. "
            "Nothing is deleted, and you can put it back. "
            "Your name, the time and your reason go on the record.",
        )

    reason_text = RESOLVE_REASONS[code] + (f" — {note}" if note else "")
    now = await _record_decision(
        device_id, "record_resolved", principal,
        reason_code=code, reason=reason_text,
        other_device_id=str(payload.get("other_device_id") or "") or None,
    )
    await db.device_status.update_one(
        {"device_id": device_id},
        {"$set": {
            "resolved_at": now,
            "resolved_by": audit_attribution(principal),
            "resolved_reason": reason_text,
            "resolved_as": code,
        }},
    )
    # #296: deliberately taking a record off the working board resolves its
    # alarms — a named human with a reason is the other legitimate way out.
    try:
        import board_alarms
        await board_alarms.resolve_for_device(
            db, device_id,
            reason=f"Taken off the working board by {audit_attribution(principal)}: {reason_text}",
        )
    except Exception as e:
        logging.warning(f"Failed to resolve board alarms on off-board: {e}")
    return {"status": "ok", "device_id": device_id, "resolved_at": now,
            "resolved_by": audit_attribution(principal),
            "resolved_reason": reason_text}


@api_router.post("/admin/records/{device_id}/unresolve")
async def unresolve_record(
    device_id: str,
    payload: dict = Body(default={}),
    request: Request = None,
    x_admin_token: Optional[str] = Header(default=None, alias="X-Admin-Token"),
):
    """Put a record back on the working board. Operators make mistakes at
    4am and a record must never be one-way. Also recorded, with who."""
    principal = await resolve_principal(request, x_admin_token, ADMIN_TRIGGER_PASSWORD, db)
    require_role(principal, "admin", "operator")

    row = await db.device_status.find_one({"device_id": device_id}, {"_id": 0})
    if not row:
        raise HTTPException(404, "No record with that id.")
    if not row.get("resolved_at"):
        raise HTTPException(400, "This record is already on the working board.")

    await _record_decision(
        device_id, "record_unresolved", principal,
        reason=str((payload or {}).get("reason") or "").strip() or None,
    )
    await db.device_status.update_one(
        {"device_id": device_id},
        {"$unset": {"resolved_at": "", "resolved_by": "",
                    "resolved_reason": "", "resolved_as": ""}},
    )
    return {
        "status": "ok",
        "device_id": device_id,
        "message": (
            "Put back, and saved. This record now reads whatever its own phone "
            "says. If the phone said the app was removed, it will say that."
        ),
    }


@api_router.post("/admin/records/{device_id}/duplicate-decision")
async def duplicate_decision(
    device_id: str,
    payload: dict = Body(...),
    request: Request = None,
    x_admin_token: Optional[str] = Header(default=None, alias="X-Admin-Token"),
):
    """An operator answers "is this the same person as X?".

    Software never decides this (duplicates.py only ever suggests, with
    the evidence). CONFIRM resolves the OLDER of the two records with the
    reason "same person as <code>" and leaves the newer one working;
    nothing is merged, no field is copied between records, and the older
    record stays fully readable. REJECT records the rejection so the
    suggestion stops coming back on the next poll.

    Body: {"other_device_id": "...", "decision": "confirmed"|"rejected"}
    """
    principal = await resolve_principal(request, x_admin_token, ADMIN_TRIGGER_PASSWORD, db)
    require_role(principal, "admin", "operator")

    other = str(payload.get("other_device_id") or "").strip()
    decision = str(payload.get("decision") or "").strip().lower()
    if decision not in ("confirmed", "rejected"):
        raise HTTPException(400, "decision must be 'confirmed' or 'rejected'.")
    if not other:
        raise HTTPException(400, "other_device_id is required.")
    if other == device_id:
        raise HTTPException(
            400,
            "This is the same record twice. Pick the other card you are "
            "comparing it with.",
        )

    rows = await db.device_status.find(
        {"device_id": {"$in": [device_id, other]}}, {"_id": 0},
    ).to_list(2)
    by_id = {r.get("device_id"): r for r in rows}
    if device_id not in by_id or other not in by_id:
        raise HTTPException(404, "One of those records no longer exists.")

    kind = f"duplicate_{decision}"
    if decision == "rejected":
        await _record_decision(device_id, kind, principal, other_device_id=other)
        return {"status": "ok", "decision": decision,
                "message": ("Saved as two different people. Neither record "
                            "was moved or merged.")}

    # Confirmed: the OLDER record (first seen earlier) is normally the
    # stale one.
    #
    # #268 follow-up (2026-08-21, found by the testing sweep): "status
    # always outranks device state" has to hold HERE too. Picking the
    # older record by date alone let this endpoint move a TRAPPED person
    # off the working board as a side effect of an operator answering
    # "same person" — software choosing to drop a casualty. Two guards:
    #   * if exactly one of the pair has ever reported needing help, the
    #     OTHER one is the one that gets resolved, whichever is older;
    #   * if the record that would be resolved has help history anyway
    #     (both do, or the only help record is the stale one), refuse.
    #     Taking a person who has reported needing help off the board is
    #     allowed, but only as an explicit, named act via /resolve — never
    #     as a by-product of answering a duplicate question.
    import record_state as _rs
    _help_ids = await _rs.help_history_ids(db)

    def _needed_help(row) -> bool:
        return (str(row.get("device_id")) in _help_ids
                or _rs.ever_needed_help_row(row))

    def _first_seen(r):
        return str(r.get("created_at") or r.get("updated_at") or "")
    pair = sorted([by_id[device_id], by_id[other]], key=_first_seen)
    older, newer = pair[0], pair[-1]
    if _needed_help(older) != _needed_help(newer):
        # Keep the record that has reported needing help, whatever its age.
        if _needed_help(older):
            older, newer = newer, older
    if _needed_help(older):
        raise HTTPException(
            409,
            f"{_short_code(older.get('device_id'))} asked for help. "
            "Answering this question will not take that record off the board. "
            "If it really is the same person, use \u201cTake off the board\u201d "
            "on that card and give your reason.",
        )
    newer_code = _short_code(newer.get("device_id"))
    # Only now — after the guards have passed — is the operator's answer
    # written down. A refused confirmation must not leave a decision on
    # file, or the suggestion would never be shown again.
    await _record_decision(device_id, kind, principal, other_device_id=other)
    reason_text = (
        f"{RESOLVE_REASONS['duplicate']} — confirmed the same person as "
        f"{newer_code} by {audit_attribution(principal)}"
    )
    now = await _record_decision(
        older.get("device_id"), "record_resolved", principal,
        other_device_id=newer.get("device_id"),
        reason_code="duplicate", reason=reason_text,
    )
    await db.device_status.update_one(
        {"device_id": older.get("device_id")},
        {"$set": {
            "resolved_at": now,
            "resolved_by": audit_attribution(principal),
            "resolved_reason": reason_text,
            "resolved_as": "duplicate",
        }},
    )
    return {
        "status": "ok",
        "decision": decision,
        "resolved_device_id": older.get("device_id"),
        "kept_device_id": newer.get("device_id"),
        "resolved_at": now,
        "message": (
            f"Saved as the same person. "
            f"{_short_code(older.get('device_id'))} is off the working board. "
            f"{newer_code} stays. Nothing was deleted or merged."
        ),
    }


# ── #271: "Ask them to check in" ──────────────────────────────────────
# Paul, 2026-08-21: "It turns a guess into a fact, which is exactly what a
# rescue service does with silence. But it has a cost and the button must
# say so. Every ask spends that person's battery and attention. On a
# trapped person, their battery is their lifeline."
#
# The limits, and why each number:
#   MAX 2 unanswered asks. The re-check ladder already prompts twice
#     before it escalates, so two is the policy this product already
#     follows; a third ask adds no information and drains the battery we
#     need alive. Refused in plain words, with what to do instead.
#   60 MINUTE gap between asks (Paul, 2026-08-21: "once per person per
#     hour as the cap"). Anything tighter cannot produce information the
#     phone has not already had a chance to send.
#   LOW BATTERY (<=20%) WIDENS the gap to 3 hours, and the button says
#     why it is greyed out. Paul: "For a trapped person, their phone
#     battery is their lifeline, and every wake-up spends it." (#189.)
#     Even after the wait, the operator confirms the battery cost.
#   ONE PERSON AT A TIME. There is no bulk ask here, deliberately — a
#     broadcast to everyone drains every phone in the incident at once
#     and needs its own control and its own confirmation (#47).
#   NEVER the critical-alert path (#207). This uses the ordinary
#     re-check prompt. That entitlement is for real earthquake alerts,
#     and misusing it risks Apple withdrawing it.
# A fresh answer resets the counter: a new conversation, not a third ask.
ASK_MAX_UNANSWERED = 2
ASK_COOLDOWN_MINUTES = 60
ASK_COOLDOWN_MINUTES_LOW_BATTERY = 180
ASK_LOW_BATTERY_PCT = 20


def _ask_state(row: dict, now: Optional[datetime] = None) -> dict:
    """The ask history an operator sees, and whether they may ask now.

    Paul, 2026-08-21: "Show the operator the history before they ask.
    An operator who can see that will make a better decision than any
    rule I set from here. Never make them guess whether someone has
    already been chased."

    The dashboard button and the endpoint both read this, so the button
    can never offer something the server will refuse, and the reason it
    is greyed out is the same sentence the server would have said.
    """
    from record_state import dur_words

    now = now or datetime.now(timezone.utc)
    asks = row.get("asks") or {}
    count = int(asks.get("count") or 0)
    unanswered = int(asks.get("unanswered") or 0)
    last_at = _parse_iso_or_none_safe(asks.get("last_at"))
    since = int((now - last_at).total_seconds() // 60) if last_at else None

    battery = row.get("battery_pct")
    low_battery = isinstance(battery, (int, float)) and battery <= ASK_LOW_BATTERY_PCT
    gap = ASK_COOLDOWN_MINUTES_LOW_BATTERY if low_battery else ASK_COOLDOWN_MINUTES

    if count == 0:
        history = "Not asked yet."
    else:
        times = "once" if count == 1 else ("twice" if count == 2 else f"{count} times")
        history = f"Asked {times}."
        if since is not None:
            history += f" Last asked {dur_words(since)} ago"
            history += ", no answer." if unanswered > 0 else ", and they answered."
        # #276: say whether the phone confirmed our question arrived. A
        # 200 from Apple is not evidence that anybody saw anything.
        delivery = asks.get("delivery") or {}
        if unanswered > 0:
            if delivery.get("confirmed_at"):
                history += " Their phone confirmed it arrived."
            elif delivery.get("apns_status") == 200:
                history += " Their phone has not confirmed it arrived."

    can_ask, blocked = True, None
    if unanswered >= ASK_MAX_UNANSWERED:
        can_ask = False
        blocked = (
            f"Already asked {unanswered} times with no answer. "
            "Asking again is unlikely to help. Try the radio, or send a team."
        )
    elif since is not None and since < gap:
        can_ask = False
        wait = max(1, gap - since)
        blocked = (
            f"Asked {dur_words(since)} ago. Wait {dur_words(wait)}."
            + (" Their battery is low, so we leave a longer gap."
               if low_battery else "")
        )
    return {
        "count": count,
        "unanswered": unanswered,
        "last_at": asks.get("last_at"),
        "last_by": asks.get("last_by"),
        "history_words": history,
        "can_ask": can_ask,
        "blocked_reason": blocked,
        "low_battery": bool(low_battery),
        "gap_minutes": gap,
        # #276: the real delivery facts, for the operator and for an inquiry.
        "delivery": asks.get("delivery") or {},
    }


@api_router.post("/admin/records/{device_id}/ask-to-check-in")
async def ask_to_check_in(
    device_id: str,
    payload: dict = Body(default={}),
    request: Request = None,
    x_admin_token: Optional[str] = Header(default=None, alias="X-Admin-Token"),
):
    """Ask ONE phone to check in, and record that we asked.

    This is what turns "we have not heard from them" into either "they
    answered" or "we asked and heard nothing" — two facts, where before
    there was one guess.
    """
    principal = await resolve_principal(request, x_admin_token, ADMIN_TRIGGER_PASSWORD, db)
    require_role(principal, "admin", "operator")

    row = await db.device_status.find_one({"device_id": device_id}, {"_id": 0})
    if not row:
        raise HTTPException(404, "We have no record with that rescue code.")

    asks = row.get("asks") or {}
    unanswered = int(asks.get("unanswered") or 0)
    now = datetime.now(timezone.utc)
    code = _short_code(device_id)
    state = _ask_state(row, now)
    battery = row.get("battery_pct")

    if not state["can_ask"]:
        # 429 when it is only a matter of waiting, 409 when asking again
        # will never help. Same sentence the button already showed.
        status = 429 if state["unanswered"] < ASK_MAX_UNANSWERED else 409
        raise HTTPException(status, f"{code}: {state['blocked_reason']}")
    if state["low_battery"] and not payload.get("acknowledge_low_battery"):
        raise HTTPException(
            409,
            f"{code} has {int(battery)}% battery left. "
            "Asking uses some of it, and their battery may be their lifeline. "
            "Confirm again if you still want to ask.",
        )

    dev = await db.push_devices.find_one(
        {"user_id": device_id}, {"_id": 0, "device_token": 1, "platform": 1},
    )
    # Platform first: "we cannot ask an Android phone yet" is a truer and
    # more useful sentence than "this phone is not registered", and an
    # Android row often has no usable token either.
    if dev and (dev.get("platform") or "").lower() != "ios":
        raise HTTPException(
            409,
            f"{code} is an Android phone. We cannot ask a single Android "
            "phone to check in yet. Try the radio, or send a team.",
        )
    if not dev or not dev.get("device_token"):
        raise HTTPException(
            409,
            f"We cannot reach {code}. This phone is not registered for "
            "messages any more, so there is no way to ask it anything. "
            "Try the radio, or send a team.",
        )

    check_id = f"ask-{uuid.uuid4().hex[:12]}"
    low_batt = bool(state["low_battery"])
    if (row.get("status") or "") == "trapped" and not row.get("rescued_at"):
        # Already on the board asking for help. The right question for them
        # is "has anything changed?", which is the re-check prompt they
        # already know — not "are you all right?".
        from apns import send_recheck_prompts as _send_recheck
        result = await _send_recheck(
            db,
            [{"user_id": device_id, "device_token": dev["device_token"],
              "check_id": check_id}],
            title="Are you still OK?",
            body="No new earthquake. Please tap to tell us how you are.",
            idempotency_key=check_id,
            battery_saving=low_batt,
        )
    else:
        # #271: ordinary notification, ordinary sound, no siren, no Focus
        # breach, and "No new earthquake" as the first words they read.
        from apns import send_check_in_request as _send_ask
        result = await _send_ask(
            db,
            {"user_id": device_id, "device_token": dev["device_token"],
             "check_id": check_id},
            idempotency_key=check_id,
            battery_saving=low_batt,
        )
    sent = any((e.get("status_code") or 0) == 200 for e in (result.get("events") or []))
    now_iso = _iso(now)
    who = audit_attribution(principal)
    # #276: keep what Apple ACTUALLY said. Paul: "Tell me the real delivery
    # response, not that our code called the send function." A 200 from
    # Apple means accepted for delivery — it does NOT mean the phone showed
    # it, which is exactly the distinction the card has to make.
    ev = (result.get("events") or [{}])[0]
    delivery = {
        "apns_status": ev.get("status_code"),
        "apns_reason": ev.get("reason"),
        "apns_id": ev.get("apns_id"),
        "environment": ev.get("environment"),
        "accepted_at": now_iso if sent else None,
        # Set only when the phone itself tells us it arrived
        # (POST /api/push/receipt).
        "confirmed_at": None,
    }

    await db.device_status.update_one(
        {"device_id": device_id},
        {"$set": {
            "asks": {
                "count": int(asks.get("count") or 0) + (1 if sent else 0),
                "unanswered": unanswered + (1 if sent else 0),
                "last_at": now_iso if sent else asks.get("last_at"),
                "last_by": who,
                "last_check_id": check_id,
                "delivery": delivery,
            },
        }},
    )
    await db.record_decisions.insert_one({
        "device_id": device_id,
        "kind": "asked_to_check_in",
        "reason": (
            f"Asked {code} to check in."
            + ("" if sent else " The message did not get through.")
        ),
        "answered": False,
        "check_id": check_id,
        "delivery": delivery,
        "decided_by": who,
        "decided_at": now_iso,
    })

    if not sent:
        raise HTTPException(
            502,
            f"We could not get a message through to {code} just now. "
            "Nothing was lost — try again in a moment, or use the radio.",
        )
    return {
        "status": "ok",
        "device_id": device_id,
        "asked_at": now_iso,
        "asked_by": who,
        "delivery": delivery,
        "message": (
            f"Sent the question to {code}. Apple accepted it. "
            "Their phone will confirm when it arrives, and the card will "
            "say so. If we never get that confirmation, the card will say "
            "we cannot be sure they saw it."
        ),
    }


def _parse_iso_or_none_safe(s):
    try:
        from timefmt import parse as _p
        return _p(s)
    except Exception:
        return None


@api_router.post("/push/receipt")
async def push_receipt(payload: dict = Body(...)):
    """The phone tells us a question actually arrived on it.

    #276 (2026-08-21 — Paul):
      "The card must distinguish 'the phone received our question and
       nobody answered' from 'we cannot confirm the phone ever saw it'.
       Those are different facts and only one is worrying."

    A 200 from Apple means Apple accepted the push. It says nothing about
    whether the phone ever showed it. Without this receipt, a notification
    lost in transit and a person ignoring us look identical on the board —
    and an operator would send help towards somebody who is fine while the
    genuinely unreachable look the same.

    Sent by the app when it sees the notification: in the foreground, on a
    quiet background wake (`content-available`), when the person taps it,
    and on next launch for anything still sitting in Notification Centre.
    Best effort by design — a missing receipt means "we cannot confirm",
    never "they ignored us".

    Body: {"device_id": "qg-...", "check_id": "ask-...", "kind": "...",
           "how": "shown" | "tapped" | "woke", "seen_at": "<ISO, optional>"}

    Deliberately unauthenticated, like the other device-reported endpoints:
    the worst a forged receipt can do is make us LESS worried about one
    phone, and it cannot change anybody's status.
    """
    device_id = str(payload.get("device_id") or "").strip()
    if not device_id:
        raise HTTPException(400, "We need to know which phone this came from.")
    now_iso = _iso(datetime.now(timezone.utc))
    seen_at = payload.get("seen_at") or now_iso
    how = str(payload.get("how") or "shown")
    receipt = {
        "check_id": (str(payload.get("check_id")) if payload.get("check_id") else None),
        "kind": str(payload.get("kind") or ""),
        "how": how,
        "at": seen_at,
        "recorded_at": now_iso,
    }
    await db.device_status.update_one(
        {"device_id": device_id},
        {"$set": {"push_receipt": receipt}},
    )
    # Stamp the confirmation onto the ask itself when the receipt is for
    # the question we are waiting on, so the card can say "their phone
    # showed our question at 16:58".
    if receipt["check_id"]:
        await db.device_status.update_one(
            {"device_id": device_id, "asks.last_check_id": receipt["check_id"]},
            {"$set": {"asks.delivery.confirmed_at": seen_at,
                      "asks.delivery.confirmed_how": how}},
        )
        await db.record_decisions.update_many(
            {"device_id": device_id, "check_id": receipt["check_id"]},
            {"$set": {"delivery.confirmed_at": seen_at,
                      "delivery.confirmed_how": how}},
        )
    return {"ok": True, "recorded_at": now_iso}


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

    # #296: being rescued is the situation actually resolving, which is the
    # only thing allowed to clear an alarm. Acknowledging never does.
    try:
        import board_alarms
        await board_alarms.resolve_for_device(
            db, device_id,
            reason=f"Marked rescued by {rescued_by}.",
        )
    except Exception as e:
        logging.warning(f"Failed to resolve board alarms on rescue: {e}")

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




class StandDownBody(BaseModel):
    # #199 (false alarm) or #202 (incident closed). Keep it two-way so
    # a future incident-lifecycle change can drive both paths.
    reason: str = "false_alarm"
    unid: Optional[str] = None
    confirmation_phrase: Optional[str] = None


STAND_DOWN_CONFIRMATION = "STANDDOWN"


async def _stand_down_split() -> Dict[str, Any]:
    """Who a stand-down clears, and who is deliberately left on the board.

    Paul, 2026-08-21: "A stand-down must only clear the people who said
    they were safe. Anyone still asking for help stays, and you must show
    me exactly who is being left behind before I confirm."

    Before this, the stand-down push went to EVERY registered phone —
    including a trapped person's, whose check-in screen would be replaced
    by a calm "alert called off" message while they were still waiting for
    a team. That is the same class of failure as the phantom casualty:
    the board and the phone quietly disagreeing with the truth.

    "Asking for help" = effective status `trapped` (rescued wins over
    trapped in people_counts.effective_status, so someone already marked
    rescued is cleared like anyone else). Help history alone does NOT keep
    a phone out of the clear list — a person who reported trapped and has
    since reported safe is safe.
    """
    from people_counts import load_board
    import timefmt

    board = await load_board(db, include_test=True)
    staying_rows = [
        r for r in board.board
        if (r.get("effective_status") == "trapped") or r.get("needs_extraction")
    ]
    staying_ids = {str(r.get("device_id")) for r in staying_rows}
    devices = await db.push_devices.find(
        {"dead_token": {"$ne": True}},
        {"_id": 0, "user_id": 1, "device_token": 1, "platform": 1},
    ).to_list(20000)
    ios_all = [
        d for d in devices
        if (d.get("platform") or "").lower() == "ios" and d.get("device_token")
    ]
    clearing = [
        {"user_id": d.get("user_id") or "", "device_token": d.get("device_token") or ""}
        for d in ios_all
        if str(d.get("user_id") or "") not in staying_ids
    ]
    held_back = [d for d in ios_all if str(d.get("user_id") or "") in staying_ids]

    def _person(r: Dict[str, Any]) -> Dict[str, Any]:
        from record_state import dur_words
        sev = (r.get("severity") or "").lower()
        sev_words = {
            "red": "Badly hurt",
            "yellow": "Hurt",
            "green": "Not hurt, but stuck",
        }.get(sev, "Asked for help")
        # #275: how long they have been waiting, in words. An operator
        # reading "waiting 2 hours 40 minutes" behaves differently from one
        # reading a timestamp they have to subtract in their head.
        since = r.get("trapped_since") or r.get("updated_at")
        waiting = None
        parsed = _parse_iso_or_none_safe(since)
        if parsed:
            mins = int((datetime.now(timezone.utc) - parsed).total_seconds() // 60)
            if mins >= 1:
                waiting = dur_words(mins)
        return {
            "device_id": str(r.get("device_id")),
            "name": r.get("display_name") or "No name given",
            "code": r.get("short_code"),
            "words": sev_words,
            "waiting_words": waiting,
            "last_heard": timefmt.human(r.get("updated_at")),
            "battery_pct": r.get("battery_pct"),
        }

    people = sorted(
        (_person(r) for r in staying_rows if not r.get("is_test")),
        key=lambda p: {"Badly hurt": 0, "Hurt": 1}.get(p["words"], 2),
    )
    # Test entries are held back from the stand-down exactly like anyone
    # else asking for help — nothing is treated differently behind the
    # scenes. But they are COUNTED, not listed: seeing thirteen TEST
    # people in the dialog would bury the one real name in it, which is
    # the whole point of the list.
    test_staying = sum(1 for r in staying_rows if r.get("is_test"))
    # #283 (2026-08-24 — Paul): the CONFIRM dialog split real people from
    # test entries correctly, and then the result toast reported one
    # collapsed total — "13 phones", then "14" — when there was one real
    # person and thirteen test entries. Same split, same source, both
    # sides of the action.
    test_ids = {str(r.get("device_id")) for r in board.board if r.get("is_test")}
    test_ids |= {str(r.get("device_id")) for r in board.off_board if r.get("is_test")}
    clearing_test = sum(1 for d in clearing if str(d.get("user_id")) in test_ids)
    return {
        "clearing": clearing,
        "clearing_count": len(clearing),
        "clearing_test_count": clearing_test,
        "clearing_real_count": len(clearing) - clearing_test,
        "staying_count": len(staying_rows),
        "staying_real_count": len(people),
        "staying_test_count": test_staying,
        "staying_people": people,
        "staying_phones_held_back": len(held_back),
        "total_phones": len(ios_all),
    }


@api_router.post("/admin/alert/stand-down")
async def alert_stand_down(
    body: StandDownBody,
    request: Request = None,
    x_admin_token: Optional[str] = Header(default=None, alias="X-Admin-Token"),
):
    """#199 / #202 (Batch 7 R4, 2026-08-19 night — Paul):
      "Add a clear-on-stand-down path now while you're in that code."

    Sends a SILENT push to every registered iOS device telling their
    app to clear its local unanswered-alert marker. Optional `unid`
    limits the stand-down to a specific incident (leave blank for a
    blanket stand-down, which is what #199 needs today).

    Same type-to-confirm pattern as #245. The typed phrase is written
    to the audit log alongside the reason so an inquiry can
    reconstruct exactly what happened. Case-insensitive check.
    """
    principal = await resolve_principal(request, x_admin_token, ADMIN_TRIGGER_PASSWORD, db)
    require_role(principal, "admin", "operator")

    typed = (body.confirmation_phrase or "").strip().upper()
    expected = STAND_DOWN_CONFIRMATION.strip().upper()
    if typed != expected:
        # #267 (Neo, 2026-08-20): name the phrase the operator was asked
        # to type, and name what the action would have done. The previous
        # generic "type the phrase shown in the confirm dialog exactly"
        # was correct but the dashboard was throwing it away and showing
        # only a red flash — Paul reported repeatedly getting no feedback
        # when he typed the phrase in the wrong case.
        raise HTTPException(
            status_code=400,
            detail=(
                f"That did not match. Type {STAND_DOWN_CONFIRMATION} "
                f"to recall this alert."
            ),
        )

    # #274 (2026-08-21 — Paul): the stand-down used to go to every phone,
    # so a person who had reported they were trapped had their check-in
    # screen replaced with "the alert has been called off" while they were
    # still waiting for a team. Only people who are NOT asking for help
    # are cleared now. See _stand_down_split for the rule.
    split = await _stand_down_split()
    ios = split["clearing"]

    idempotency_key = f"stand-down-{uuid.uuid4().hex}"
    from apns import send_stand_down as _send_stand_down
    stand_result = (
        await _send_stand_down(
            db, ios,
            reason=body.reason or "false_alarm",
            unid=body.unid,
            idempotency_key=idempotency_key,
        )
        if ios
        else {"payload": None, "events": []}
    )

    # #296 (2026-08-24 — Paul, live test): calling the alert off cleared the
    # check-in screen but left the phone's OWN reminder ladder running, so
    # "Are you safe?" kept arriving as a CRITICAL notification roughly every
    # 90 seconds for another 11½ minutes after the alert no longer existed.
    # Those reminders are scheduled ON the device, so nothing server-side
    # reached them except the operator's separate kill-switch button — which
    # an operator standing down an alert has no reason to know they must
    # also press. Stand-down now cancels them itself, in the same action.
    #
    # Deliberately the same set as the stand-down (#274): a person who is
    # still asking for help keeps their screen and their reminders.
    reminder_result = (
        await send_silent_cancel_reminders(
            db=db,
            devices=ios,
            idempotency_key=f"stand-down-reminders-{uuid.uuid4().hex}",
            reason="stand_down",
        )
        if ios
        else {"payload": None, "events": []}
    )
    reminders_cancelled = sum(
        1 for e in (reminder_result.get("events") or []) if e.get("delivered")
    )

    await db.push_events.insert_one({
        "kind": "alert_stood_down",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "triggered_by": audit_attribution(principal),
        "reason": body.reason or "false_alarm",
        "unid": body.unid,
        "recipients": len(ios),
        "apns_events": stand_result.get("events") or [],
        "apns_payload": stand_result.get("payload"),
        "confirmation_expected": STAND_DOWN_CONFIRMATION,
        "confirmation_typed": body.confirmation_phrase or "",
        # #274: the exact split, in the record, so an inquiry can see who
        # was cleared and who was deliberately left on the working board.
        "cleared_count": len(ios),
        # #283: real people and test entries, never one collapsed number.
        "cleared_real_count": split["clearing_real_count"],
        "cleared_test_count": split["clearing_test_count"],
        "kept_on_board_count": split["staying_count"],
        "kept_on_board_real_count": split["staying_real_count"],
        "kept_on_board_test_count": split["staying_test_count"],
        "kept_on_board": split["staying_people"],
        # #296: proof in the record that the phones' own reminder ladders
        # were called off too, not just the check-in screen.
        "reminders_cancelled": reminders_cancelled,
        "reminder_apns_events": reminder_result.get("events") or [],
    })

    return {
        "ok": True,
        "recipients": len(ios),
        "reason": body.reason or "false_alarm",
        "cleared_count": len(ios),
        "cleared_real_count": split["clearing_real_count"],
        "cleared_test_count": split["clearing_test_count"],
        "kept_on_board_count": split["staying_count"],
        "kept_on_board_real_count": split["staying_real_count"],
        "kept_on_board_test_count": split["staying_test_count"],
        "kept_on_board": split["staying_people"],
        "reminders_cancelled": reminders_cancelled,
    }


@api_router.get("/admin/alert/stand-down/preview")
async def alert_stand_down_preview(
    request: Request,
    x_admin_token: Optional[str] = Header(default=None, alias="X-Admin-Token"),
):
    """What the operator is shown before they confirm a stand-down.

    #274: a bare total was not enough. The dialog must name the people who
    stay on the working board, because standing an incident down while
    somebody is still asking for help is the single most costly mistake
    an operator can make on this screen.
    """
    principal = await resolve_principal(request, x_admin_token, ADMIN_TRIGGER_PASSWORD, db)
    require_role(principal, "admin", "operator")
    split = await _stand_down_split()
    return {
        "total": split["total_phones"],
        "clearing_count": split["clearing_count"],
        "clearing_real_count": split["clearing_real_count"],
        "clearing_test_count": split["clearing_test_count"],
        "staying_count": split["staying_count"],
        "staying_real_count": split["staying_real_count"],
        "staying_test_count": split["staying_test_count"],
        "staying_people": split["staying_people"],
        "confirmation_phrase": STAND_DOWN_CONFIRMATION,
    }



@api_router.get("/admin/trigger-alert/preview")
async def trigger_alert_preview(
    request: Request,
    x_admin_token: Optional[str] = Header(default=None, alias="X-Admin-Token"),
):
    """#245 (Batch 7 R4): counts the operator sees BEFORE they confirm.

    The dashboard's type-to-confirm dialog reads this to say — plainly,
    in the confirm text — how many phones will siren if the operator
    types the phrase. Same auth as /trigger-alert itself so a signed-out
    operator does not see the counts.
    """
    principal = await resolve_principal(request, x_admin_token, ADMIN_TRIGGER_PASSWORD, db)
    require_role(principal, "admin", "operator")
    devices = await db.push_devices.find(
        {"dead_token": {"$ne": True}},
        {"_id": 0, "user_id": 1, "platform": 1, "device_token": 1},
    ).to_list(20000)
    ios = sum(
        1 for d in devices
        if (d.get("platform") or "").lower() == "ios" and d.get("device_token")
    )
    android = sum(
        1 for d in devices if (d.get("platform") or "").lower() != "ios"
    )
    total = ios + android
    return {
        "total": total,
        "ios": ios,
        "android": android,
        "confirmation_phrase": TRIGGER_ALERT_CONFIRMATION,
    }


@api_router.get("/admin/incident-status")
async def admin_incident_status(
    request: Request,
    x_admin_token: Optional[str] = Header(default=None, alias="X-Admin-Token"),
):
    """#135 (Neo, 2026-08-20 — Paul):
      "The dashboard must NEVER sign an operator out while an
       earthquake alert is live and unresolved. Being signed out
       mid-incident is a safety failure, not an inconvenience.
       Suspend the idle timer entirely whenever there is an active
       alert with anyone still unanswered."

    A live alert = the most recent trigger in `push_events` has no
    stand-down recorded after it AND arrived within the last 72h.
    72h matches the dashboard's existing "since the alert" active
    window (ALERT_ACTIVE_MS in the dashboard JS) so the operator's
    UI and this idle-timer suspend share one definition of "live".

    Returned fields:
      - active                    (bool)
      - last_trigger_at           (ISO string or null)
      - last_stand_down_at        (ISO string or null)
      - hours_since_trigger       (float or null)
      - reason                    (plain-English, non-empty when active)
    Same admin+operator gate as /admin/device-registry — this is
    dashboard-driven diagnostic info for the same audience.
    """
    principal = await resolve_principal(
        request, x_admin_token, ADMIN_TRIGGER_PASSWORD, db
    )
    require_role(principal, "admin", "operator")

    ACTIVE_WINDOW_H = 72.0

    async def _latest(kind: str):
        # #135: tolerate historical trigger rows that were inserted
        # before we started stamping `kind: "trigger"` on them. A row
        # without a `kind` is by convention a trigger — the only other
        # kind ever written to push_events is `alert_stood_down`.
        if kind == "trigger":
            query = {"$or": [{"kind": "trigger"}, {"kind": {"$exists": False}}]}
        else:
            query = {"kind": kind}
        rows = await db.push_events.find(
            query, {"_id": 0, "created_at": 1},
        ).sort("created_at", -1).to_list(1)
        if not rows:
            return None
        try:
            dt = datetime.fromisoformat(
                str(rows[0]["created_at"]).replace("Z", "+00:00")
            )
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            return None

    last_trigger = await _latest("trigger")
    last_stand_down = await _latest("alert_stood_down")

    now = datetime.now(timezone.utc)
    hours_since = None
    if last_trigger is not None:
        hours_since = (now - last_trigger).total_seconds() / 3600.0

    active = False
    reason = None
    if last_trigger is not None and hours_since is not None:
        stood_down_after = (
            last_stand_down is not None and last_stand_down > last_trigger
        )
        within_window = hours_since <= ACTIVE_WINDOW_H
        if within_window and not stood_down_after:
            active = True
            # Round to a human-readable phrase so the dashboard banner
            # can say "alert live for 2h 14m" without doing math client-side.
            h = int(hours_since)
            m = int(round((hours_since - h) * 60))
            if h == 0 and m == 0:
                dur = "under a minute"
            elif h == 0:
                dur = f"{m} minutes"
            elif m == 0:
                dur = f"{h} hours" if h != 1 else "1 hour"
            else:
                dur = (f"{h} hours {m} minutes" if h != 1
                       else f"1 hour {m} minutes")
            # #277 (2026-08-21 — Paul): "'Idle sign-out suspended' and '72h
            # window' are jargon, and it takes three readings to work out it
            # is good news." Good news first, in everyday words, and the
            # elapsed time on its own line.
            days = int(round(ACTIVE_WINDOW_H / 24))
            reason = (
                "An alert is running. You will stay signed in while it is. "
                "This lasts until you call the alert off, or "
                f"{days} days pass.\n"
                f"Running for {dur}."
            )

    return {
        "active": active,
        "reason": reason,
        "last_trigger_at": last_trigger.isoformat() if last_trigger else None,
        "last_stand_down_at": last_stand_down.isoformat() if last_stand_down else None,
        "hours_since_trigger": hours_since,
        "active_window_hours": ACTIVE_WINDOW_H,
        "generated_at": now.isoformat(),
    }


@api_router.get("/admin/device-registry")
async def device_registry(
    request: Request,
    x_admin_token: Optional[str] = Header(default=None, alias="X-Admin-Token"),
):
    """#262 follow-up (Neo, 2026-08-20 — Paul): a signed-in, in-dashboard
    view of every registered device, so an operator never has to visit
    a raw URL with the admin password sitting in the query string
    (that page — GET /api/admin/devices — still exists for now and is
    NOT removed by this change; this is an additive, safer alternative
    the dashboard can render inline).

    Deliberately queries `push_devices` with NO filter and returns the
    SAME ios/android/total breakdown as /admin/trigger-alert/preview,
    computed the same way, so the two numbers an operator sees
    (this list, and the count in the trigger confirm dialog) are
    always the same source and can be visually reconciled — that
    mismatch was the entire reason #262 was raised.

    Same auth + role gate as the trigger preview itself (admin OR
    operator) — this is audit information for the same audience who
    decides whether to pull the trigger, not a wider-access surface.

    Returns device_id (client-generated, not a secret — same value
    already shown on /admin/emsc/preview/candidates) and a token
    FINGERPRINT only; the raw push token is never returned here.
    """
    principal = await resolve_principal(request, x_admin_token, ADMIN_TRIGGER_PASSWORD, db)
    require_role(principal, "admin", "operator")

    rows = await db.push_devices.find(
        {}, {"_id": 0, "user_id": 1, "platform": 1, "device_token": 1,
             "created_at": 1, "updated_at": 1, "dead_token": 1,
             "dead_token_reason": 1, "dead_token_at": 1},
    ).sort("updated_at", -1).to_list(5000)

    def _fingerprint(tok: Optional[str]) -> Optional[str]:
        tok = tok or ""
        if not tok:
            return None
        return f"{tok[:8]}…{tok[-8:]}" if len(tok) > 16 else tok

    # Counts here MUST match /admin/trigger-alert/preview exactly — both
    # now exclude dead_token rows (#262 follow-up), since a dead-marked
    # device is not actually a phone that would siren.
    active = [r for r in rows if not r.get("dead_token")]
    ios = sum(
        1 for d in active
        if (d.get("platform") or "").lower() == "ios" and d.get("device_token")
    )
    android = sum(1 for d in active if (d.get("platform") or "").lower() != "ios")

    return {
        "total": ios + android,
        "ios": ios,
        "android": android,
        "generated_at": _iso(datetime.now(timezone.utc)),
        # Every row is still listed — including dead-marked ones — for
        # full transparency (that was the whole point of #262). The
        # `status` field is what distinguishes "counts toward a real
        # trigger" from "known-dead, excluded from the count above".
        "devices": [
            {
                "device_id": r.get("user_id"),
                "platform": (r.get("platform") or "unknown"),
                "registered_at": _iso(r.get("created_at")),
                "last_seen_at": _iso(r.get("updated_at")),
                "device_token_fingerprint": _fingerprint(r.get("device_token")),
                "status": "dead_token" if r.get("dead_token") else "active",
                "dead_token_reason": r.get("dead_token_reason"),
                "dead_token_at": _iso(r.get("dead_token_at")),
            }
            for r in rows
        ],
    }


# #262 follow-up (2026-08-20 — Paul, pre-pilot cleanup): "clear all
# current entries... these are all Paul's own repeated test installs...
# only new registrations from real testers should appear going forward."
#
# Deliberately a delete_many, unlike _prune_dead_devices in apns.py.
# #268 (2026-08-21) narrowed it: anyone who has EVER reported needing
# help is kept back, and the whole action is refused while an alert is
# live. What was removed and what was kept is reported in plain words
# and written to `record_decisions`.
# The soft-mark in apns.py exists because IT runs automatically from an
# unattended background job with no human per call — hard-deleting there
# risked silently destroying a real device. THIS is the opposite shape:
# one explicit, admin-only, human-typed-confirmation action, run by a
# human who has confirmed by hand what the rows are.
#
# admin-only (not "admin","operator" like the read/preview endpoints) —
# wiping the whole registry is a materially bigger blast radius than
# viewing it or triggering one alert.
DEVICE_PURGE_CONFIRMATION = "WIPE"


class DevicePurgeBody(BaseModel):
    confirmation_phrase: Optional[str] = None


@api_router.post("/admin/device-registry/purge-all")
async def purge_all_devices(
    body: DevicePurgeBody,
    request: Request = None,
    x_admin_token: Optional[str] = Header(default=None, alias="X-Admin-Token"),
):
    principal = await resolve_principal(request, x_admin_token, ADMIN_TRIGGER_PASSWORD, db)
    require_role(principal, "admin")

    typed = (body.confirmation_phrase or "").strip().upper()
    if typed != DEVICE_PURGE_CONFIRMATION:
        # #267 (Neo, 2026-08-20 — Paul): plain-English match error
        # naming the phrase AND the action. The dashboard shows this
        # inside the modal so a mistyped attempt is never a silent flash.
        raise HTTPException(
            400,
            f"That did not match. Type {DEVICE_PURGE_CONFIRMATION} "
            f"to erase every registered device.",
        )

    before = await db.push_devices.count_documents({})

    # ── #268 (Neo, 2026-08-21 — Paul) ────────────────────────────────
    # "Refuse to delete any record that has ever reported needing help,
    #  or that has ever been marked trapped or injured. Refuse entirely
    #  while an alert is live. Record every wipe in the audit log: who
    #  did it, when, how many records, and how many were refused and
    #  why. Tell the person on screen afterwards in plain words what was
    #  removed and what was kept back."
    import record_state as _rs
    if await _rs.incident_is_active(db):
        raise HTTPException(
            409,
            "An alert is live. Nothing can be wiped while an alert is open. "
            "Call the alert off first, then try again.",
        )
    protected_ids = await _rs.help_history_ids(db)
    # Anyone whose CURRENT row shows help history too, not just the ledger.
    for r in await db.device_status.find(
        {}, {"_id": 0, "device_id": 1, "status": 1, "rescued_at": 1,
             "trapped_since": 1, "needs_extraction": 1, "pre_rescue_status": 1},
    ).to_list(10000):
        if _rs.ever_needed_help_row(r) and r.get("device_id"):
            protected_ids.add(str(r["device_id"]))
    kept_rows = await db.push_devices.find(
        {"user_id": {"$in": sorted(protected_ids)}},
        {"_id": 0, "user_id": 1},
    ).to_list(10000) if protected_ids else []
    kept_ids = [str(r.get("user_id")) for r in kept_rows if r.get("user_id")]

    res = await db.push_devices.delete_many({"user_id": {"$nin": kept_ids}})
    after = await db.push_devices.count_documents({})

    now_iso = _iso(datetime.now(timezone.utc))
    kept_detail = [
        {"device_id": d, "short_code": _short_code(d),
         "why": "This person asked for help at some point."}
        for d in kept_ids
    ]
    try:
        await db.record_decisions.insert_one({
            "device_id": None,
            "kind": "device_registry_wipe",
            "reason": (
                f"Wiped {res.deleted_count} registered device(s). "
                f"Kept {len(kept_ids)} back because they have reported "
                f"needing help at some point."
            ),
            "deleted_count": res.deleted_count,
            "kept_count": len(kept_ids),
            "kept_device_ids": kept_ids,
            "decided_by": audit_attribution(principal),
            "decided_at": now_iso,
        })
    except Exception as e:
        logging.warning(f"Failed to log #268 wipe decision: {e}")

    logging.info(
        f"[admin] #262/#268 purge-all-devices by "
        f"{audit_attribution(principal)}: {before} -> {after} "
        f"({res.deleted_count} deleted, {len(kept_ids)} kept back)"
    )
    if len(kept_ids) == 1:
        message = (
            f"Removed {res.deleted_count} phones. 1 was kept. "
            "That person asked for help at some point. "
            "Records like that are never wiped."
        )
    elif kept_ids:
        message = (
            f"Removed {res.deleted_count} phones. {len(kept_ids)} were kept. "
            "Those people asked for help at some point. "
            "Records like that are never wiped."
        )
    else:
        message = (
            f"Removed {res.deleted_count} phones. "
            "None of them had ever asked for help, so nothing was kept."
        )
    return {
        "before": before,
        "deleted": res.deleted_count,
        "after": after,
        "kept_back": len(kept_ids),
        "kept_back_detail": kept_detail,
        "message": message,
        "purged_at": now_iso,
        "purged_by": audit_attribution(principal),
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

    #245 (Batch 7 R4): a real alert now requires a matching confirmation
    phrase in `body.confirmation_phrase`. The phrase names the
    consequence (matches TRIGGER_ALERT_CONFIRMATION below), the check
    happens BEFORE any push is queued, and the phrase that was typed is
    written into push_events.confirmation_phrase so the audit log
    shows exactly what the operator was asked and what they answered.
    Sending without the phrase — or with the wrong one — returns 400
    with a plain-language message no operator has to translate.
    """
    principal = await resolve_principal(request, x_admin_token, ADMIN_TRIGGER_PASSWORD, db)
    require_role(principal, "admin", "operator")
    triggered_by_user = audit_attribution(principal)

    # #245 (Batch 7): phrase check. Case-insensitive; whitespace-tolerant.
    # #267 (Neo, 2026-08-20 — Paul): name the phrase in the error
    # detail. The dashboard shows this verbatim inside the confirm
    # modal, replacing the silent-red-flash mismatch that read as
    # "button broken".
    typed = (body.confirmation_phrase or "").strip().upper()
    expected = TRIGGER_ALERT_CONFIRMATION.strip().upper()
    if typed != expected:
        raise HTTPException(
            status_code=400,
            detail=(
                f"That did not match. Type {TRIGGER_ALERT_CONFIRMATION} "
                f"to send the alert."
            ),
        )

    query = {"dead_token": {"$ne": True}}
    if body.triggeredBy:
        query["user_id"] = {"$ne": body.triggeredBy}
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
            # #135 (Neo, 2026-08-20 — Paul): explicit `kind` on the
            # trigger insert so /admin/incident-status can distinguish
            # a trigger from a stand-down. Historically the trigger
            # row had no `kind`; the stand-down insert (line ~2518)
            # adds `"kind": "alert_stood_down"`. Adding it here makes
            # the two symmetric and lets `_latest("trigger")` work.
            "kind": "trigger",
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
            # #245 (Batch 7 R4): what the operator was asked to type, and
            # what they actually typed. Recorded exactly so an inquiry
            # can prove the operator saw the consequence in words before
            # the phones sirened. Case is normalised for the check but
            # the raw input is preserved verbatim for the audit.
            "confirmation_expected": TRIGGER_ALERT_CONFIRMATION,
            "confirmation_typed": (body.confirmation_phrase or ""),
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
            "region": r.get("region") or r.get("place_name") or "Not known",
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

    # #262 (Neo, 2026-08-20): TTL index so hourly rate-limit buckets for
    # /register-push clean themselves up — no cron, no unbounded growth.
    # expireAfterSeconds=0 means "expire at the time stored in
    # expires_at" (already set 2h in the future when the bucket is
    # created), giving each bucket a generous grace window past its
    # actual 1h rate-limit relevance before Mongo reaps it.
    try:
        await db.push_register_rate_limit.create_index(
            "expires_at", expireAfterSeconds=0, name="ttl_expires_at",
        )
    except Exception as e:
        logger.warning("push_register_rate_limit TTL index creation failed: %s", e)


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
async def start_board_alarm_indexes():
    """#296: indexes for the alarm feed. Separate from the users bootstrap
    so a failure in one cannot block the other."""
    try:
        import board_alarms
        await board_alarms.ensure_indexes(db)
    except Exception as e:
        logger.warning("board_alarms index setup failed: %s", e)


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
