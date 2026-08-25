"""#296 (2026-08-23 — Paul) — the board's annunciator.

Paul's instruction, and the reason this is not invented from scratch:

    "Do not invent this. There is a defined sequence used in control rooms
     for decades, standardised as ISA-18.1, and it exists because it works
     under stress. Follow it."

The sequence, and where each part lives:

  1. An alarm arrives. The visual FLASHES and a short sound plays. The
     sound's only job is to pull the operator's eyes to the screen — it
     carries no information. (Sound: `window.qgSound` in the dashboard.
     Flashing: the alarm strip.)
  2. The operator presses Acknowledge. The sound stops. The visual stops
     flashing but STAYS highlighted. (`ack` below.)
  3. The highlight remains until the underlying situation is actually
     resolved — not until the operator has looked at it. (`resolve_for_device`,
     called only when someone is rescued or deliberately taken off the
     board, or when a phone that had gone quiet reports again.)

What is an alarm here — an alarm is defined as something REQUIRING the
operator to act:

  · NEEDS_HELP    a new person reporting they need help
  · WORSE         an existing person getting worse (minor → serious →
                  immediate, or discovering they cannot get out)
  · GONE_QUIET    a person who reported needing help going quiet

What is information and must never flash or sound (so it is deliberately
absent from this file): someone reporting they are safe, a battery level
changing, routine list housekeeping, new phones registering, tremor
notices.

Rules this module enforces so no caller can break them:

  · Every alarm carries the ACTION expected, not just a state change.
  · Nothing auto-acknowledges. Nothing auto-clears. Nothing times out.
  · Acknowledging is recorded with who and when, and it is readable back
    in the audit feed — it will be read in an inquiry.
  · Acknowledging NEVER resolves. Only a rescue, a deliberate move off
    the board, or a phone speaking again resolves.
  · One open alarm of a kind per person. A second identical alarm cannot
    be raised while the first is unresolved, so nobody can be shouted
    about twice for the same fact.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

NEEDS_HELP = "needs_help"
WORSE = "worse"
GONE_QUIET = "gone_quiet"

# Word + shape, never colour alone. The dashboard draws the shape from
# this field so the strip is readable with the sound off, in black and
# white, and by an operator who cannot tell red from green.
WORDS: Dict[str, str] = {
    NEEDS_HELP: "Needs help",
    WORSE: "Getting worse",
    GONE_QUIET: "Gone quiet",
}
SHAPES: Dict[str, str] = {
    NEEDS_HELP: "triangle",
    WORSE: "diamond",
    GONE_QUIET: "square",
}

# The wire values are the colours the phone sends (green / yellow / red);
# the words are the triage categories an operator says out loud. Triage
# category names stay capitalised — the one agreed exception to sentence
# case, because they are spoken as categories over a radio. The mapping is
# the same one the dashboard uses (index.html: red → IMMEDIATE).
SEVERITY_RANK: Dict[str, int] = {"green": 1, "yellow": 2, "red": 3}
SEVERITY_WORD: Dict[str, str] = {
    "green": "MINOR", "yellow": "SERIOUS", "red": "IMMEDIATE",
}
# Mobility on the wire is mobile | trapped.
CANNOT_MOVE = "trapped"

# Published practice: roughly six alarms an hour is manageable, about ten
# in ten minutes is the human limit. Past that operators stop reading the
# board at all, so the board says out loud that it is summarising.
FLOOD_WINDOW_MINUTES = 10
FLOOD_LIMIT = 10


def _now(now: Optional[datetime] = None) -> datetime:
    return now or datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


async def ensure_indexes(db) -> None:
    try:
        await db.board_alarms.create_index([("resolved_at", 1), ("created_at", -1)])
        await db.board_alarms.create_index([("device_id", 1), ("kind", 1)])
        await db.board_alarms.create_index("id", unique=True)
    except Exception as e:  # index creation must never block a request
        logging.warning(f"board_alarms index setup: {e}")


def _who(row: Dict[str, Any], short_code: Optional[str] = None) -> str:
    """Name and rescue code together — never the code alone (#298)."""
    code = short_code or row.get("short_code") or (row.get("device_id") or "")[-5:].upper()
    name = (row.get("display_name") or "").strip()
    return f"{name} · {code}" if name else str(code)


def _help_action(row: Dict[str, Any]) -> str:
    """What the operator is expected to DO. Not "status changed"."""
    sev = str(row.get("severity") or "").lower()
    stuck = bool(row.get("needs_extraction")) or row.get("egress") == "cannot_exit"
    cannot_move = str(row.get("mobility") or "") == CANNOT_MOVE
    # #297: a team must never be sent to walking wounded. In real triage
    # they are the lowest rescue priority precisely because they can move
    # themselves, so the alarm says what to do about them instead of
    # implying a rescue task.
    if sev == "green" and not stuck and not cannot_move:
        return (
            "MINOR, and they can move themselves. Not a rescue task — "
            "they are on the walking wounded list."
        )
    parts: List[str] = []
    if sev in SEVERITY_WORD:
        parts.append(SEVERITY_WORD[sev])
    if stuck:
        parts.append("cannot get out")
    if cannot_move:
        parts.append("cannot move")
    tail = ", ".join(parts)
    return "Send a team" + (f" — {tail}." if tail else ".")


async def _open_alarm(db, device_id: str, kind: str) -> Optional[dict]:
    return await db.board_alarms.find_one(
        {"device_id": device_id, "kind": kind, "resolved_at": None},
        {"_id": 0},
    )


async def raise_alarm(
    db,
    *,
    kind: str,
    device_id: str,
    row: Dict[str, Any],
    headline: str,
    action: str,
    now: Optional[datetime] = None,
) -> Optional[dict]:
    """Create one alarm, unless an unresolved one of the same kind already
    exists for this person. Returns the alarm, or None if it was a repeat."""
    n = _now(now)
    if await _open_alarm(db, device_id, kind):
        return None
    doc = {
        "id": str(uuid.uuid4()),
        "kind": kind,
        "device_id": device_id,
        "short_code": row.get("short_code") or (device_id or "")[-5:].upper(),
        "display_name": row.get("display_name"),
        "severity": row.get("severity"),
        "headline": headline,
        "action": action,
        "word": WORDS[kind],
        "shape": SHAPES[kind],
        "created_at": _iso(n),
        # Grouping bucket: same kind, same minute. Thirty people reporting
        # in one minute is one decision for the operator, not thirty.
        "group_key": f"{kind}:{n.strftime('%Y-%m-%dT%H:%M')}",
        "ack_by": None,
        "ack_at": None,
        "resolved_at": None,
        "resolved_reason": None,
    }
    try:
        await db.board_alarms.insert_one(dict(doc))
    except Exception as e:
        logging.warning(f"board_alarms insert failed: {e}")
        return None
    return doc


async def on_status_change(
    db,
    prior: Optional[Dict[str, Any]],
    doc: Dict[str, Any],
    now: Optional[datetime] = None,
) -> List[dict]:
    """Called from POST /api/status, with the row as it was and as it now
    is. The only place that decides whether a report is an alarm."""
    raised: List[dict] = []
    device_id = doc.get("device_id") or ""
    if not device_id:
        return raised
    # Test entries never sound an alarm. They are visible on the board when
    # an operator asks for them, but an alarm means "act now", and nobody
    # should be pulled out of a real incident by a rehearsal.
    from deps import is_test_device
    if is_test_device({**doc, **(prior or {})}):
        return raised
    prior = prior or {}
    was = str(prior.get("status") or "")
    now_status = str(doc.get("status") or "")

    if now_status == "trapped" and was != "trapped":
        a = await raise_alarm(
            db, kind=NEEDS_HELP, device_id=device_id, row=doc,
            headline=f"{_who(doc)} needs help",
            action=_help_action(doc), now=now,
        )
        if a:
            raised.append(a)
    elif now_status == "trapped" and was == "trapped":
        old_rank = SEVERITY_RANK.get(str(prior.get("severity") or "").lower(), 0)
        new_rank = SEVERITY_RANK.get(str(doc.get("severity") or "").lower(), 0)
        newly_stuck = bool(doc.get("needs_extraction")) and not bool(
            prior.get("needs_extraction")
        )
        if new_rank > old_rank or newly_stuck:
            if new_rank > old_rank:
                head = (
                    f"{_who(doc)} is worse — now "
                    f"{SEVERITY_WORD.get(str(doc.get('severity') or '').lower(), 'worse')}"
                )
            else:
                head = f"{_who(doc)} now cannot get out"
            a = await raise_alarm(
                db, kind=WORSE, device_id=device_id, row=doc,
                headline=head, action=_help_action(doc), now=now,
            )
            if a:
                raised.append(a)

    # A phone that speaks again is no longer quiet. That is the situation
    # being resolved, which is the only thing allowed to clear an alarm.
    await resolve_for_device(
        db, device_id, kinds=[GONE_QUIET],
        reason="Their phone reported again.", now=now,
    )
    # #289 (2026-08-24 — Paul, live test): an acknowledged "IMMEDIATE,
    # cannot move" alarm sat in the strip while the same person's card had
    # since moved to MINOR, twice. The alarm is RIGHT to stay — only a
    # human resolves one, and a self-reported improvement must never
    # quietly clear a report of a serious injury (adrenaline and shock
    # make people wrong about that). But the board must not contradict
    # itself either. So the alarm now carries what has happened SINCE it
    # was raised, and the operator decides with both facts in front of
    # them.
    await _stamp_latest_report(db, device_id, doc, now=now)
    return raised


async def _stamp_latest_report(
    db, device_id: str, doc: Dict[str, Any], now: Optional[datetime] = None,
) -> None:
    """Record the person's newest report on their still-open alarms."""
    n = _now(now)
    status = str(doc.get("status") or "")
    sev = str(doc.get("severity") or "").lower()
    if status == "safe":
        words = "reported they are safe"
    elif status == "rescued":
        words = "was marked rescued"
    elif status == "trapped":
        bits = [SEVERITY_WORD.get(sev, "severity not given")]
        if doc.get("needs_extraction") or doc.get("egress") == "cannot_exit":
            bits.append("cannot get out")
        elif doc.get("egress") == "can_exit":
            bits.append("can get out")
        if str(doc.get("mobility") or "") == CANNOT_MOVE:
            bits.append("cannot move")
        words = "reported " + ", ".join(bits)
    else:
        words = f"reported {status or 'something we do not have a word for'}"
    try:
        await db.board_alarms.update_many(
            {"device_id": device_id, "resolved_at": None,
             "kind": {"$in": [NEEDS_HELP, WORSE]}},
            {"$set": {"since_report": {
                "words": words,
                "at": _iso(n),
                "status": status or None,
                "severity": doc.get("severity"),
            }}},
        )
    except Exception as e:
        logging.warning(f"board_alarms since-stamp failed: {e}")


async def sweep_silence(db, rows: List[Dict[str, Any]], now: Optional[datetime] = None) -> List[Dict[str, Any]]:
    """Raise GONE_QUIET for anyone who asked for help and has stopped
    answering. Called while the board is being read, because silence is
    measured by the clock and nothing arrives to announce it.

    Returns the people who are quiet but too old to sound (#306) — quiet
    for a day does not mean resolved, and Paul's rule is that they must
    never become wallpaper: "no sound, but they must never be invisible."

    `rows` are working-board rows that already carry `record_state` and
    `ever_needed_help` from record_state.classify — one classifier, so the
    alarm and the card can never disagree about who is quiet.
    """
    from record_state import DARK, NO_ANSWER

    n = _now(now)
    long_quiet: List[Dict[str, Any]] = []
    for r in rows:
        try:
            if not r.get("ever_needed_help"):
                continue
            if r.get("is_test") or r.get("synthetic"):
                continue
            state = (r.get("record_state") or {}).get("state")
            device_id = r.get("device_id") or ""
            if not device_id:
                continue
            # An alarm means "act now". A record that has been quiet for
            # days is history, not news, and a wall of them on the first
            # load of the day would train an operator to ignore the strip —
            # the exact failure the flood rules exist to prevent. They stay
            # on the working board, with their own card saying they are
            # quiet; they simply do not sound.
            fresh = False
            quiet_hours = None
            try:
                last = r.get("updated_at")
                if last:
                    ts = datetime.fromisoformat(str(last).replace("Z", "+00:00"))
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                    quiet_hours = (n - ts).total_seconds() / 3600.0
                    fresh = quiet_hours <= 24
            except Exception:
                fresh = False
            if state in (DARK, NO_ANSWER) and not fresh:
                # #306: no sound, never invisible.
                long_quiet.append({
                    "device_id": device_id,
                    "who": _who(r),
                    "hours": int(quiet_hours) if quiet_hours is not None else None,
                })
            if state in (DARK, NO_ANSWER) and fresh:
                label = (r.get("record_state") or {}).get("label") or "has gone quiet"
                await raise_alarm(
                    db, kind=GONE_QUIET, device_id=device_id, row=r,
                    headline=f"{_who(r)} has gone quiet",
                    action=(
                        f"{label}. They asked for help and have stopped answering. "
                        "Treat as still needing a team until someone reaches them."
                    ),
                    now=now,
                )
            elif state not in (DARK, NO_ANSWER):
                # Answering again. That is the situation resolving, which is
                # the only thing allowed to clear an alarm. A record that is
                # still quiet but too old to sound keeps its alarm exactly
                # as it is — nothing clears on the clock.
                await resolve_for_device(
                    db, device_id, kinds=[GONE_QUIET],
                    reason="Their phone is answering again.", now=now,
                )
        except Exception as e:  # one bad row must never stop the sweep
            logging.warning(f"board_alarms sweep row failed: {e}")

    # Stored so the strip can print it on every read without re-deriving it.
    try:
        await db.board_meta.update_one(
            {"_id": "long_quiet"},
            {"$set": {"people": long_quiet, "generated_at": _iso(n)}},
            upsert=True,
        )
    except Exception as e:
        logging.warning(f"board_alarms long-quiet write failed: {e}")
    return long_quiet


async def resolve_for_device(
    db,
    device_id: str,
    *,
    kinds: Optional[List[str]] = None,
    reason: str,
    now: Optional[datetime] = None,
) -> int:
    """Resolve open alarms for one person. This is the ONLY route out of
    an alarm, and it is only ever called because the real situation
    changed: rescued, deliberately taken off the board, or a quiet phone
    speaking again."""
    q: Dict[str, Any] = {"device_id": device_id, "resolved_at": None}
    if kinds:
        q["kind"] = {"$in": kinds}
    try:
        res = await db.board_alarms.update_many(
            q, {"$set": {"resolved_at": _iso(_now(now)), "resolved_reason": reason}},
        )
        return res.modified_count
    except Exception as e:
        logging.warning(f"board_alarms resolve failed: {e}")
        return 0


async def ack(db, ids: List[str], who: str, now: Optional[datetime] = None) -> int:
    """Acknowledge. Stops the sound and the flashing, records who and when,
    and changes nothing else — the alarm stays on the board, highlighted,
    until the person is actually rescued or moved off it."""
    if not ids:
        return 0
    try:
        res = await db.board_alarms.update_many(
            {"id": {"$in": ids}, "ack_at": None},
            {"$set": {"ack_by": who, "ack_at": _iso(_now(now))}},
        )
        return res.modified_count
    except Exception as e:
        logging.warning(f"board_alarms ack failed: {e}")
        return 0


async def list_open(db, now: Optional[datetime] = None) -> Dict[str, Any]:
    """Everything the strip needs, grouped, with the unacknowledged count
    the board must always show."""
    n = _now(now)
    try:
        rows = await db.board_alarms.find(
            {"resolved_at": None}, {"_id": 0},
        ).sort("created_at", -1).to_list(500)
    except Exception as e:
        logging.warning(f"board_alarms read failed: {e}")
        rows = []

    unacked = [r for r in rows if not r.get("ack_at")]
    # #306 (Paul, 2026-08-23): "quiet for a day does not mean resolved...
    # no sound, but they must never be invisible." A permanent line, always
    # in view, whether or not anything is sounding.
    long_quiet: List[Dict[str, Any]] = []
    try:
        meta = await db.board_meta.find_one({"_id": "long_quiet"}, {"_id": 0})
        long_quiet = (meta or {}).get("people") or []
    except Exception as e:
        logging.warning(f"board_alarms long-quiet read failed: {e}")
    window_start = _iso(n - timedelta(minutes=FLOOD_WINDOW_MINUTES))
    recent = [r for r in rows if str(r.get("created_at") or "") >= window_start]
    flood = len(recent) > FLOOD_LIMIT

    # Group by kind + minute. One sound, one decision, one line.
    groups: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        key = r.get("group_key") or r.get("id")
        g = groups.get(key)
        if not g:
            groups[key] = {
                "group_key": key,
                "kind": r.get("kind"),
                "word": r.get("word"),
                "shape": r.get("shape"),
                "at": r.get("created_at"),
                "ids": [r.get("id")],
                "unacked_ids": [] if r.get("ack_at") else [r.get("id")],
                "headline": r.get("headline"),
                "action": r.get("action"),
                "members": [r],
            }
        else:
            g["ids"].append(r.get("id"))
            if not r.get("ack_at"):
                g["unacked_ids"].append(r.get("id"))
            g["members"].append(r)
            if str(r.get("created_at") or "") > str(g["at"] or ""):
                g["at"] = r.get("created_at")

    out: List[Dict[str, Any]] = []
    for g in groups.values():
        count = len(g["ids"])
        if count > 1:
            plural = "people" if count > 1 else "person"
            verb = {
                NEEDS_HELP: f"{count} {plural} reported needing help in the same minute",
                WORSE: f"{count} {plural} got worse in the same minute",
                GONE_QUIET: f"{count} {plural} who asked for help went quiet in the same minute",
            }.get(g["kind"], f"{count} alarms in the same minute")
            g["headline"] = verb
            g["action"] = "Open the list and work down it. Every name is below."
        g["count"] = count
        g["acknowledged"] = len(g["unacked_ids"]) == 0
        # #289: what has happened SINCE. For a single-person alarm it is the
        # person's newest report; in a group it would be several different
        # facts, so the group line says how many have moved on and the
        # names underneath carry the detail.
        _since = [m.get("since_report") for m in g["members"] if m.get("since_report")]
        if count == 1:
            g["since_report"] = _since[0] if _since else None
        else:
            g["since_report"] = (
                {"words": (f"{len(_since)} of these have reported again since. "
                           "Open the list to see what each of them said."),
                 "at": None} if _since else None
            )
        # Who acknowledged, for the operator to read now and an inquiry to
        # read later.
        acked = [m for m in g["members"] if m.get("ack_at")]
        g["ack_by"] = acked[0].get("ack_by") if acked else None
        g["ack_at"] = acked[0].get("ack_at") if acked else None
        g["people"] = [
            {
                "id": m.get("id"),
                "device_id": m.get("device_id"),
                "who": _who(m),
                "headline": m.get("headline"),
                "action": m.get("action"),
                "acknowledged": bool(m.get("ack_at")),
                "ack_by": m.get("ack_by"),
                "ack_at": m.get("ack_at"),
                "since_report": m.get("since_report"),
            }
            for m in g["members"]
        ]
        g.pop("members", None)
        out.append(g)

    out.sort(key=lambda g: str(g.get("at") or ""), reverse=True)
    n_lq = len(long_quiet)
    return {
        "generated_at": _iso(n),
        "long_quiet": {
            "count": n_lq,
            "words": (
                (f"{n_lq} person asked for help and has been quiet for more than a day."
                 if n_lq == 1 else
                 f"{n_lq} people asked for help and have been quiet for more than a day.")
                + " They are still on the board. They do not sound, because a day-old"
                + " silence is not news — but they are still missing."
            ) if n_lq else None,
            "people": long_quiet,
        },
        "unacknowledged": len(unacked),
        "open": len(rows),
        "flood": flood,
        "flood_note": (
            f"More than {FLOOD_LIMIT} alarms in {FLOOD_WINDOW_MINUTES} minutes. "
            "Alarms of the same kind are being shown as one line per minute so "
            "the board stays readable. Nothing has been dropped."
        ) if flood else None,
        "groups": out,
    }
