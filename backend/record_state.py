"""#268 (Neo, 2026-08-21 — Paul): the four kinds of silence, and which
records belong on the working rescue board.

The defect this exists to kill
------------------------------
The board showed two entries that were both Paul: `CW7EF` (his live
phone) and `F6XJY` (an old install that no longer exists on any phone).
`F6XJY` read "Not responding · Phone dark since 21:08 · Battery ?%".
To an operator that is a missing person with a last known position.
It is a deleted app. In a real incident a team gets sent to a last
known location for someone who does not exist while a real missing
person waits — a phantom casualty.

Doctrine encoded here (Paul's words, kept verbatim so a future refactor
cannot quietly reinterpret them)
--------------------------------------------------------------------
1. "Silence is information, but only if we know what kind of silence it
   is." Four states, never one bucket.
2. "Never infer that anyone is safe from an absence of data." Nothing in
   this module ever marks anyone safe, and nothing ever reduces concern.
3. "Never delete a person from a rescue board." Nothing here deletes.
   The strongest thing this module can say is `on_working_board = False`,
   which moves a record to a labelled area an operator can open.
4. "Status always outranks device state." A person who has EVER reported
   needing help, or was ever marked trapped/injured/rescued, stays on
   the working board no matter what their phone's push token does.
   Enforced by the first branch of `classify` and pinned by
   tests/test_record_state_268.py.

What the phone networks actually tell us — and its limits
--------------------------------------------------------
* Apple APNs returns reason `Unregistered` (HTTP 410) when the app is no
  longer installed on that phone. That is a POSITIVE FACT reported by
  Apple, not an absence of data, which is exactly why it is allowed to
  move a record off the working board.
* `BadDeviceToken` (HTTP 400) is NOT that fact. It can mean a malformed
  token or a prod/sandbox mismatch — a configuration error on our side.
  Before #268 both reasons were treated identically, which meant a
  config mistake could manufacture a phantom "removed". Only
  `APP_REMOVED_REASON` may claim removal now; everything else reads as
  "Phone went dark" plus a technical note.
* A phone destroyed in the earthquake, out of battery, or out of signal
  produces NO `Unregistered`. Apple stores or drops the push. So a
  destroyed phone lands on "Phone went dark", which is the safe default
  and is deliberate.
* `Unregistered` cannot distinguish "the user deleted the app" from "the
  phone was wiped/restored" or "the OS invalidated the token". It is
  reported as what it is — the phone told us the app is gone — and never
  as intent.
* Android has no equivalent. The Emergent/SuprSend relay reports
  chunk-level HTTP status, not per-token invalidity, so an Android
  record can never be classified "app removed" (see apns.py
  DEAD_TOKEN_REASONS). Android silence is always "Phone went dark".
"""
from __future__ import annotations

import math
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Set

# ── The four states ───────────────────────────────────────────────────
# Wire values are snake_case; NOTHING user-facing ever prints them. The
# label is what an operator reads and what gets spoken over a radio.
WAITING = "waiting_for_answer"
DARK = "phone_went_dark"
APP_REMOVED = "app_removed"
NEVER_USED = "never_used"
RESOLVED = "resolved_by_operator"

# Read each of these aloud. Distinct on the first word, no jargon, no
# acronym, nothing that can be misheard as another one at 4am over a
# radio. "Not responding", "dead token" and "unregistered" are banned
# from every operator-facing surface by this table.
LABELS: Dict[str, str] = {
    WAITING: "Waiting for an answer",
    DARK: "Phone went dark",
    APP_REMOVED: "App removed from this phone",
    NEVER_USED: "Never used the app",
    RESOLVED: "Resolved by an operator",
}

# Only this APNs reason is evidence that the app is gone from the phone.
APP_REMOVED_REASON = "Unregistered"

# ── Dark thresholds (Paul, 2026-08-21) ────────────────────────────────
# "Forty-five minutes is reasonable for a person who reported safe. It is
# far too long for a person who reported needing help. For them, silence
# is clinically meaningful and the operator needs to know quickly."
# The card always shows the ACTUAL elapsed time as well, so an operator
# judges from the real number and never has to trust a label to flip.
DARK_AFTER_MINUTES = 45
DARK_AFTER_MINUTES_NEEDS_HELP = 15

