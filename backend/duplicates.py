"""#268 (Neo, 2026-08-21 — Paul): the same person appearing twice.

"When someone reinstalls the app they get a new identity, and the old
record stays behind looking like a missing person. Two entries, one
human. In an incident that means a team searching for someone already
accounted for."

Hard rule, and the reason this module only ever SUGGESTS:
"Do not merge them automatically. Flag them ... An operator confirms or
rejects, and the decision is recorded with who made it and when. Never
let software decide two records are the same person."

So: no writes here, no merging anywhere, no data copied between records.
This module returns a suggestion plus the evidence that produced it, in
plain English, and stops. `record_decisions` (written by the endpoints in
server.py) holds the human answer, with who and when.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any, Dict, List, Optional

from record_state import dur_words, metres_between, parse_dt

# ── Evidence thresholds, and why ──────────────────────────────────────
# NEAR_METRES = 150. A reinstall happens where the person is standing, so
#   the two records' last positions should be within GPS noise of each
#   other. 150 m is roughly the worst civilian GPS fix indoors/urban
#   (accuracy_m on real check-ins here runs 5–250 m), so this is
#   deliberately generous: a missed suggestion is worse than a suggestion
#   an operator rejects in one tap.
# HANDOVER_MINUTES = 30. The old record must have gone quiet around the
#   time the new one appeared — that is the actual signature of a
#   reinstall, as opposed to two different people with the same first
#   name. 30 minutes covers "deleted the app, found the App Store,
#   reinstalled, opened it, checked in".
NEAR_METRES = 150.0
HANDOVER_MINUTES = 30


def _name(row: Dict[str, Any]) -> str:
    return str(row.get("display_name") or "").strip().casefold()


def find_duplicate_candidates(
    rows: List[Dict[str, Any]],
    decisions: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Dict[str, Dict[str, Any]]:
    """rows: device_status rows (need device_id, display_name, latitude,
    longitude, created_at, updated_at, short_code).

    Returns {device_id: suggestion} for BOTH records of a flagged pair —
    the operator must see the flag whichever card they are looking at.

    A pair is suggested when the handover timing holds AND at least one
    identifying piece of evidence holds:
      * the old record stopped reporting around when the new one first
        appeared (required — this is what makes it a reinstall rather
        than a coincidence), and
      * the same first name, or the last positions within 150 m.
    """
    decisions = decisions or {}
    out: Dict[str, Dict[str, Any]] = {}

    def already_decided(a: str, b: str) -> bool:
        for did in (a, b):
            d = decisions.get(did)
            if d and d.get("other_device_id") in (a, b):
                return True
        return False

    for new in rows:
        new_first = parse_dt(new.get("created_at"))
        if not new_first:
            continue
        best: Optional[Dict[str, Any]] = None
        for old in rows:
            if old.get("device_id") == new.get("device_id"):
                continue
            old_last = parse_dt(old.get("updated_at"))
            old_first = parse_dt(old.get("created_at"))
            if not old_last or not old_first:
                continue
            # The old record must predate the new one, and must not have
            # reported since the new one appeared — otherwise both phones
            # are live and they are two people.
            if old_first >= new_first or old_last > new_first:
                continue
            gap_min = int((new_first - old_last).total_seconds() // 60)
            if gap_min > HANDOVER_MINUTES:
                continue
            if already_decided(str(new.get("device_id")), str(old.get("device_id"))):
                continue

            evidence: List[str] = []
            same_name = bool(_name(new)) and _name(new) == _name(old)
            # Different first names, both recorded, is a veto — not weak
            # evidence. Two people standing in the same place with
            # different names must never be suggested as one person; an
            # operator who sees a bad suggestion stops trusting the good
            # ones, and this is the surface where trust matters most.
            if _name(new) and _name(old) and not same_name:
                continue
            if same_name:
                evidence.append(f"Same first name ({new.get('display_name')})")
            dist = metres_between(
                old.get("latitude"), old.get("longitude"),
                new.get("latitude"), new.get("longitude"),
            )
            near = dist is not None and dist <= NEAR_METRES
            if near:
                evidence.append(f"Last positions {int(round(dist))} m apart")
            if not (same_name or near):
                continue
            evidence.append(
                f"{old.get('short_code') or old.get('device_id')} last reported "
                f"{dur_words(gap_min)} before "
                f"{new.get('short_code') or new.get('device_id')} first appeared"
            )
            score = (1 if same_name else 0) + (1 if near else 0)
            if best is None or score > best["score"] or gap_min < best["gap_minutes"]:
                best = {
                    "score": score,
                    "gap_minutes": gap_min,
                    "old": old,
                    "new": new,
                    "evidence": evidence,
                }
        if not best:
            continue

        old, new_ = best["old"], best["new"]
        old_code = old.get("short_code") or old.get("device_id")
        new_code = new_.get("short_code") or new_.get("device_id")
        out[str(new_.get("device_id"))] = {
            "other_device_id": old.get("device_id"),
            "other_short_code": old_code,
            "other_display_name": old.get("display_name"),
            "role": "newer",
            "text": f"This may be the same person as {old_code}.",
            "evidence": list(best["evidence"]),
        }
        out[str(old.get("device_id"))] = {
            "other_device_id": new_.get("device_id"),
            "other_short_code": new_code,
            "other_display_name": new_.get("display_name"),
            "role": "older",
            "text": f"This may be the same person as {new_code}.",
            "evidence": list(best["evidence"]),
        }
    return out
