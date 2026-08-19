"""C1 re-check endpoints: the answer path, plus sweeper status and kill switch.

The answer endpoint is PUBLIC and authenticated by device_id, exactly like
POST /api/status. That is deliberate: a trapped person answering from a lock
screen has no session, and adding one would be a way to lose answers.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from apns import send_recheck_prompts as apns_send_rechecks
from auth import require_role, resolve_principal
from deps import ADMIN_TRIGGER_PASSWORD, db, recheck_sweeper
from recheckin import (
    VALID_ANSWERS,
    _manual_candidates,
    manual_recheck_cost,
    record_answer,
    send_manual_rechecks,
)

router = APIRouter()


class RecheckAnswerBody(BaseModel):
    device_id: str = Field(..., min_length=3, max_length=200)
    answer: str = Field(..., description="same | worse | much_worse | better")
    check_id: Optional[str] = None
    # Device tap time. Authoritative — an answer tapped offline and delivered
    # later is recorded at the time it was TAPPED, never the time it arrived.
    answered_at: Optional[str] = None
    battery_pct: Optional[int] = Field(default=None, ge=0, le=100)
    latitude: Optional[float] = None
    longitude: Optional[float] = None


@router.post("/recheck/answer")
async def recheck_answer(body: RecheckAnswerBody):
    if body.answer not in VALID_ANSWERS:
        raise HTTPException(400, f"answer must be one of {', '.join(VALID_ANSWERS)}")
    try:
        return await record_answer(
            db,
            device_id=body.device_id,
            answer=body.answer,
            check_id=body.check_id,
            answered_at=body.answered_at,
            battery_pct=body.battery_pct,
            latitude=body.latitude,
            longitude=body.longitude,
        )
    except KeyError:
        raise HTTPException(404, f"Unknown device_id: {body.device_id}")


class RecheckEnabledBody(BaseModel):
    enabled: bool


class ManualRecheckBody(BaseModel):
    """Operator-initiated ask (C1 phase 2).

    `confirm` defaults to False and that is the whole point: the first call is
    a dry run that returns exactly who would be woken and what it costs, so
    the dashboard can put the cost in front of the operator BEFORE anyone's
    phone lights up. Nothing is sent until confirm=true.
    """
    device_ids: Optional[list[str]] = Field(default=None, max_length=500)
    severity: Optional[str] = Field(default=None, pattern=r"^(green|yellow|red)$")
    confirm: bool = False
    reason: Optional[str] = Field(default=None, max_length=280)


@router.post("/admin/recheck")
async def admin_recheck(body: ManualRecheckBody, request: Request):
    """Preview (confirm=false) or send (confirm=true) an immediate re-check.

    Refusals are not overridable: someone whose current status is not
    `trapped` is never asked (that is the Critical Alerts entitlement
    boundary), and a dark phone is never asked because it cannot answer.
    Both come back in `skipped` with a plain-language reason so the operator
    sees who was left out and why.
    """
    principal = await resolve_principal(
        request, request.headers.get("x-admin-token"), ADMIN_TRIGGER_PASSWORD, db
    )
    require_role(principal, "admin", "operator")
    who = principal.get("email") or principal.get("identity") or "dashboard"

    now = datetime.now(timezone.utc)
    will_ask, skipped = await _manual_candidates(
        db, now, body.device_ids, body.severity,
    )
    cost = manual_recheck_cost(will_ask)

    if not body.confirm:
        return {
            "preview": True,
            "asked": 0,
            "would_ask": cost["will_ask"],
            "cost": cost,
            "skipped": skipped,
            "people": [
                {"device_id": r.get("device_id"),
                 "display_name": r.get("display_name"),
                 "severity": r.get("severity"),
                 "battery_pct": r.get("battery_pct")}
                for r in will_ask
            ],
        }

    result = await send_manual_rechecks(
        db, apns_send_rechecks, initiated_by=who,
        device_ids=body.device_ids, severity=body.severity, now=now,
    )
    # Audited as an operator action, not an anonymous system event: someone
    # chose to wake injured people's phones and the record has to say who.
    await db.recheck_audit.insert_one({
        "at": now.isoformat(),
        "initiated_by": who,
        "reason": body.reason,
        "device_ids": result.get("device_ids") or [],
        "asked": result["asked"],
        "sent": result["sent"],
        "severity_filter": body.severity,
    })
    return {"preview": False, **result, "initiated_by": who}


@router.get("/admin/recheck/status")
async def recheck_status(request: Request):
    """Is the ladder running, and what did the last sweep do?"""
    principal = await resolve_principal(
        request, request.headers.get("x-admin-token"), ADMIN_TRIGGER_PASSWORD, db
    )
    require_role(principal, "admin", "operator")
    # A2 (Batch 7): counts read from the same function every other
    # dashboard surface reads from. Previously this line was a bare
    # count_documents({"status": "trapped"}) which ignored the test-entry
    # filter and included rescued rows whose `status` was still 'trapped'.
    from people_counts import compute_counts
    _c = await compute_counts(db, include_test=False)
    trapped = _c.needs_help
    return {
        "enabled": recheck_sweeper.enabled,
        "task_running": bool(recheck_sweeper.task and not recheck_sweeper.task.done()),
        "last_sweep_at": recheck_sweeper.last_sweep_at.isoformat()
        if recheck_sweeper.last_sweep_at else None,
        "last_result": recheck_sweeper.last_result,
        "trapped_count": trapped,
    }


@router.post("/admin/recheck/enabled")
async def recheck_set_enabled(body: RecheckEnabledBody, request: Request):
    """Kill switch with a UI behind it — no terminal step for the operator
    (operational standard locked 2026-08-06)."""
    principal = await resolve_principal(
        request, request.headers.get("x-admin-token"), ADMIN_TRIGGER_PASSWORD, db
    )
    require_role(principal, "admin")
    recheck_sweeper.enabled = bool(body.enabled)
    return {"ok": True, "enabled": recheck_sweeper.enabled}