# ── Mass-dark detection (#268, Paul) ──────────────────────────────────
# "If many devices go dark at roughly the same moment, that is almost
# certainly a network or power failure, not many people simultaneously
# going missing."
#
# How these numbers were chosen — not guessed:
#
# WINDOW = 10 minutes. Independent phone loss is a trickle: batteries
#   die at random times across hours, individuals wander out of coverage
#   one at a time. Correlated loss is the signature of infrastructure
#   failure, and a cell site or grid outage takes every phone behind it
#   offline within seconds. The window has to be wider than "seconds"
#   only because our own check-in ladder is coarse: the tightest
#   interval is 15 minutes, so two phones that died in the same second
#   can have last-contact stamps up to ~15 minutes apart. 10 minutes
#   catches the bulk of one cluster without being wide enough for
#   ordinary battery deaths (spread over hours) to co-occur.
# MIN_RECORDS = 5. An absolute floor. With three testers on the board,
#   one phone dying is 33% and the notice would fire constantly and
#   train an operator to ignore it.
# MIN_SHARE = 0.40. A proportion test as well, so a large deployment
#   does not fire the notice on 5 unrelated dropouts out of 500.
# BOTH must hold. Change either number here — they are read from this
# one place by every surface.
MASS_DARK_WINDOW_MINUTES = 10
MASS_DARK_MIN_RECORDS = 5
MASS_DARK_MIN_SHARE = 0.40


def parse_dt(ts) -> Optional[datetime]:
    if not ts:
        return None
    if isinstance(ts, datetime):
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    try:
        d = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _clock(dt: Optional[datetime]) -> str:
    return dt.strftime("%H:%M") if dt else "an unknown time"


def dur_words(minutes: Optional[int]) -> str:
    """"1 hour 12 minutes". Spoken-language duration, no abbreviations."""
    if minutes is None:
        return "an unknown time"
    m = max(0, int(minutes))
    if m < 1:
        return "less than a minute"
    h, mm = divmod(m, 60)
    parts = []
    if h:
        parts.append(f"{h} hour" if h == 1 else f"{h} hours")
    if mm:
        parts.append(f"{mm} minute" if mm == 1 else f"{mm} minutes")
    return " ".join(parts)


@dataclass(frozen=True)
class RecordState:
    """One record, one state. Every surface reads this — board card, map
    marker, counts, CSV, PDF — so no two surfaces can disagree."""
    state: Optional[str]          # None = they have answered; not silent
    label: str                    # plain English, radio-safe
    detail: str                   # the sentence shown under the label
    on_working_board: bool
    held_reason: Optional[str]    # why a device signal did NOT move them
    off_board_reason: Optional[str]   # why they are not on the board
    silent_minutes: Optional[int]
    dark_after_minutes: int
    ever_needed_help: bool
    app_removed_at: Optional[str]
    token_note: Optional[str]     # ambiguous token rejection, if any
    # #268, and it is where two of Paul's rules meet:
    #   rule 1 — "if a token goes dead during a live incident for someone
    #            who has not yet answered ... do not move them"
    #   rule 4 — "dead and removed devices must not be counted in 'not
    #            responding' anywhere"
    # Both are right. A record whose phone reported the app removed can
    # therefore be ON the working board (held, because an alert is live)
    # while being counted in its own named bucket instead of inside
    # "not responding". Anyone with help history is exempt: status
    # outranks device state, so they are counted as the person they are.
    count_in_status_buckets: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def ever_needed_help_row(row: Dict[str, Any]) -> bool:
    """Help history carried on the device_status row itself. The caller
    ORs this with the append-only status_events ledger, because a row can
    be overwritten and the ledger cannot."""
    if row.get("rescued_at") or row.get("trapped_since"):
        return True
    if row.get("needs_extraction"):
        return True
    if (row.get("status") or "") == "trapped":
        return True
    if (row.get("pre_rescue_status") or "") == "trapped":
        return True
    return False


