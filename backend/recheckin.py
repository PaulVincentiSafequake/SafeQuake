"""C1 — periodic re-check-in ladder for people who reported being trapped.

Design: memory/recheckin-design.md (agreed with Paul 2026-08-17).

Nothing used to re-check anyone. A person who reported trapped kept that
status forever and the dashboard's `trapped_since` just counted up, so
"reported trapped three hours ago and has gone quiet" looked exactly like
"reported trapped three hours ago and is answering every check". Those are
completely different situations for whoever decides who is reached first.

Shape (all of it server-driven, deliberately):
  * Battery is the constraint that decides everything, so the interval
    widens: 15 min for the first hour, 30 min to 4 h, hourly to 12 h, then
    every 3 h. ~26 wake-ups in the first 12 hours instead of 48.
  * Below 20% reported battery every interval doubles; below 10% it triples,
    and the prompt says so — otherwise the app looks like it has forgotten
    them.
  * One tap to answer: SAME · WORSE · MUCH WORSE · BETTER. WORSE moves one
    band; MUCH WORSE goes straight to red whatever they were before. BETTER
    is recorded but NEVER auto-reduces severity: we escalate on the person's
    word, we de-escalate only on a human decision.
  * Silence is information and its two kinds must look different —
    `silent_alive` (not answering, phone still reporting) vs `dark` (nothing
    at all for over 45 minutes). Neither reduces priority.
  * Stop condition is a STATE, not a clock: we keep asking at the widest
    interval for as long as the phone is alive, and stop when it goes dark
    because asking a dead phone achieves nothing.

Hard invariant, and the thing that keeps our Critical Alerts entitlement
justification true: `_eligible_devices` refuses any device whose CURRENT
status is not `trapped`. There is no other way into this path.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

log = logging.getLogger(__name__)

SWEEP_INTERVAL_SEC = 60

# (age_hours_upper_bound, interval_minutes) — the last row is open-ended.
DEFAULT_LADDER = [
    (1, 15),
    (4, 30),
    (12, 60),
    (None, 180),
]

# Reported battery below these levels widens the interval by the multiplier.
LOW_BATTERY_PCT = 20
LOW_BATTERY_MULTIPLIER = 2
CRITICAL_BATTERY_PCT = 10
CRITICAL_BATTERY_MULTIPLIER = 3

# Nothing at all from the device for this long → `dark`. Twice the longest
# expected ping, so a single missed wake-up can't be mistaken for a dead phone.
DARK_AFTER_MINUTES = 45

VALID_ANSWERS = ("same", "worse", "much_worse", "better")
SEVERITY_LADDER = ("green", "yellow", "red")


def _parse(ts) -> Optional[datetime]:
    """device_status stores timestamps as ISO strings; status_events too."""
    if not ts:
        return None
    if isinstance(ts, datetime):
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    try:
        d = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def interval_minutes(
    trapped_for: timedelta,
    battery_pct: Optional[int],
    ladder=None,
) -> tuple[int, bool]:
    """Minutes until the next check, and whether battery widened it.

    Returned separately because the prompt must SAY when we have slowed down
    — silence that the user can't explain reads as the app having forgotten
    them, which is the opposite of the reassurance this feature exists for.
    """
    hours = trapped_for.total_seconds() / 3600.0
    base = (ladder or DEFAULT_LADDER)[-1][1]
    for upper, minutes in (ladder or DEFAULT_LADDER):
        if upper is None or hours < upper:
            base = minutes
            break
    saving = False
    if battery_pct is not None:
        if battery_pct <= CRITICAL_BATTERY_PCT:
            base *= CRITICAL_BATTERY_MULTIPLIER
            saving = True
        elif battery_pct <= LOW_BATTERY_PCT:
            base *= LOW_BATTERY_MULTIPLIER
            saving = True
    return int(base), saving


def escalate(severity: Optional[str], answer: str) -> Optional[str]:
    """Severity after an answer. One-way: nothing here ever steps down.

    Not forced stepwise — green straight to red must be possible (Paul,
    2026-08-17), which is what MUCH WORSE is for. A single WORSE button
    cannot express how much worse.
    """
    if answer == "much_worse":
        return "red"
    if answer == "worse":
        if severity not in SEVERITY_LADDER:
            return "red"          # unknown band + deteriorating → assume worst
        i = SEVERITY_LADDER.index(severity)
        return SEVERITY_LADDER[min(i + 1, len(SEVERITY_LADDER) - 1)]
    # "same" and "better" never change the band. BETTER is self-reported and
    # easy to be wrong about (adrenaline, shock) — an operator re-triages.
    return severity


def silence_state(row: dict, now: Optional[datetime] = None) -> Optional[str]:
    """`dark` | `silent_alive` | None, derived from the last contact.

    "We don't know" must never look like "we know they're fine", so this is
    surfaced on the dashboard rather than folded into the status.
    """
    now = now or datetime.now(timezone.utc)
    last = _parse(row.get("updated_at"))
    if last is None:
        return None
    silent_for = now - last
    if silent_for > timedelta(minutes=DARK_AFTER_MINUTES):
        return "dark"
    rc = row.get("recheck") or {}
    if int(rc.get("consecutive_missed") or 0) >= 2:
        return "silent_alive"
    return None


def prompt_text(trapped_for: timedelta, battery_saving: bool) -> tuple[str, str]:
    """Title and body. Plain language, one question, no jargon."""
    title = "Are you still OK?"
    body = "Has anything changed? Tap to answer — it takes one tap."
    if battery_saving:
        body += " We'll check less often to save your battery."
    return title, body


async def _eligible_devices(db, now: datetime) -> list[dict]:
    """Everyone whose next check is due AND who is CURRENTLY trapped.

    The status re-check is the enforcement point for the Critical Alerts
    entitlement justification — see apns._build_recheck_payload. A device
    that has since reported safe, or been marked rescued, is dropped here
    even if a check was already scheduled for it.
    """
    rows = await db.device_status.find(
        {"status": "trapped"},
        {"_id": 0, "device_id": 1, "status": 1, "severity": 1, "battery_pct": 1,
         "updated_at": 1, "trapped_since": 1, "recheck": 1, "synthetic": 1,
         "is_test": 1, "created_at": 1},
    ).to_list(2000)

    due = []
    for r in rows:
        if r.get("status") != "trapped":
            continue                      # belt and braces; query already filters
        if silence_state(r, now) == "dark":
            continue                      # asking a dead phone achieves nothing
        rc = r.get("recheck") or {}
        next_at = _parse(rc.get("next_check_at"))
        if next_at is not None and next_at > now:
            continue
        due.append(r)
    return due


def _trapped_since(row: dict, now: datetime) -> datetime:
    return (
        _parse(row.get("trapped_since"))
        or _parse(row.get("created_at"))
        or now
    )


async def send_due_rechecks(db, apns_send_rechecks, now: Optional[datetime] = None) -> dict:
    """One sweep: prompt everyone due, log what was sent and what was missed."""
    now = now or datetime.now(timezone.utc)
    due = await _eligible_devices(db, now)
    if not due:
        return {"due": 0, "sent": 0}
    result = await _dispatch_rechecks(db, apns_send_rechecks, due, now)
    return {"due": len(due), "sent": result["sent"]}


async def _dispatch_rechecks(
    db,
    apns_send_rechecks,
    due: list[dict],
    now: datetime,
    initiated_by: Optional[str] = None,
) -> dict:
    """Send one prompt per device in `due` and write the ledger rows.

    Shared by the automatic sweep and the operator-initiated ask (C1 phase 2)
    so there is exactly one send path: an operator-triggered check must obey
    the same invariants, write the same rows and update the same schedule as
    an automatic one, or the two drift and the record stops being trustworthy.
    `initiated_by` is stamped on the ledger rows when a human asked.
    """
    ids = [r["device_id"] for r in due]
    tokens = {
        d["user_id"]: d.get("device_token")
        for d in await db.push_devices.find(
            {
                "user_id": {"$in": ids}, "platform": "ios",
                "dead_token": {"$ne": True},  # #262 follow-up
            },
            {"_id": 0, "user_id": 1, "device_token": 1},
        ).to_list(2000)
    }

    targets, meta = [], {}
    for r in due:
        did = r["device_id"]
        token = tokens.get(did)
        rc = r.get("recheck") or {}
        check_id = uuid.uuid4().hex
        mins, saving = interval_minutes(
            now - _trapped_since(r, now), r.get("battery_pct"),
        )
        meta[check_id] = {"row": r, "minutes": mins, "saving": saving}

        # An unanswered previous check is a POSITIVE fact in the record —
        # "we asked and heard nothing" must not look like an absence of data.
        pending = rc.get("pending_check_id")
        if pending:
            await db.status_events.insert_one({
                "device_id": did,
                # Batch 7 C6: snapshot display_name onto the row so the
                # activity feed can render "🔁 RE-CHECK · NAME · CODE"
                # like every other row. Without this, RE-CHECK rows
                # showed the code alone (Paul, 2026-08-19).
                "display_name": r.get("display_name"),
                "kind": "recheck_missed",
                "check_id": pending,
                "status": r.get("status"),
                "severity": r.get("severity"),
                "battery_pct": r.get("battery_pct"),
                "latitude": r.get("latitude"),
                "longitude": r.get("longitude"),
                "at": now.isoformat(),
                "recorded_at": now.isoformat(),
            })

        if token:
            # #207 (Batch 7): pass `consecutive_missed` alongside the
            # target so the payload builder can decide whether this
            # specific person's check should escalate to Critical Alert
            # (only after 3 unanswered in this same trapped incident;
            # a fresh incident resets the counter). ONE escalation per
            # person per incident: `critical_escalated` is a sticky
            # flag on the device row that suppresses further
            # escalations for the remainder of this trapped run. The
            # payload builder gates on the `escalate` boolean below —
            # `consecutive_missed` alone would re-escalate on every
            # sweep once the count crossed 3.
            missed = int(rc.get("consecutive_missed") or 0)
            if pending:
                # A pending check that has not been answered counts too.
                missed += 1
            already_escalated = bool(rc.get("critical_escalated"))
            escalate = (missed >= 3) and (not already_escalated)
            targets.append({"user_id": did, "device_token": token,
                            "check_id": check_id,
                            "consecutive_missed": missed,
                            "escalate": escalate})
            meta[check_id]["consecutive_missed"] = missed
            meta[check_id]["escalate"] = escalate
        else:
            meta[check_id]["no_token"] = True

    sent = 0
    if targets:
        # Group by identical body so we don't build a different prompt per
        # device unnecessarily: battery_saving is the only variable.
        for saving in (False, True):
            group = [t for t in targets
                     if meta[t["check_id"]]["saving"] is saving]
            if not group:
                continue
            sample = meta[group[0]["check_id"]]
            title, body = prompt_text(
                now - _trapped_since(sample["row"], now), saving,
            )
            try:
                result = await apns_send_rechecks(
                    db=db,
                    devices=group,
                    title=title,
                    body=body,
                    idempotency_key=f"recheck-{now.strftime('%Y%m%dT%H%M%S')}",
                    battery_saving=saving,
                )
            except Exception as e:                       # never kill the sweep
                log.warning("recheck send raised: %s", e)
                result = {"events": [{"user_id": t["user_id"],
                                      "check_id": t["check_id"],
                                      "delivered": False, "error": str(e)}
                                     for t in group]}
            for ev in (result.get("events") or []):
                if ev.get("delivered"):
                    sent += 1
                await db.status_events.insert_one({
                    "device_id": ev.get("user_id"),
                    # Batch 7 C6: snapshot the display_name (from the
                    # meta lookup captured at check-formation time) so
                    # the activity-feed row can render name+code, not
                    # just the code. See recheck_missed above.
                    "display_name": (meta.get(ev.get("check_id"), {}).get("row") or {}).get("display_name"),
                    "kind": "recheck_sent",
                    "check_id": ev.get("check_id"),
                    "delivered": bool(ev.get("delivered")),
                    "error": ev.get("error"),
                    "initiated_by": initiated_by,
                    "manual": bool(initiated_by),
                    "at": now.isoformat(),
                    "recorded_at": now.isoformat(),
                })

    for check_id, m in meta.items():
        did = m["row"]["device_id"]
        rc = m["row"].get("recheck") or {}
        missed = int(rc.get("consecutive_missed") or 0)
        if rc.get("pending_check_id"):
            missed += 1
        # #207 (Batch 7): remember whether this incident has ALREADY
        # been escalated to Critical Alert on this device — one
        # escalation per person per trapped run, never twice. A new
        # incident (safe / rescued cleared and trapped again later)
        # resets this via `record_answer` and the status-flip handlers.
        already_escalated = bool(rc.get("critical_escalated"))
        will_escalate = bool(m.get("escalate"))
        await db.device_status.update_one(
            {"device_id": did},
            {"$set": {
                "recheck": {
                    "pending_check_id": None if m.get("no_token") else check_id,
                    "last_check_at": now.isoformat(),
                    "next_check_at": (now + timedelta(minutes=m["minutes"])).isoformat(),
                    "interval_minutes": m["minutes"],
                    "battery_saving": m["saving"],
                    "consecutive_missed": missed,
                    "checks_sent": int(rc.get("checks_sent") or 0) + 1,
                    # Sticky once true. Cleared only when the person
                    # tells us they're safe / is marked rescued —
                    # both handled by `record_answer` and the
                    # status-flip handlers on POST /status.
                    "critical_escalated": already_escalated or will_escalate,
                    "critical_escalated_at": (
                        now.isoformat() if will_escalate
                        else rc.get("critical_escalated_at")
                    ),
                },
            }},
        )

    return {"due": len(due), "sent": sent,
            "device_ids": [m["row"]["device_id"] for m in meta.values()]}


async def _manual_candidates(
    db,
    now: datetime,
    device_ids: Optional[list[str]] = None,
    severity: Optional[str] = None,
) -> tuple[list[dict], list[dict]]:
    """(will_ask, skipped) for an operator-initiated ask.

    Same two refusals as the automatic sweep — not currently trapped, or
    dark — because they are the reasons the feature is defensible: the
    Critical Alerts entitlement rests on only ever waking someone who has
    told us they need help, and a dead phone cannot answer. An operator
    cannot override either, deliberately: there is no flag for it.
    """
    q: dict = {}
    if device_ids:
        q["device_id"] = {"$in": list(device_ids)}
    rows = await db.device_status.find(
        q,
        {"_id": 0, "device_id": 1, "status": 1, "severity": 1, "battery_pct": 1,
         "updated_at": 1, "trapped_since": 1, "recheck": 1, "created_at": 1,
         "display_name": 1},
    ).to_list(2000)

    will_ask, skipped = [], []
    named = bool(device_ids)
    for r in rows:
        if r.get("status") != "trapped":
            # Only worth reporting when the operator NAMED this person. In a
            # broadcast ask, every safe phone in the database is "skipped",
            # and a 400-row skip list buries the two entries that matter.
            if named:
                skipped.append({"device_id": r.get("device_id"),
                                "reason": "not currently marked as needing help"})
            continue
        if severity and (r.get("severity") or "") != severity:
            continue
        if silence_state(r, now) == "dark":
            skipped.append({"device_id": r.get("device_id"),
                            "reason": "phone has gone dark — it cannot answer"})
            continue
        will_ask.append(r)
    return will_ask, skipped


def manual_recheck_cost(will_ask: list[dict]) -> dict:
    """What asking now costs, in facts rather than invented percentages.

    An operator pressing this wakes injured people's phones. The confirm step
    has to state the cost, and it must not state a number we cannot stand
    behind — so it counts the phones that are already low and says what the
    ask does to them, instead of guessing at a battery percentage.
    """
    low = [r for r in will_ask
           if isinstance(r.get("battery_pct"), (int, float))
           and r["battery_pct"] <= LOW_BATTERY_PCT]
    critical = [r for r in will_ask
                if isinstance(r.get("battery_pct"), (int, float))
                and r["battery_pct"] <= CRITICAL_BATTERY_PCT]
    n = len(will_ask)
    lines = []
    if n == 0:
        lines.append("There is no one to ask right now.")
    else:
        lines.append(
            ("This will wake 1 phone belonging to someone who told us they need help."
             if n == 1 else
             f"This will wake {n} phones belonging to people who told us they need help.")
        )
    if critical:
        lines.append(
            ("1 of them is on less than 10% battery."
             if len(critical) == 1 else
             f"{len(critical)} of them are on less than 10% battery.")
        )
    if low and len(low) > len(critical):
        lines.append(
            f"{len(low)} of them are on 20% battery or less. "
            "Their automatic checks are already spaced out to save power."
        )
    if n:
        lines.append("Their next automatic check is rescheduled from now, so asking "
                     "does not add an extra one on top.")
    return {
        "will_ask": n,
        "low_battery": len(low),
        "critical_battery": len(critical),
        "lines": lines,
    }


async def send_manual_rechecks(
    db,
    apns_send_rechecks,
    initiated_by: str,
    device_ids: Optional[list[str]] = None,
    severity: Optional[str] = None,
    now: Optional[datetime] = None,
) -> dict:
    """C1 phase 2: ask now, because a human decided to (audited)."""
    now = now or datetime.now(timezone.utc)
    will_ask, skipped = await _manual_candidates(db, now, device_ids, severity)
    cost = manual_recheck_cost(will_ask)
    if not will_ask:
        return {"asked": 0, "sent": 0, "skipped": skipped, "cost": cost}
    result = await _dispatch_rechecks(
        db, apns_send_rechecks, will_ask, now, initiated_by=initiated_by,
    )
    return {
        "asked": len(will_ask),
        "sent": result["sent"],
        "device_ids": result.get("device_ids") or [],
        "skipped": skipped,
        "cost": cost,
    }


async def record_answer(
    db,
    device_id: str,
    answer: str,
    check_id: Optional[str] = None,
    answered_at: Optional[str] = None,
    battery_pct: Optional[int] = None,
    latitude: Optional[float] = None,
    longitude: Optional[float] = None,
) -> dict:
    """Record one answer and apply its consequence.

    `answered_at` is the DEVICE tap time and is authoritative: an answer
    tapped offline and delivered when signal returns is real information with
    a real timestamp, and that is the value every human-facing surface reads
    (Paul, 2026-08-17). `received_at` is kept alongside it, never substituted.
    A device clock can be wrong — we keep both and flag the row rather than
    quietly rewriting a timestamp on a rescue record.
    """
    if answer not in VALID_ANSWERS:
        raise ValueError(f"answer must be one of {VALID_ANSWERS}")

    now = datetime.now(timezone.utc)
    tapped = _parse(answered_at) or now
    clock_suspect = tapped > now + timedelta(minutes=2) or (now - tapped) > timedelta(days=1)

    row = await db.device_status.find_one({"device_id": device_id}, {"_id": 0})
    if not row:
        raise KeyError(device_id)

    prior_severity = row.get("severity")
    new_severity = escalate(prior_severity, answer)
    deteriorating = answer in ("worse", "much_worse")

    event = {
        "device_id": device_id,
        "kind": "recheck_answered",
        "check_id": check_id,
        "answer": answer,
        "status": row.get("status"),
        "severity": new_severity,
        "prior_severity": prior_severity,
        "deteriorating": deteriorating,
        "reports_improving": answer == "better",
        "battery_pct": battery_pct if battery_pct is not None else row.get("battery_pct"),
        "latitude": latitude if latitude is not None else row.get("latitude"),
        "longitude": longitude if longitude is not None else row.get("longitude"),
        # answered_at is what the history modal, the audit CSV and the audit
        # PDF render, and what the dashboard sorts and ages by.
        "answered_at": tapped.isoformat(),
        "received_at": now.isoformat(),
        "at": tapped.isoformat(),
        "recorded_at": now.isoformat(),
        "queued_offline": (now - tapped) > timedelta(minutes=2),
        "device_clock_suspect": clock_suspect,
    }
    await db.status_events.insert_one(dict(event))

    rc = row.get("recheck") or {}
    mins, saving = interval_minutes(
        now - _trapped_since(row, now),
        battery_pct if battery_pct is not None else row.get("battery_pct"),
    )
    set_fields = {
        "updated_at": now.isoformat(),
        "recheck": {
            **rc,
            "pending_check_id": None,
            "consecutive_missed": 0,
            "last_answer": answer,
            "last_answer_at": tapped.isoformat(),
            "next_check_at": (now + timedelta(minutes=mins)).isoformat(),
            "interval_minutes": mins,
            "battery_saving": saving,
            "answers": int(rc.get("answers") or 0) + 1,
        },
    }
    if deteriorating:
        set_fields["severity"] = new_severity
        set_fields["deteriorating"] = True
    if answer == "better":
        # Recorded, shown as "reports improving", NOT acted on automatically.
        set_fields["reports_improving"] = True
    if battery_pct is not None:
        set_fields["battery_pct"] = battery_pct
    if latitude is not None and longitude is not None:
        set_fields["latitude"] = latitude
        set_fields["longitude"] = longitude

    await db.device_status.update_one({"device_id": device_id}, {"$set": set_fields})
    return {
        "ok": True,
        "device_id": device_id,
        "answer": answer,
        "severity": new_severity,
        "prior_severity": prior_severity,
        "deteriorating": deteriorating,
        "answered_at": tapped.isoformat(),
        "received_at": now.isoformat(),
        "next_check_at": set_fields["recheck"]["next_check_at"],
    }


class RecheckSweeper:
    """Owns the due-check loop. Same shape as the testimonies sweeper: its own
    asyncio task, started and stopped by server.py."""

    def __init__(self, db, apns_send_rechecks):
        self.db = db
        self.apns_send_rechecks = apns_send_rechecks
        self.task: Optional[asyncio.Task] = None
        self.last_sweep_at: Optional[datetime] = None
        self.last_result: Optional[dict] = None
        self.enabled = True

    async def start(self) -> None:
        if self.task and not self.task.done():
            return
        self.task = asyncio.create_task(self._run(), name="recheck_sweeper")
        log.info("Re-check sweeper started (%ss interval)", SWEEP_INTERVAL_SEC)

    async def stop(self) -> None:
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except (asyncio.CancelledError, Exception):
                pass

    async def _run(self) -> None:
        await asyncio.sleep(20)
        while True:
            try:
                if self.enabled:
                    self.last_result = await send_due_rechecks(
                        self.db, self.apns_send_rechecks,
                    )
                    self.last_sweep_at = datetime.now(timezone.utc)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.exception("recheck sweep failed: %s", e)
            await asyncio.sleep(SWEEP_INTERVAL_SEC)
