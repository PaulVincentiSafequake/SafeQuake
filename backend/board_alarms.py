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


def _looks_like_test(row: Dict[str, Any]) -> bool:
    """One test-or-not answer, shared with the board and the counts (#301).

    Imported lazily because `deps` pulls in the app's settings, and this
    module is imported by scripts that have no app."""
    try:
        from deps import is_test_device
        return bool(is_test_device(row or {}))
    except Exception:
        return False


async def _open_alarm(db, device_id: str, kind: str) -> Optional[dict]:    return await db.board_alarms.find_one(
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
    re_raise: bool = False,
    now: Optional[datetime] = None,
) -> Optional[dict]:
    """Create one alarm, unless an unresolved one of the same kind already
    exists for this person. Returns the alarm, or None if it was a repeat.

    #290 (Paul, 2026-08-25 — reconfirmed three times): "when a new report
    arrives for a person whose last alarm was already acknowledged, the
    card never regains an Acknowledge button and is never counted in the
    alarm total." That was the dedupe rule doing its job too well. An
    acknowledgement means "I have seen THIS fact and I am dealing with
    it". A person getting WORSE is a new fact, so the acknowledgement no
    longer covers it and the alarm has to be put back in front of the
    operator — sound, flashing, button, counted. This is standard
    re-alarming: `re_raise` below un-acknowledges the existing alarm
    rather than creating a second one, so the strip never shows the same
    person twice for the same thing.

    Paul's ruling on the boundary (2026-08-25): ONLY worse re-alarms. A
    same-or-better report updates the yellow note and nothing else —
    otherwise every routine check-in from someone already being helped
    would sound the alarm again, which is how a room learns to ignore it.
    """
    n = _now(now)
    existing = await _open_alarm(db, device_id, kind)
    if existing:
        if re_raise and existing.get("ack_at"):
            try:
                await db.board_alarms.update_one(
                    {"id": existing["id"]},
                    {"$set": {
                        "ack_by": None,
                        "ack_at": None,
                        "headline": headline,
                        "action": action,
                        "severity": row.get("severity"),
                        "re_raised_at": _iso(n),
                        # Kept so the audit trail shows the alarm was
                        # acknowledged once and then re-opened by a new
                        # fact, rather than looking like it was never
                        # acknowledged at all.
                        "previous_ack_by": existing.get("ack_by"),
                        "previous_ack_at": existing.get("ack_at"),
                    },
                     "$inc": {"re_raise_count": 1}},
                )
            except Exception as e:
                logging.warning(f"board_alarms re-raise failed: {e}")
                return None
            out = dict(existing)
            out.update({
                "ack_by": None, "ack_at": None, "headline": headline,
                "action": action, "re_raised_at": _iso(n),
                "re_raise_count": int(existing.get("re_raise_count") or 0) + 1,
            })
            return out
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
        # #301 (Paul, 2026-08-25): test people used to be dropped before an
        # alarm was ever written, which is why "Add 33 test people" could
        # never be used to rehearse the alarm panel — the thing it exists
        # for. They now raise alarms like anybody else, flagged, and the
        # flag is what lets the board hide them until an operator ticks
        # "Show test entries". Nothing labelled TEST can ever be mistaken
        # for a real casualty, and nothing real is ever hidden.
        "is_test": bool(row.get("is_test") or row.get("synthetic")) or _looks_like_test(row),
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
    # #304 (Paul, 2026-08-26): every write inside one status change
    # shares one wall-clock instant, so sibling rows re-raised together
    # get the same `re_raised_at` and the merged story dedupes them.
    now = _now(now)
    raised: List[dict] = []
    device_id = doc.get("device_id") or ""
    if not device_id:
        return raised
    # #301 (Paul, 2026-08-25): test people used to return here, so the
    # alarm panel could never be rehearsed with them — the reason they
    # exist. They now go through exactly the same path, and the alarm row
    # carries a test flag so the board can keep them out of sight until an
    # operator ticks "Show test entries".
    prior = prior or {}
    was = str(prior.get("status") or "")
    now_status = str(doc.get("status") or "")

    if now_status == "trapped" and was != "trapped":
        a = await raise_alarm(
            db, kind=NEEDS_HELP, device_id=device_id, row=doc,
            headline=f"{_who(doc)} needs help",
            action=_help_action(doc),
            # #290: needing help again after being safe is a new fact, and
            # a worse one. If an old acknowledged alarm is still open, put
            # it back in front of the operator rather than silently
            # stamping a note on it.
            re_raise=True, now=now,
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
                headline=head, action=_help_action(doc),
                # #290: getting worse is exactly the case an old
                # acknowledgement no longer covers.
                re_raise=True, now=now,
            )
            if a:
                raised.append(a)

    # A phone that speaks again is no longer quiet. That is the situation
    # being resolved, which is the only thing allowed to clear an alarm.
    await resolve_for_device(
        db, device_id, kinds=[GONE_QUIET],
        reason="Their phone reported again.", now=now,
    )
    # #303 (Paul, 2026-08-26 — live, urgent): a brand-new report from a
    # person whose open cards were already acknowledged was being
    # presented as "already handled". An acknowledgement means "I have
    # seen THIS fact and I am dealing with it"; a new fact — of any kind,
    # not only strictly-worse — is something the operator has not seen
    # yet, so the card has to go back into needs-action.
    #
    # #290 already did this for strictly-worse re-alarms via the
    # `re_raise` path inside `raise_alarm`, but that path only fires when
    # a fresh alarm of the same kind is being raised. It does not fire
    # when the person reports again at the SAME severity, and it does not
    # touch open rows of a DIFFERENT kind (which is how QQ43D ended up
    # with two acknowledged cards on the board at the same time). This
    # widens the rule to every status report: any new fact clears
    # acknowledgement on all of that person's open cards.
    await _clear_ack_on_new_report(db, device_id, doc, now=now)
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


