"""Admin endpoints for the EMSC/USGS soak, continuity and preview mode.

Extracted from server.py on 2026-06-18 — behaviour unchanged.

  * /admin/emsc/health      — per-provider poller health, uptime-monitorable
  * /admin/emsc/recent      — ingested events with filters
  * /admin/emsc/config/{cc} — active thresholds for a country
  * /admin/emsc/continuity  — soak coverage_pct, gaps, reset history. THIS is
                              the number to quote when claiming "we have
                              soaked for N days".
  * /admin/emsc/preview/*   — preview-mode config, allowlist, kill switch,
                              candidates and audit trail.

The preview path is the ONLY sender of real notifications today, and it is
never the Critical Alerts path — see emsc/preview.py for the locked
constraints.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from auth import require_role, resolve_principal
from deps import ADMIN_TRIGGER_PASSWORD, db, emsc_poller, iso_utc as _iso

router = APIRouter()
api_router = router   # endpoints below keep their original decorators verbatim

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

