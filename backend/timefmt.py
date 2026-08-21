"""#272 (Neo, 2026-08-21 — Paul): one timezone, named, everywhere a
person reads a time.

The defect
----------
The F6XJY card said "Taken off the working board ... at 10:22" and,
directly beneath it, "Moved: 21/08/2026, 12:22:08". Two hours apart, in
one record. One was UTC (rendered by the backend) and one was the
browser's local time (rendered by the dashboard), and neither said which.
Paul: "This is a legal record that will be read back in an inquiry
alongside radio logs kept in local time. A two-hour discrepancy inside
one record makes the whole audit trail contestable."

The rule, decided 2026-08-21
----------------------------
* **Malta local time everywhere a person reads a time** — dashboard, both
  PDFs, CSV headings, the phone app. Named on screen, so nobody has to
  guess.
* **UTC only in export file names and in machine-readable columns.** File
  names end in `Z` so a file named at 10:22Z cannot be mistaken for
  12:22 local. Machine columns keep a full ISO timestamp WITH its offset,
  because a spreadsheet needs one and an offset can never be misread.
* **On legal records — the audit log and both PDFs — the offset is
  printed next to the local time** ("21:08 (Malta, UTC+02:00)"), so an
  inquiry reading it in another country cannot misread it.

Daylight saving — asked about explicitly, answered here
------------------------------------------------------
Malta is UTC+01:00 in winter and UTC+02:00 in summer, changing on the
last Sunday of March and October at 01:00 UTC.

How this code handles it: every timestamp we store is an *instant* in
UTC, and every human rendering is that instant converted through
`ZoneInfo("Europe/Malta")` at render time. That means:

* The conversion is always correct, including for events either side of a
  changeover — we never do arithmetic on local clock times, and we never
  parse a naive local string, so the ambiguous-hour problem cannot arise
  in our data.
* An incident running across the October changeover WILL show local times
  that appear to repeat (02:30 happens twice) and, in March, appear to
  jump (02:30 never happens). That is real, it is what the radio logs
  will also show, and it is why the offset is printed on the legal
  records: "02:30 (Malta, UTC+02:00)" and "02:30 (Malta, UTC+01:00)" are
  one hour apart and unambiguous.
* Ordering is never taken from the displayed local time. Every sort in
  the product is on the stored UTC instant.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

MALTA = ZoneInfo("Europe/Malta")

# What we call it on screen. One phrase, everywhere.
TZ_NAME = "Malta time"
TZ_SENTENCE = "All times are Malta time."


def parse(ts) -> Optional[datetime]:
    """Any stored timestamp -> an aware UTC datetime. Naive strings are
    treated as UTC, which is what every writer in this codebase stores."""
    if not ts:
        return None
    if isinstance(ts, datetime):
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    try:
        d = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def local(ts) -> Optional[datetime]:
    d = parse(ts)
    return d.astimezone(MALTA) if d else None


def hhmm(ts) -> str:
    """"21:08". Malta time. Used inside sentences on cards, where the
    date is obvious from context and repeating it would add noise."""
    d = local(ts)
    return d.strftime("%H:%M") if d else "an unknown time"


def day_month(ts) -> str:
    d = local(ts)
    return d.strftime("%d %b").lstrip("0") if d else "an unknown date"


def human(ts) -> str:
    """"21 Aug 2026, 21:08". Malta time. For lists and tables."""
    d = local(ts)
    return d.strftime("%d %b %Y, %H:%M").lstrip("0") if d else "—"


def offset_words(ts) -> str:
    """"UTC+02:00" for that instant — correct either side of a daylight
    saving change, because it is derived from the instant itself."""
    d = local(ts)
    if not d:
        return ""
    off = d.utcoffset()
    if off is None:
        return ""
    mins = int(off.total_seconds() // 60)
    sign = "+" if mins >= 0 else "-"
    return f"UTC{sign}{abs(mins) // 60:02d}:{abs(mins) % 60:02d}"


def legal(ts) -> str:
    """"21 Aug 2026, 21:08 (Malta time, UTC+02:00)". For the audit log and
    both PDFs — the records an inquiry reads, possibly in another
    country."""
    d = local(ts)
    if not d:
        return "—"
    return f"{human(ts)} ({TZ_NAME}, {offset_words(ts)})"


def machine(ts) -> str:
    """Full ISO 8601 with the offset, for machine-readable columns. Never
    shortened, never localised in words — a spreadsheet reads this."""
    d = parse(ts)
    return d.isoformat() if d else ""


def file_stamp(now: Optional[datetime] = None) -> str:
    """"20260821T093000Z" for export file names: UTC, sortable, and with
    the Z on it so nobody reads it as local time."""
    d = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return d.strftime("%Y%m%dT%H%M%SZ")