async def _clear_ack_on_new_report(
    db, device_id: str, doc: Dict[str, Any], now: Optional[datetime] = None,
) -> int:
    """#303 — any new status report un-acknowledges that person's open
    cards.

    The old acknowledgement stays visible in the timeline (`previous_ack_by`
    / `previous_ack_at`) so an inquiry can still see who saw what and when.
    We do NOT touch rows that were never acknowledged — nothing to clear —
    and we do NOT create rows here; that is `raise_alarm`'s job.

    Returns the number of rows whose acknowledgement was cleared, so the
    caller (and tests) can see whether a new fact re-opened anything.
    """
    n = _now(now)
    try:
        rows = await db.board_alarms.find(
            {"device_id": device_id, "resolved_at": None,
             "ack_at": {"$ne": None}},
            {"_id": 0},
        ).to_list(50)
    except Exception as e:
        logging.warning(f"board_alarms clear-ack read failed: {e}")
        return 0
    if not rows:
        return 0
    cleared = 0
    # #304: use one timestamp for every row cleared in this batch so
    # sibling rows all get the same `re_raised_at`. That way the merged
    # story dedupes on (at, words) and shows one "sounded again" line
    # for the one event, not one per row.
    stamp = _iso(n)
    for r in rows:
        try:
            await db.board_alarms.update_one(
                {"id": r["id"]},
                {"$set": {
                    "ack_by": None,
                    "ack_at": None,
                    "re_raised_at": stamp,
                    "previous_ack_by": r.get("ack_by"),
                    "previous_ack_at": r.get("ack_at"),
                    # Keep the newest fact on the row itself so a
                    # re-opened card carries the report that re-opened it,
                    # not just a stale headline from when it was raised.
                    "severity": doc.get("severity") or r.get("severity"),
                 },
                 "$inc": {"re_raise_count": 1}},
            )
            cleared += 1
        except Exception as e:
            logging.warning(f"board_alarms clear-ack update failed: {e}")
    return cleared


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
                    "is_test": bool(r.get("is_test") or r.get("synthetic")),
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