def classify(
    row: Optional[Dict[str, Any]],
    push_row: Optional[Dict[str, Any]],
    *,
    ever_needed_help: bool,
    ever_located: bool,
    incident_active: bool,
    last_alert_at: Optional[datetime] = None,
    now: Optional[datetime] = None,
) -> RecordState:
    """The single classifier. `row` is a device_status row (None when the
    phone registered for alerts but has never checked in); `push_row` is
    the matching push_devices row (None when we have no registration).

    Branch order IS the doctrine — read it top to bottom:
      1. resolved by a human    → off board, because a human said so
      2. ever needed help       → ON the board, always, whatever the token says
      3. never used the app     → off board, nothing to act on, but counted aloud
      4. app removed            → off board ONLY when no alert is live
      5. otherwise              → dark / waiting / answering, by the clock
    """
    now = now or datetime.now(timezone.utc)
    row = row or {}

    last = parse_dt(row.get("updated_at"))
    silent_minutes = int((now - last).total_seconds() // 60) if last else None
    dark_after = (
        DARK_AFTER_MINUTES_NEEDS_HELP if ever_needed_help else DARK_AFTER_MINUTES
    )

    # What the phone network told us, and how much of it we are allowed
    # to believe (see module docstring).
    #
    # Two sources, deliberately. The push registration row carries the
    # live signal; `app_removed_at` on the rescue record itself is the
    # durable copy, because the registration row is transient (a registry
    # wipe or a re-register removes it) and without the durable copy a
    # known-deleted app silently reverts to "Phone went dark" and walks
    # back onto the working board as a missing person.
    dead_reason = (push_row or {}).get("dead_token_reason")
    removed_at = (
        (push_row or {}).get("dead_token_at")
        if (push_row or {}).get("dead_token") and dead_reason == APP_REMOVED_REASON
        else None
    ) or row.get("app_removed_at")
    token_note = None
    if (push_row or {}).get("dead_token") and not removed_at:
        token_note = (
            f"This phone's push token was rejected ({dead_reason or 'reason not given'}). "
            "That is not proof the app was removed, so this record is treated as "
            "a phone that went dark."
        )

    is_dark = silent_minutes is not None and silent_minutes > dark_after
    awaiting = _awaiting_answer(row, last, last_alert_at)

    def _clock_state() -> tuple[Optional[str], str]:
        """dark / waiting / answering, from the clock alone."""
        if is_dark:
            return DARK, (
                f"Nothing from this phone for {dur_words(silent_minutes)} "
                f"(last heard {_clock(last)}). No contact possible. "
                "The status and position shown are LAST KNOWN."
            )
        if awaiting:
            return WAITING, (
                "Alerted"
                + (f" at {_clock(last_alert_at)}" if last_alert_at else "")
                + ", no reply yet. The phone is still reachable — last heard "
                + f"{dur_words(silent_minutes)} ago."
            )
        return None, (
            f"Answering. Last heard {dur_words(silent_minutes)} ago."
            if silent_minutes is not None else "No contact recorded yet."
        )

    # 1 ── a human resolved this record, with a reason on the file.
    if row.get("resolved_at"):
        who = row.get("resolved_by") or "an operator"
        why = row.get("resolved_reason") or "no reason recorded"
        return RecordState(
            state=RESOLVED,
            label=LABELS[RESOLVED],
            detail=(
                f"Taken off the working board by {who} at "
                f"{_clock(parse_dt(row.get('resolved_at')))}. Reason: {why}. "
                "Nothing has been deleted — this record is still here in full."
            ),
            on_working_board=False,
            held_reason=None,
            off_board_reason=f"Resolved by {who}: {why}",
            count_in_status_buckets=False,
            silent_minutes=silent_minutes,
            dark_after_minutes=dark_after,
            ever_needed_help=ever_needed_help,
            app_removed_at=removed_at,
            token_note=token_note,
        )

    # 2 ── STATUS OUTRANKS DEVICE STATE. This branch is the guarantee.
    # A trapped person whose phone is then destroyed, or whose app is
    # then removed, must not drop off the board.
    if ever_needed_help:
        state, detail = _clock_state()
        held = None
        if removed_at:
            held = (
                "Kept on the working board: this person reported needing help. "
                f"Their phone reported the app was removed at "
                f"{_clock(parse_dt(removed_at))} — that is not a report that "
                "they are safe."
            )
        return RecordState(
            state=state, label=LABELS.get(state, "Answering"), detail=detail,
            on_working_board=True, held_reason=held, off_board_reason=None,
            silent_minutes=silent_minutes, dark_after_minutes=dark_after,
            ever_needed_help=True, app_removed_at=removed_at,
            token_note=token_note,
        )

    # 3 ── registered for alerts, never opened the app, never located.
    # Paul, 2026-08-21: "if a person ever had a position recorded, even
    # once, they are not in this category. Never demote someone we have
    # previously located."
    if not ever_located and not row.get("status"):
        reg = parse_dt((push_row or {}).get("created_at"))
        return RecordState(
            state=NEVER_USED,
            label=LABELS[NEVER_USED],
            detail=(
                "This phone is registered for alerts"
                + (f" (since {reg.strftime('%d %b')})" if reg else "")
                + " but the app has never been opened and no position has ever "
                "been recorded, so there is nowhere for a team to go. "
                "They received the alert and have not answered."
            ),
            on_working_board=False,
            held_reason=None,
            off_board_reason="Never used the app — no position ever recorded",
            count_in_status_buckets=False,
            silent_minutes=silent_minutes, dark_after_minutes=dark_after,
            ever_needed_help=False, app_removed_at=removed_at,
            token_note=token_note,
        )

    # 4 ── the phone told us the app is gone.
    if removed_at:
        if incident_active:
            # Paul's question 1, answered in code: a token dying mid-incident
            # for someone who has not answered must NOT move them.
            state, detail = _clock_state()
            return RecordState(
                state=state or WAITING,
                label=LABELS.get(state or WAITING),
                detail=detail,
                on_working_board=True,
                held_reason=(
                    f"The app was removed from this phone at "
                    f"{_clock(parse_dt(removed_at))}. That is not a report that "
                    "they are safe, and an alert is live, so this record stays "
                    "on the working board until the alert is stood down. It is "
                    "counted as an app that was removed, not as a person who is "
                    "not responding."
                ),
                off_board_reason=None,
                silent_minutes=silent_minutes, dark_after_minutes=dark_after,
                ever_needed_help=False, app_removed_at=removed_at,
                token_note=token_note,
                count_in_status_buckets=False,
            )
        return RecordState(
            state=APP_REMOVED,
            label=LABELS[APP_REMOVED],
            detail=(
                f"This phone told us the app was removed at "
                f"{_clock(parse_dt(removed_at))}. This is a deleted app, not a "
                "missing person. Nothing has been deleted from the record."
            ),
            on_working_board=False,
            held_reason=None,
            off_board_reason=(
                f"The phone reported the app was removed at "
                f"{_clock(parse_dt(removed_at))}"
            ),
            count_in_status_buckets=False,
            silent_minutes=silent_minutes, dark_after_minutes=dark_after,
            ever_needed_help=False, app_removed_at=removed_at,
            token_note=token_note,
        )

    # 5 ── ordinary case: the clock decides, and nobody leaves the board.
    state, detail = _clock_state()
    return RecordState(
        state=state, label=LABELS.get(state, "Answering"), detail=detail,
        on_working_board=True, held_reason=None, off_board_reason=None,
        silent_minutes=silent_minutes, dark_after_minutes=dark_after,
        ever_needed_help=False, app_removed_at=removed_at,
        token_note=token_note,
    )


def _awaiting_answer(
    row: Dict[str, Any],
    last: Optional[datetime],
    last_alert_at: Optional[datetime],
) -> bool:
    """"Alerted, no reply yet, phone still reachable." Two independent
    sources, either is enough: they have not reported since the last
    broadcast, or the re-check ladder has an unanswered prompt out."""
    rc = row.get("recheck") or {}
    if int(rc.get("consecutive_missed") or 0) >= 1:
        return True
    if last_alert_at and last and last < last_alert_at:
        return True
    return False


# ── Mass-dark notice ──────────────────────────────────────────────────
def detect_mass_dark(
    dark_last_seen: Sequence[Any],
    reporting_total: int,
    now: Optional[datetime] = None,
) -> Optional[Dict[str, Any]]:
    """Did a cluster of phones stop reporting at roughly the same moment?

    `dark_last_seen` = last-contact timestamps of every record currently
    dark. `reporting_total` = how many records had ever been in contact
    (the denominator; a phone that never reported cannot "go dark").

    Returns None, or a notice dict with a plain-English sentence. This
    NEVER moves, reclassifies or downgrades anybody — it only adds a
    sentence to the top of the board so a wall of dark cards is read as
    what it almost certainly is.
    """
    now = now or datetime.now(timezone.utc)
    stamps = sorted(d for d in (parse_dt(s) for s in dark_last_seen) if d)
    if len(stamps) < MASS_DARK_MIN_RECORDS or reporting_total <= 0:
        return None

    window = timedelta(minutes=MASS_DARK_WINDOW_MINUTES)
    best_i = best_j = 0
    j = 0
    for i in range(len(stamps)):
        while j + 1 < len(stamps) and stamps[j + 1] - stamps[i] <= window:
            j += 1
        if (j - i) > (best_j - best_i):
            best_i, best_j = i, j
    size = best_j - best_i + 1
    share = size / float(reporting_total)
    if size < MASS_DARK_MIN_RECORDS or share < MASS_DARK_MIN_SHARE:
        return None

    first, last = stamps[best_i], stamps[best_j]
    spread = int((last - first).total_seconds() // 60)
    return {
        "kind": "mass_dark",
        "count": size,
        "reporting_total": reporting_total,
        "share_pct": int(round(share * 100)),
        "window_minutes": MASS_DARK_WINDOW_MINUTES,
        "first_at": first.isoformat(),
        "last_at": last.isoformat(),
        "text": (
            f"{size} of {reporting_total} phones stopped reporting within "
            f"{dur_words(spread)} of each other "
            f"({first.strftime('%H:%M')}–{last.strftime('%H:%M')}). "
            "This usually means a network or power failure, not that these "
            "people are missing. Nobody has been moved or reclassified."
        ),
    }


# ── Distance helper, shared with duplicates.py ─────────────────────────
def metres_between(lat1, lon1, lat2, lon2) -> Optional[float]:
    try:
        vals = [float(lat1), float(lon1), float(lat2), float(lon2)]
    except (TypeError, ValueError):
        return None
    if any(v is None or math.isnan(v) for v in vals):
        return None
    la1, lo1, la2, lo2 = (math.radians(v) for v in vals)
    dla, dlo = la2 - la1, lo2 - lo1
    a = math.sin(dla / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin(dlo / 2) ** 2
    return 6371000.0 * 2 * math.asin(min(1.0, math.sqrt(a)))


# ── Help-history and location-history sets ────────────────────────────
async def help_history_ids(db) -> Set[str]:
    """Every device_id that has EVER reported needing help, from the
    append-only ledger. The ledger is used rather than the live row
    because the live row can be overwritten and the ledger cannot —
    which is precisely what "status always outranks device state" needs
    in order to be true after a reinstall or a status change."""
    ids: Set[str] = set()
    for q in (
        {"status": "trapped"},
        {"status": "rescued"},
        {"needs_extraction": True},
    ):
        ids.update(
            d for d in await db.status_events.distinct("device_id", q) if d
        )
    return ids


async def located_ids(db) -> Set[str]:
    """Every device_id that has EVER had a position recorded, even once.
    Paul's exception to "Never used the app" — we never demote someone we
    have previously located."""
    ids = set(
        d for d in await db.status_events.distinct(
            "device_id", {"latitude": {"$ne": None}},
        ) if d
    )
    ids.update(
        d for d in await db.device_status.distinct(
            "device_id", {"latitude": {"$ne": None}},
        ) if d
    )
    return ids


async def _latest_push_event(db, kind: str) -> Optional[datetime]:
    if kind == "trigger":
        query: Dict[str, Any] = {
            "$or": [{"kind": "trigger"}, {"kind": {"$exists": False}}]
        }
    else:
        query = {"kind": kind}
    rows = await db.push_events.find(
        query, {"_id": 0, "created_at": 1},
    ).sort("created_at", -1).to_list(1)
    return parse_dt(rows[0]["created_at"]) if rows else None


async def last_alert_at(db) -> Optional[datetime]:
    """When the most recent alert went out. It is what separates
    "Waiting for an answer" from a record nobody has asked anything."""
    return await _latest_push_event(db, "trigger")


async def incident_is_active(db) -> bool:
    """A live alert = most recent trigger in push_events with no
    stand-down after it, inside the 72h window. Same definition
    /admin/incident-status uses for the idle timer, deliberately: one
    definition of "live" across the whole product."""
    async def _latest(kind: str) -> Optional[datetime]:
        return await _latest_push_event(db, kind)

    trigger = await _latest("trigger")
    if not trigger:
        return False
    if (datetime.now(timezone.utc) - trigger) > timedelta(hours=72):
        return False
    stand_down = await _latest("alert_stood_down")
    return not (stand_down and stand_down > trigger)
