"""Single source of truth for "how many people are in each state right now".

Created 2026-08-19 in response to Batch 7 A2. Before this, four separate
call sites each derived counts independently:

  * /api/public/summary          (server.py:603)  — unfiltered, no test flag
  * /api/devices                 (server.py:365)  — client-side test filter
  * /api/admin/recheck/status    (routes_recheck.py:141) — unfiltered, count_documents
  * PDF _bucket_by_status        (reports_export.py:1371) — status_events-derived

That produced the "3 vs 1 vs 0 for the same person" defect Paul reported
(Batch 7 A2). Every count you show anywhere in the product now goes
through `compute_counts()` here, and `include_test` is the ONE knob.

Design decisions
----------------
1. **Current-state authority = `device_status`.** The event log is for
   history and narrative ("N told us they were trapped THIS AFTERNOON"),
   never for "how many are trapped right now." A person trapped before
   the report window is still trapped now, and the live dashboard, the
   PDF aggregate table, and the re-check panel must all agree with each
   other on that.
2. **Rescued wins.** If `rescued_at` is set, the row's effective status
   is "rescued" no matter what the raw `status` field says. This
   removed the map-marker duplication defect (green rescued tick
   overlapping the amber trapped triangle) — one row, one status.
3. **`include_test` is a single parameter, defaulted to False.** Test
   entries are never silently dropped from a legal record — the
   operational read excludes them, the raw audit read includes them,
   and both are labelled.
4. **No environment lookups here.** The module reads from the passed-in
   Mongo handle and nothing else, so it can be unit-tested against a
   fake DB without touching os.environ or dotenv.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict, field, replace
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from deps import is_test_device


# ── Effective status derivation ────────────────────────────────────────
# Called from THIS module and also exposed for the dashboard-side map
# marker renderer, so "one row, one status" holds on the front-end too.
def effective_status(row: Dict[str, Any]) -> str:
    """Rescued wins. Otherwise use the raw status. 'unknown' as fallback.

    This is the single classifier every caller must use — never look at
    row['status'] directly for display. See A2 Pattern 2 (two parts
    stating different facts): the map's rescued layer and severity layer
    were both reading row['status'] and independently deciding whether
    to draw, so a person marked rescued still got their trapped triangle
    drawn underneath.
    """
    if row.get("rescued_at"):
        return "rescued"
    st = row.get("status")
    return st if st in ("safe", "trapped", "not_responding", "rescued") else "unknown"


# ── Result shape ───────────────────────────────────────────────────────
@dataclass(frozen=True)
class Counts:
    """Immutable snapshot. Fields are stable — the dashboard, the PDFs
    and any external caller can rely on this shape."""
    total: int
    safe: int
    trapped: int
    trapped_red: int
    trapped_yellow: int
    trapped_green: int
    trapped_unknown: int
    rescued: int
    not_responding: int
    unknown: int
    # People currently marked as needing help (== trapped_total). The
    # re-check panel wants this named explicitly so the code reads
    # right ("if counts.needs_help > 0 ...").
    needs_help: int
    # How many test entries were filtered out to produce these numbers.
    # Returned so the dashboard can show "2 test entries hidden" when
    # the operator has "Show test entries" unchecked.
    test_filtered_out: int
    # Whether test entries were included (for stated provenance).
    include_test: bool
    # ── #268 (2026-08-21 — Paul): the four kinds of silence ───────────
    # "Every number must say what it counts and what it leaves out."
    # These five are counted on the WORKING BOARD ONLY, except
    # app_removed / never_used / resolved which are BY DEFINITION off it.
    # `not_responding` above therefore excludes all three — that is the
    # phantom-casualty fix, and `counts_notes()` states it in words
    # wherever a number is shown.
    waiting_for_answer: int = 0
    phone_went_dark: int = 0
    # #276: of the people we asked and have not heard from, how many
    # confirmed on their own phone that the question arrived. Those are
    # the worrying ones; the rest may simply never have seen it.
    no_answer: int = 0
    app_removed: int = 0
    # Of those, how many are still ON the working board because an alert
    # is live and they have not answered (Paul's rule 1 beats rule 5, but
    # they are still never counted as "not responding").
    app_removed_held_on_board: int = 0
    never_used: int = 0
    resolved_by_operator: int = 0
    off_board_total: int = 0
    # #283 (2026-08-24 — Paul): the board used to count these itself, in
    # JavaScript, from the rows it happened to be showing — so the stat
    # box and the sentence under it disagreed. Counted here instead, with
    # everything else, from the same rows.
    walking_wounded: int = 0
    # #283 (2026-08-25 — Paul): the sentence explaining where the quiet
    # people sit used to name three categories from memory — "Safe,
    # Trapped and Not responding" — and they only added to 2 of 7,
    # because it never said "Rescued". Counted here instead of written by
    # hand, so the sentence can only ever name the categories the quiet
    # people are actually in. Keys are the effective_status values;
    # `quiet_rescued` is pulled out because Paul asked whether an
    # already-rescued person's silence should worry anybody (it should
    # not, and the note now says so).
    quiet_by_status: Dict[str, int] = field(default_factory=dict)
    quiet_rescued: int = 0

    def to_dict(self) -> Dict[str, int]:
        return asdict(self)


# ── The one function ───────────────────────────────────────────────────
async def compute_counts(db, include_test: bool = False) -> Counts:
    """Aggregate counts across all devices. Read this, and nothing else.

    Args:
      db: Motor Mongo handle.
      include_test: If True, test/synthetic devices are counted alongside
        real ones. Default False — the operator-facing and public views
        both hide test entries by default.

    Returns:
      Counts (see dataclass). All fields are integers.
    """
    board = await load_board(db, include_test=include_test)
    return board.counts


async def compute_people(db, include_test: bool = False) -> List[Dict[str, Any]]:
    """Per-person rows, with the same test filter applied. Used by
    /api/devices — kept in this module so the filter decision lives in
    exactly one place, no matter what the row is used for.

    Returns the raw dicts (with `is_test` and `effective_status` added).
    Consumers are expected to project their own view fields.
    """
    rows = await _load_rows(db)
    out: List[Dict[str, Any]] = []
    for r in rows:
        r["is_test"] = is_test_device(r)
        r["effective_status"] = effective_status(r)
        if r["is_test"] and not include_test:
            continue
        out.append(r)
    return out


# ── #268: the working board, and what is deliberately not on it ───────
@dataclass(frozen=True)
class Board:
    """One load of the whole picture. `/api/devices`, every count, the
    PDFs and the CSVs all read THIS, so no two surfaces can disagree
    about who is on the working board.

    board      — records an operator works from.
    off_board  — records deliberately NOT on it, each carrying the reason
                 and, where a human decided, who decided and when.
                 Visible, openable, never deleted, never hidden.
    notices    — board-level plain-English notices (e.g. mass-dark).
    notes      — "what this counts and what it leaves out", in words, for
                 every surface that shows a number.
    """
    board: List[Dict[str, Any]]
    off_board: List[Dict[str, Any]]
    counts: Counts
    notices: List[Dict[str, Any]]
    notes: List[str]
    # #283: the same rows counted with test entries left out, so a
    # consumer showing "real people only" never has to recount.
    counts_without_test: Optional[Counts] = None
    notes_without_test: Optional[List[str]] = None


async def load_board(db, include_test: bool = False, now=None) -> Board:
    """Classify every record once, split the working board from the
    labelled off-board area, and count both.

    Ordering matters and is asserted by tests/test_record_state_268.py:
    help history is resolved from the append-only ledger BEFORE any
    device signal is consulted, so "status outranks device state" cannot
    be broken by a later edit to the token-handling code.
    """
    import record_state as rs
    from duplicates import find_duplicate_candidates

    now = now or datetime.now(timezone.utc)
    rows = await _load_rows(db)

    push_rows = await db.push_devices.find(
        {}, {"_id": 0, "user_id": 1, "platform": 1, "created_at": 1,
             "updated_at": 1, "dead_token": 1, "dead_token_reason": 1,
             "dead_token_at": 1},
    ).to_list(10000)
    push_by_id = {str(p.get("user_id")): p for p in push_rows if p.get("user_id")}

    help_ids = await rs.help_history_ids(db)
    located = await rs.located_ids(db)
    incident_active = await rs.incident_is_active(db)
    from reports_export import _short_codes_for
    last_alert_at = await rs.last_alert_at(db)

    # Registered for alerts but never checked in — invisible before #268
    # because the board only ever read device_status. They are real people
    # whose phones received the siren and who did not answer, so they get
    # a record, a state and a spoken count.
    known = {str(r.get("device_id")) for r in rows}
    for uid, p in push_by_id.items():
        if uid not in known:
            rows.append({
                "device_id": uid,
                "platform": p.get("platform"),
                "created_at": p.get("created_at"),
                "never_checked_in": True,
            })

    code_map = _short_codes_for([r.get("device_id") for r in rows])

    kept: List[Dict[str, Any]] = []
    filtered = 0
    for r in rows:
        r["is_test"] = is_test_device(r)
        if r["is_test"] and not include_test:
            filtered += 1
            continue
        did = str(r.get("device_id"))
        r["short_code"] = code_map.get(r.get("device_id"))
        r["effective_status"] = effective_status(r)
        r["ever_needed_help"] = did in help_ids or rs.ever_needed_help_row(r)
        r["record_state"] = rs.classify(
            r, push_by_id.get(did),
            ever_needed_help=r["ever_needed_help"],
            ever_located=did in located,
            incident_active=incident_active,
            last_alert_at=last_alert_at,
            now=now,
        ).to_dict()
        kept.append(r)

    # Duplicate SUGGESTIONS only — never a merge, never an auto-resolve.
    # Test entries are excluded: they are not people, and a bulk seed
    # inserts dozens of rows at the same instant and the same coordinates,
    # which would otherwise flood the board with suggestions and train an
    # operator to dismiss the real ones.
    decisions = await _latest_decisions(db)
    flags = find_duplicate_candidates(
        [r for r in kept if not r.get("is_test")], decisions,
    )
    for r in kept:
        flag = flags.get(str(r.get("device_id")))
        if flag:
            r["possible_duplicate"] = flag

    board = [r for r in kept if r["record_state"]["on_working_board"]]
    off_board = [r for r in kept if not r["record_state"]["on_working_board"]]

    counts = _tally(board, off_board,
                    include_test=include_test, already_filtered=filtered)
    # #283: the same rows, counted twice on purpose — once as the operator
    # works (real people only) and once including test entries. Both are
    # returned so no consumer has to recount anything to answer "how many
    # of those are real?", which is how the board and the sentence under
    # it came to disagree.
    counts_without_test = (
        counts if not include_test
        else _tally(board, off_board, include_test=False, already_filtered=0)
    )

    # Mass-dark: many phones stopping at roughly the same moment is a
    # network or power failure, not many people going missing at once.
    notices: List[Dict[str, Any]] = []
    # #276: a confirmed-but-unanswered question is NOT evidence of a
    # network failure — the phone plainly had a network. Only the
    # unconfirmed silences feed the mass-dark test.
    dark_stamps = [r.get("updated_at") for r in board
                   if (r["record_state"]["state"]) == rs.DARK]
    reporting_total = sum(1 for r in board if r.get("updated_at"))
    notice = rs.detect_mass_dark(dark_stamps, reporting_total, now=now)
    if notice:
        notices.append(notice)

    return Board(
        board=board, off_board=off_board, counts=counts,
        notices=notices, notes=counts_notes(counts),
        counts_without_test=counts_without_test,
        notes_without_test=counts_notes(counts_without_test),
    )


def _tally(
    board_rows: List[Dict[str, Any]],
    off_board_rows: List[Dict[str, Any]],
    *,
    include_test: bool,
    already_filtered: int,
) -> Counts:
    """Every number on every surface, built once from the same rows.

    #283 (2026-08-24 — Paul): three surfaces were each doing their own
    sum — the call-off toast, the sentence under the dashboard's stat
    boxes, and the team PDF's breakdown — and all three disagreed with
    the box beside them. A wrong number on a life-safety screen is a
    false fact, so there is now exactly one function that produces them
    and it is called with the population you want, never re-derived.

    `already_filtered` carries test rows dropped BEFORE classification
    (load_board does that when include_test is False) so the "N test
    entries hidden" figure stays truthful either way.
    """
    import record_state as rs

    def _keep(rows):
        return rows if include_test else [r for r in rows if not r.get("is_test")]

    board = _keep(board_rows)
    off_board = _keep(off_board_rows)
    filtered = (
        already_filtered
        + (len(board_rows) - len(board))
        + (len(off_board_rows) - len(off_board))
    )

    counts = _bucket(board, include_test=True)
    st_of = lambda r: r["record_state"]["state"]  # noqa: E731
    counted = lambda r: r["record_state"].get("count_in_status_buckets") is not False  # noqa: E731
    # #283: which status buckets do the quiet people actually sit in? Read
    # off the rows rather than remembered in a sentence.
    quiet_rows = [
        r for r in board
        if st_of(r) in (rs.WAITING, rs.DARK, rs.NO_ANSWER) and counted(r)
    ]
    quiet_by_status: Dict[str, int] = {}
    for r in quiet_rows:
        st = effective_status(r)
        quiet_by_status[st] = quiet_by_status.get(st, 0) + 1
    # Held on the board because an alert is live, but the phone has told us
    # the app is gone. Counted here, and never inside "not responding".
    held_removed = [
        r for r in board
        if r["record_state"].get("app_removed_at") and not counted(r)
    ]
    return replace(
        counts,
        include_test=include_test,
        test_filtered_out=filtered,
        waiting_for_answer=sum(
            1 for r in board if st_of(r) == rs.WAITING and counted(r)),
        phone_went_dark=sum(
            1 for r in board if st_of(r) == rs.DARK and counted(r)),
        no_answer=sum(
            1 for r in board if st_of(r) == rs.NO_ANSWER and counted(r)),
        app_removed=(sum(1 for r in off_board if st_of(r) == rs.APP_REMOVED)
                     + len(held_removed)),
        app_removed_held_on_board=len(held_removed),
        never_used=sum(1 for r in off_board if st_of(r) == rs.NEVER_USED),
        resolved_by_operator=sum(1 for r in off_board if st_of(r) == rs.RESOLVED),
        off_board_total=len(off_board),
        walking_wounded=sum(1 for r in board if is_walking_wounded(r)),
        quiet_by_status=quiet_by_status,
        quiet_rescued=quiet_by_status.get("rescued", 0),
    )


def is_walking_wounded(row: Dict[str, Any]) -> bool:
    """Someone who reported an injury but can get themselves out.

    They stay inside the trapped total — they did report an injury — and
    are also counted separately, because they are the number who will
    need treating somewhere rather than digging out (#297-agent).

    #289 (2026-08-24 — Paul): "we do not know whether they can get out"
    is NOT walking wounded. A person who chose MINOR and then never
    answered the way-out question used to land in the lowest-priority
    list on an assumption. They stay on the working board until somebody
    knows.
    """
    if effective_status(row) != "trapped":
        return False
    sev = (row.get("severity") or "").lower()
    if sev not in ("", "green"):
        return False
    if row.get("needs_extraction"):
        return False
    if (row.get("egress") or "") == "not_answered":
        return False
    return (row.get("mobility") or "") != "trapped"


def moved_by_words(row: Dict[str, Any]) -> str:
    """Who took this record off the working board, in plain words. One
    wording for the dashboard, the PDF and the CSV — an inquiry must not
    find three different answers to the same question."""
    st = row.get("record_state") or {}
    if row.get("resolved_by"):
        return str(row["resolved_by"])
    if st.get("state") == "app_removed":
        return "the phone itself — Apple reported the app is no longer installed"
    if st.get("state") == "never_used":
        return "nobody — this record has never been on the working board"
    # #292: one phrase for the unknown, on every surface.
    return "Not known"


def counts_notes_short(c: Counts) -> str:
    """One sentence, for the public one-page report (#126 keeps B2 to a
    single page, and it is read by families and journalists). Says the
    same thing as counts_notes, shorter."""
    excluded = c.app_removed + c.never_used + c.resolved_by_operator
    if not excluded:
        return "These numbers cover everyone on the working rescue board."
    return (
        "These numbers cover the working rescue board only. They leave out "
        f"{excluded} set-aside "
        + ("record" if excluded == 1 else "records")
        + ": app removed, app never used, or taken off by an operator. "
        "Nothing was deleted."
    )


_STATUS_WORDS = {
    "safe": "Safe",
    "trapped": "Needing help",
    "not_responding": "Not responding",
    "rescued": "Rescued",
    "unknown": "Not known yet",
}


def _quiet_spread_words(c: Counts) -> str:
    """"…spread across Safe, Rescued and Not responding." (#283)

    Paul, 2026-08-25: "those three only add to 2." The sentence used to
    name three categories from memory and left out Rescued, so the
    breakdown it offered did not add up to the number in front of it — on
    a page whose whole job is that the numbers agree.

    Now it names the categories the quiet people are ACTUALLY in, with
    the number in each, taken from the same rows as every other figure.
    """
    parts = [
        f"{n} {_STATUS_WORDS.get(st, st)}"
        for st, n in sorted(c.quiet_by_status.items(), key=lambda kv: -kv[1])
        if n
    ]
    if not parts:
        return "inside the status numbers above."
    if len(parts) == 1:
        return f"inside {parts[0]}."
    return "spread across " + ", ".join(parts[:-1]) + " and " + parts[-1] + "."


def counts_notes(c: Counts) -> List[str]:
    """"Every number must say what it counts and what it leaves out."

    Short lines, one idea each, plain words — Paul is dyslexic and asked
    for both the dashboard and the app to read simply. See
    memory/writing-and-layout-rules.md before changing any of this. These
    exact sentences ship with the numbers on the dashboard, in the team
    PDF and in the audit CSV, so there is one wording everywhere.
    """
    people = "person" if c.not_responding == 1 else "people"
    notes = [
        f"Not responding: {c.not_responding} {people} on the working board.",
        "That number leaves out records we have set aside. They are listed "
        "under \u201cNot on the working board\u201d.",
    ]
    # #283 (2026-08-24 — Paul): these three used to be written by hand on
    # the dashboard, under a sentence claiming "all three are counted in
    # the numbers above" — which read as a breakdown of the box beside
    # them and never matched it. Silence is not a status: a person who
    # reported safe an hour ago and has said nothing since is quiet AND
    # safe. So the lines now say which numbers they sit inside, and say
    # out loud that they are not extra people.
    quiet = c.waiting_for_answer + c.no_answer + c.phone_went_dark
    if quiet:
        notes.append(
            f"Gone quiet: {quiet} of the {c.total} "
            + ("person" if c.total == 1 else "people")
            + " on the board."
        )
        notes.append(f"\u2014 waiting for an answer: {c.waiting_for_answer}.")
        notes.append(f"\u2014 got our question, no answer: {c.no_answer}.")
        notes.append(
            "\u2014 we asked, no answer, and their phone never confirmed "
            f"our question arrived: {c.phone_went_dark}."
        )
        notes.append(
            "Those "
            + ("one is" if quiet == 1 else f"{quiet} are")
            + " already counted above, "
            + _quiet_spread_words(c)
            + " They are not extra people."
        )
        if c.quiet_rescued:
            n = c.quiet_rescued
            notes.append(
                f"\u2014 {n} of them "
                + ("has" if n == 1 else "have")
                + " already been rescued, so their silence is not a worry. "
                + ("That one is" if n == 1 else "Those are")
                + " listed here only so the numbers add up."
            )
    if c.app_removed:
        thing = "record" if c.app_removed == 1 else "records"
        notes.append(
            f"Set aside: {c.app_removed} {thing} where the phone said the app "
            "was removed. A removed app is not a missing person."
        )
    if c.app_removed_held_on_board:
        n = c.app_removed_held_on_board
        notes.append(
            f"{n} of those {'is' if n == 1 else 'are'} still shown on the "
            "board, because an alert is live and they have not answered."
        )
    if c.never_used:
        n = c.never_used
        notes.append(
            f"Set aside: {n} {'phone' if n == 1 else 'phones'} that got the "
            "alert but never used the app. We have no place for them."
        )
    if c.resolved_by_operator:
        n = c.resolved_by_operator
        notes.append(
            f"Set aside: {n} {'record' if n == 1 else 'records'} an operator "
            "took off the board, with a reason."
        )
    notes.append("Nothing is ever deleted. You can put any record back.")
    return notes


async def _latest_decisions(db) -> Dict[str, Dict[str, Any]]:
    """Newest duplicate decision per device_id. A rejected suggestion must
    not come back on the next 4-second poll and re-ask the same question."""
    out: Dict[str, Dict[str, Any]] = {}
    try:
        rows = await db.record_decisions.find(
            {"kind": {"$in": ["duplicate_confirmed", "duplicate_rejected"]}},
            {"_id": 0},
        ).sort("decided_at", -1).to_list(5000)
    except Exception:
        return out
    for r in rows:
        did = str(r.get("device_id"))
        out.setdefault(did, r)
        other = str(r.get("other_device_id") or "")
        if other:
            out.setdefault(other, {**r, "device_id": other,
                                   "other_device_id": r.get("device_id")})
    return out


# ── Internals ──────────────────────────────────────────────────────────
async def _load_rows(db) -> List[Dict[str, Any]]:
    """Fetch every device_status row once. The projection includes just
    enough for classification and downstream display fields — full row
    fetches for detail views go through the existing /api/devices code.
    """
    return await db.device_status.find(
        {},
        # NOTE: keep this list minimal. Anything a caller doesn't need
        # is a potential leak (see the 2026-08-04 notes incident).
        {
            "_id": 0,
            "device_id": 1, "display_name": 1,
            "status": 1, "severity": 1, "mobility": 1, "egress": 1,
            "needs_extraction": 1,
            # #185: group_size at this address — surfaced on the map pin
            # and details for a rescuer, NEVER summed into any count.
            "group_size": 1,
            "latitude": 1, "longitude": 1, "accuracy_m": 1,
            "battery_pct": 1, "battery_state": 1, "platform": 1,
            "updated_at": 1,
            "rescued_at": 1, "rescued_by": 1,
            "pre_rescue_status": 1, "pre_rescue_severity": 1, "pre_rescue_mobility": 1,
            "synthetic": 1,
            "recheck": 1, "deteriorating": 1, "reports_improving": 1,
            # #271: what WE have asked this phone, and when. record_state
            # reads this to tell "we asked and heard nothing" apart from
            # "nothing has asked them anything".
            "asks": 1,
            # #268: help history and the human-resolution fields. Both are
            # needed by record_state.classify — without them the "status
            # outranks device state" guarantee cannot be evaluated.
            "trapped_since": 1, "created_at": 1,
            "resolved_at": 1, "resolved_by": 1, "resolved_reason": 1,
            "resolved_as": 1, "is_test": 1,
            # #268: the durable copy of "the phone told us the app is gone".
            "app_removed_at": 1, "app_removed_source": 1,
        },
    ).to_list(10000)


def _bucket(rows: List[Dict[str, Any]], *, include_test: bool) -> Counts:
    """Pure function — accepts rows and returns Counts. Extracted so
    tests can call it with fixed inputs and assert on the outputs,
    without setting up a Mongo instance.
    """
    kept = []
    filtered = 0
    for r in rows:
        # #268: a record that is not on the working board is not in ANY of
        # these buckets. Defensive — load_board only passes board rows —
        # but it means a future caller cannot accidentally count a deleted
        # app as a person who is not responding.
        st_meta = r.get("record_state") or {}
        if st_meta and st_meta.get("on_working_board") is False:
            continue
        # A record whose phone reported the app removed is counted in its
        # own named bucket, never inside "not responding" — even while it
        # is held on the working board because an alert is live.
        if st_meta.get("count_in_status_buckets") is False:
            continue
        if is_test_device(r):
            if not include_test:
                filtered += 1
                continue
        kept.append(r)

    total = len(kept)
    safe = trapped = rescued = not_responding = unknown = 0
    t_red = t_yellow = t_green = t_unknown = 0
    for r in kept:
        st = effective_status(r)
        if st == "safe":
            safe += 1
        elif st == "rescued":
            rescued += 1
        elif st == "trapped":
            trapped += 1
            sev = (r.get("severity") or "").lower()
            if sev == "red":
                t_red += 1
            elif sev == "yellow":
                t_yellow += 1
            elif sev == "green":
                t_green += 1
            else:
                t_unknown += 1
        elif st == "not_responding":
            not_responding += 1
        else:
            unknown += 1

    return Counts(
        total=total,
        safe=safe,
        trapped=trapped,
        trapped_red=t_red,
        trapped_yellow=t_yellow,
        trapped_green=t_green,
        trapped_unknown=t_unknown,
        rescued=rescued,
        not_responding=not_responding,
        unknown=unknown,
        needs_help=trapped,       # semantic alias
        test_filtered_out=filtered,
        include_test=include_test,
    )