def _story(alarm: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The alarm's own history, in order, in plain words (#298).

    Paul, 2026-08-25: "if I was an operator, I have no idea what all that
    was about." An alarm card used to show one line and a button. Now it
    can show what was reported when it was raised, what the person has
    said since, whether somebody acknowledged it, and whether a worse
    report re-opened it afterwards.

    Everything here comes off the alarm row itself — no extra queries, so
    a board with fifty alarms costs exactly what it did before.
    """
    steps: List[Dict[str, Any]] = []
    sev = str(alarm.get("severity") or "").lower()
    raised = alarm.get("headline") or "Alarm raised"
    if sev in SEVERITY_WORD:
        raised = f"{raised} — reported {SEVERITY_WORD[sev]}"
    steps.append({"at": alarm.get("created_at"), "words": raised})
    since = alarm.get("since_report") or {}
    if since.get("words"):
        steps.append({"at": since.get("at"),
                      "words": f"Since then, they {since['words']}"})
    if alarm.get("previous_ack_at"):
        steps.append({
            "at": alarm.get("previous_ack_at"),
            "words": ("Acknowledged by "
                      + str(alarm.get("previous_ack_by") or "an operator")),
        })
    if alarm.get("re_raised_at"):
        steps.append({
            "at": alarm.get("re_raised_at"),
            "words": ("Sounded again, because they got worse after that "
                      "acknowledgement — it needs a fresh decision."),
        })
    if alarm.get("ack_at"):
        steps.append({
            "at": alarm.get("ack_at"),
            "words": ("Acknowledged by " + str(alarm.get("ack_by") or "an operator")
                      + " — still on the board until they are reached."),
        })
    else:
        steps.append({"at": None, "words": "Not acknowledged by anybody yet."})
    return steps


async def list_open(db, now: Optional[datetime] = None,
                    include_test: bool = False) -> Dict[str, Any]:
    """Everything the strip needs, grouped, with the unacknowledged count
    the board must always show.

    #301: `include_test` mirrors the board's "Show test entries" tick. Off,
    an operator sees only real casualties; on, the rehearsal appears here
    too — same shapes, same sounds, same buttons — so the alarm panel can
    actually be practised on. Test alarms are labelled, never silent
    substitutes for real ones.

    #303 (Paul, 2026-08-26 — live, urgent): one card per person, not one
    per past trigger event. The strip used to show one row per
    (device_id, kind), so a single person who had gone worse and then
    also gone quiet — or whose old NEEDS_HELP never resolved before a new
    WORSE arrived — appeared as two cards side by side. This dedupes by
    device_id BEFORE grouping, so the operator sees each person exactly
    once and their full history is inside "What happened". The underlying
    rows are kept in Mongo so an inquiry can still see everything.
    """
    n = _now(now)
    try:
        rows = await db.board_alarms.find(
            {"resolved_at": None}, {"_id": 0},
        ).sort("created_at", -1).to_list(500)
    except Exception as e:
        logging.warning(f"board_alarms read failed: {e}")
        rows = []
    if not include_test:
        rows = [r for r in rows if not r.get("is_test")]

    # #303 — collapse to one card per person. The card's headline, action,
    # severity, kind, shape, group_key, and ack fields come from the
    # PRIMARY row (see `_pick_primary` — report-driven rows outrank
    # inferred ones so a live "IMMEDIATE" report never hides behind a
    # freshly-raised "gone quiet"), but the ID list contains every open
    # row for that person so a single "Acknowledge" press silences them
    # all at once and an inquiry can still read every underlying row.
    def _pick_primary(rlist: List[Dict[str, Any]]) -> Dict[str, Any]:
        """#304 (Paul, 2026-08-26 — live re-test): the card face has to
        represent the person's condition RIGHT NOW, and a `gone quiet`
        row raised seconds ago by the silence sweep is not more
        important than an `IMMEDIATE, cannot move` report that came
        directly from the person a minute earlier.

        Rule: prefer report-driven rows (NEEDS_HELP / WORSE) over
        inferred ones (GONE_QUIET) when picking the card face. Within
        each class, newest-by-created_at wins.
        """
        report_driven = [r for r in rlist if r.get("kind") in (NEEDS_HELP, WORSE)]
        pool = report_driven or rlist
        return max(
            pool,
            key=lambda r: (str(r.get("created_at") or ""),
                           str(r.get("re_raised_at") or "")),
        )

    per_device: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows:
        did = r.get("device_id") or ""
        if not did:
            continue
        per_device.setdefault(did, []).append(r)

    # Rebuild `rows` as one synthetic row per device. Each synthetic row
    # carries the underlying row IDs (in `_all_ids`) and every underlying
    # row (in `_all_rows`) so the story below can walk the whole timeline.
    merged_rows: List[Dict[str, Any]] = []
    for all_rows in per_device.values():
        primary = _pick_primary(all_rows)
        ordered = sorted(all_rows, key=lambda r: str(r.get("created_at") or ""))
        card = dict(primary)
        card["_all_ids"] = [r.get("id") for r in ordered if r.get("id")]
        # If ANY of the underlying rows is unacknowledged, the whole card
        # is unacknowledged — otherwise a person whose newest row happens
        # to be an acked GONE_QUIET could hide an unacked NEEDS_HELP
        # underneath it.
        any_unacked = any(not r.get("ack_at") for r in ordered)
        if any_unacked:
            card["ack_at"] = None
            card["ack_by"] = None
        card["_all_rows"] = ordered
        # #304 — if a report-driven row is the card face but the person
        # ALSO has an open GONE_QUIET row, name that in the card's
        # "since" line so the operator sees both facts at once.
        if primary.get("kind") in (NEEDS_HELP, WORSE):
            quiet = next(
                (r for r in ordered if r.get("kind") == GONE_QUIET),
                None,
            )
            if quiet:
                q_at = quiet.get("created_at")
                existing_since = card.get("since_report") or {}
                # #306 (Paul, 2026-08-26 — live re-test): the sentence
                # used to concatenate `str(q_at)` directly, which leaked
                # a raw ISO timestamp like "2026-08-26T08:16:16.166124
                # +00:00" onto the card. The at-timestamp is carried on
                # the `since_report.at` field the client formats itself,
                # so the sentence just names the fact.
                extra = "Their phone has also gone quiet"
                if existing_since.get("words"):
                    card["since_report"] = {
                        "words": existing_since["words"] + ". " + extra,
                        # Keep the newest at-timestamp so the client's
                        # own formatter picks the right instant.
                        "at": existing_since.get("at") or q_at,
                    }
                else:
                    card["since_report"] = {"words": extra, "at": q_at}
        merged_rows.append(card)
    merged_rows.sort(
        key=lambda r: (str(r.get("created_at") or ""),
                       str(r.get("re_raised_at") or "")),
        reverse=True,
    )
    rows = merged_rows

    # #341 (Paul, 2026-09-04 — live re-test): the alarm card must say
    # when a person has no saved location. Paul reported QQ43D on a
    # NEEDS HELP card with no pin on the map and no explanation on the
    # card itself. The triage-sidebar card already carries the note; the
    # alarm card needs the same fact so an operator reading the alarm
    # panel does not have to cross-reference the sidebar to understand
    # why the pin is missing.
    #
    # Batched device_status lookup by device_id — single query per
    # list_open() call, no per-alarm chatter. A person "has a saved
    # location" when both latitude and longitude are numbers; anything
    # else (missing field, None, empty) counts as no location.
    _dids = sorted({r.get("device_id") for r in rows if r.get("device_id")})
    _has_loc: Dict[str, bool] = {}
    if _dids:
        try:
            async for st in db.device_status.find(
                {"device_id": {"$in": list(_dids)}},
                {"_id": 0, "device_id": 1, "latitude": 1, "longitude": 1},
            ):
                did = st.get("device_id")
                if not did:
                    continue
                lat = st.get("latitude")
                lng = st.get("longitude")
                _has_loc[did] = (
                    isinstance(lat, (int, float)) and isinstance(lng, (int, float))
                )
        except Exception as e:
            logging.warning(f"alarm has_location lookup failed: {e}")
    # Attach the flag to every row so it flows into `people` below.
    for r in rows:
        r["_has_location"] = bool(_has_loc.get(r.get("device_id"), False))

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
    if not include_test:
        long_quiet = [p for p in long_quiet if not p.get("is_test")]
    window_start = _iso(n - timedelta(minutes=FLOOD_WINDOW_MINUTES))
    recent = [r for r in rows if str(r.get("created_at") or "") >= window_start]
    flood = len(recent) > FLOOD_LIMIT

    # Group by kind + minute. One sound, one decision, one line.
    groups: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        key = r.get("group_key") or r.get("id")
        # Count of alarms for this row: every underlying row for the
        # device, so the group summary and the top-of-panel count still
        # match Mongo.
        ids = r.get("_all_ids") or [r.get("id")]
        unacked_ids = [
            rr.get("id") for rr in (r.get("_all_rows") or [r])
            if not rr.get("ack_at")
        ]
        g = groups.get(key)
        if not g:
            groups[key] = {
                "group_key": key,
                "kind": r.get("kind"),
                "word": r.get("word"),
                "shape": r.get("shape"),
                "at": r.get("created_at"),
                "ids": list(ids),
                "unacked_ids": list(unacked_ids),
                "headline": r.get("headline"),
                "action": r.get("action"),
                "members": [r],
            }
        else:
            g["ids"].extend(ids)
            g["unacked_ids"].extend(unacked_ids)
            g["members"].append(r)
            if str(r.get("created_at") or "") > str(g["at"] or ""):
                g["at"] = r.get("created_at")

    out: List[Dict[str, Any]] = []
    for g in groups.values():
        # #303: `count` is people, not rows. One card per person.
        count = len(g["members"])
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
        g["is_test"] = all(bool(m.get("is_test")) for m in g["members"])
        g["acknowledged"] = len(g["unacked_ids"]) == 0
        # #341: fold the per-member `has_location` up to the card. For a
        # single-person alarm the flag mirrors that person exactly. For a
        # multi-person minute-cluster we also count how many are missing
        # a saved location so the card can say "3 of 5 have no saved
        # location" instead of hiding a partial gap.
        _no_loc = [m for m in g["members"] if not m.get("_has_location")]
        if count == 1:
            g["has_location"] = bool(g["members"][0].get("_has_location"))
            g["missing_location_count"] = 0 if g["has_location"] else 1
        else:
            g["has_location"] = len(_no_loc) == 0
            g["missing_location_count"] = len(_no_loc)
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
        # read later. Only report an ack at the group level when EVERY
        # member of the group is acked — otherwise a card could show
        # "acknowledged by X" while still needing action.
        if g["acknowledged"]:
            first = g["members"][0]
            g["ack_by"] = first.get("ack_by")
            g["ack_at"] = first.get("ack_at")
        else:
            g["ack_by"] = None
            g["ack_at"] = None
        g["people"] = [
            {
                "id": m.get("id"),
                "device_id": m.get("device_id"),
                "who": _who(m),
                "headline": m.get("headline"),
                "action": m.get("action"),
                # #303: card-level ack reflects all of that person's rows.
                "acknowledged": not any(
                    not rr.get("ack_at") for rr in (m.get("_all_rows") or [m])
                ),
                "ack_by": (m.get("_all_rows") or [m])[-1].get("ack_by") if
                    all(rr.get("ack_at") for rr in (m.get("_all_rows") or [m])) else None,
                "ack_at": (m.get("_all_rows") or [m])[-1].get("ack_at") if
                    all(rr.get("ack_at") for rr in (m.get("_all_rows") or [m])) else None,
                "since_report": m.get("since_report"),
                # #301: labelled, never hidden inside a real-looking row.
                "is_test": bool(m.get("is_test")),
                # #341 (Paul, 2026-09-04): does this device have a saved
                # latitude/longitude on device_status? Drives the "no
                # saved location" note on the alarm card, so an operator
                # reading a red NEEDS HELP row never has to guess whether
                # the map is broken or the phone simply never shared its
                # position. False when either coord is missing/null.
                "has_location": bool(m.get("_has_location")),
                # #298 (Paul, 2026-08-25): "if I was an operator, I have no
                # idea what all that was about." Every alarm now carries its
                # own short story — what was reported when it was raised,
                # what has happened since, whether it was acknowledged and
                # then re-opened by a worse report, and by whom. Built from
                # the alarm row itself, so reading it costs nothing.
                "severity": m.get("severity"),
                "created_at": m.get("created_at"),
                "re_raised_at": m.get("re_raised_at"),
                "re_raise_count": int(m.get("re_raise_count") or 0),
                # #303: story is now merged across ALL open rows for this
                # person, chronologically, so "What happened" carries the
                # entire history of that person's incidents.
                "story": _merged_story(m.get("_all_rows") or [m]),
            }
            for m in g["members"]
        ]
        g.pop("members", None)
        out.append(g)

    out.sort(key=lambda g: str(g.get("at") or ""), reverse=True)
    n_lq = len(long_quiet)
    # #303: the top-of-panel "unacknowledged" number is people, not rows,
    # so it never disagrees with the number of cards on screen.
    unacked_people = sum(
        1 for r in rows
        if any(not rr.get("ack_at") for rr in (r.get("_all_rows") or [r]))
    )
    open_people = len(rows)
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
        "unacknowledged": unacked_people,
        "open": open_people,
        "flood": flood,
        "flood_note": (
            f"More than {FLOOD_LIMIT} alarms in {FLOOD_WINDOW_MINUTES} minutes. "
            "Alarms of the same kind are being shown as one line per minute so "
            "the board stays readable. Nothing has been dropped."
        ) if flood else None,
        "groups": out,
    }


def _merged_story(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """#303 — the full history of one person's incidents, in order, built
    from every open row we have for them.

    #304 (Paul, 2026-08-26 — live re-test): the story used to walk each
    row's fixed logical order (raise → since → ack → re-raise → current)
    and print them in blocks, so the timeline read 09:53, 12:29, 09:55,
    11:35 — nowhere near time-ordered — and any `since_report` stamped
    on more than one row would print twice.

    Now: flatten every step from every row into one list, sort strictly
    by the timestamp on the step, and drop duplicates (same instant +
    same words). Steps with no timestamp — the trailing "Not
    acknowledged by anybody yet." — go at the end, once, only if the
    card is currently unacknowledged.
    """
    if not rows:
        return []
    steps: List[Dict[str, Any]] = []
    for r in rows:
        for s in _story(r):
            steps.append({
                "at": s.get("at"),
                "words": str(s.get("words") or ""),
            })
    # #304: dedupe on (at, words) so the same event, stamped on more
    # than one row (e.g., a `since_report` shared across sibling rows),
    # only appears once.
    seen = set()
    dedup: List[Dict[str, Any]] = []
    for s in steps:
        key = (str(s.get("at") or ""), s.get("words") or "")
        if key in seen:
            continue
        seen.add(key)
        dedup.append(s)
    # #304: strict time order. Steps without a timestamp — the "not
    # acknowledged" placeholder — go last, and only once, and only if
    # the card is truly unacknowledged (i.e. every open row is unacked).
    timed = [s for s in dedup if s.get("at")]
    untimed = [s for s in dedup if not s.get("at")]
    timed.sort(key=lambda s: str(s.get("at") or ""))
    # Only keep the trailing "not acknowledged" step if EVERY underlying
    # row is currently unacknowledged — otherwise a person whose newest
    # row is acked would still see "not acknowledged by anybody yet."
    if any(r.get("ack_at") for r in rows):
        untimed = [s for s in untimed if "Not acknowledged" not in s["words"]]
    # And dedupe the untimed tail too — one placeholder is enough.
    seen_untimed = set()
    untimed_out: List[Dict[str, Any]] = []
    for s in untimed:
        if s["words"] in seen_untimed:
            continue
        seen_untimed.add(s["words"])
        untimed_out.append(s)
    return timed + untimed_out


async def dedupe_open_alarms(db, now: Optional[datetime] = None) -> Dict[str, Any]:
    """#303 (Paul, 2026-08-26): "keep a hidden backup copy of what was
    merged. Nothing in this project gets permanently deleted."

    This snapshots every open board_alarms row for any device that has
    more than one open row into `board_alarms_backup`, so if the merge
    ever looks wrong, the exact pre-merge state is still readable. It
    does NOT delete or resolve anything — the runtime dedupe happens in
    `list_open` — so this is safe to run repeatedly on startup.

    Returns a summary the admin endpoint can echo back.
    """
    n = _now(now)
    try:
        rows = await db.board_alarms.find(
            {"resolved_at": None}, {"_id": 0},
        ).to_list(2000)
    except Exception as e:
        logging.warning(f"board_alarms dedupe read failed: {e}")
        return {"scanned": 0, "backed_up": 0, "devices": 0, "error": str(e)}
    by_dev: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows:
        did = r.get("device_id") or ""
        if not did:
            continue
        by_dev.setdefault(did, []).append(r)
    to_backup: List[Dict[str, Any]] = []
    devices = 0
    for did, rlist in by_dev.items():
        if len(rlist) <= 1:
            continue
        devices += 1
        for r in rlist:
            snap = dict(r)
            snap["_snapshot_at"] = _iso(n)
            snap["_snapshot_reason"] = "303-per-device-dedupe"
            snap["_source_id"] = r.get("id")
            # Give the backup a fresh key so re-runs do not collide.
            snap["_backup_id"] = str(uuid.uuid4())
            to_backup.append(snap)
    if to_backup:
        try:
            await db.board_alarms_backup.insert_many(to_backup)
        except Exception as e:
            logging.warning(f"board_alarms backup write failed: {e}")
            return {"scanned": len(rows), "backed_up": 0,
                    "devices": devices, "error": str(e)}
    return {
        "scanned": len(rows),
        "backed_up": len(to_backup),
        "devices": devices,
        "at": _iso(n),
    }
