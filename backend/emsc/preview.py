"""EMSC preview-mode notification pipeline.

Sends REAL notifications to an allowlisted device (Paul's phone) for
EMSC/USGS events that would have fired at a configured tier — while
shadow_mode remains true for every other device on the platform.

Why this exists (from the 2026-08-06 design conversation):
  Shadow mode validates DETECTION but not DELIVERY. We currently have
  zero evidence that "EMSC reports an event → notification appears on
  a real iPhone lock screen" works end to end. Discovering that on
  go-live day is the worst possible timing. Preview mode also turns
  threshold-tuning from guesswork into Paul's lived experience — the
  point at which the notification frequency annoys him is real
  calibration data.

Non-negotiable constraints (locked 2026-08-06):
  1. NEVER the Critical Alerts / siren path. Ordinary interruption
     level ONLY. Misusing the critical entitlement risks Apple
     revoking it — product-ending.
  2. Visibly labelled as a test. Notification prefix `PREVIEW · ` and
     suffix `. Test notification, no action needed.` Prevents alert
     fatigue on the channel we need Paul to trust in a real emergency.
  3. Audit-tagged distinctly (`emsc_preview_notifications` collection),
     never conflated with real triggers in `push_events`.
  4. Rate limit ~1 per 10 min per device — swarm sequences shouldn't
     produce 50 notifications in an hour.
  5. One-tap off via admin kill switch (`POST /api/admin/emsc/preview/kill`).
  6. Cross-device isolation: the allowlist is explicit; no other device
     receives anything from this pipeline.

Config lives on the country_config document under `preview_mode`:
  {
    "enabled": bool,
    "device_ids": ["qg-xxx", ...],
    "trigger_tier": "all_ingested" | "quiet_tier" | "critical_tier",
    "rate_limit_minutes": 10,
  }
"""
from __future__ import annotations

import logging
import math
import uuid
from datetime import datetime, timedelta, timezone
from typing import List, Optional


log = logging.getLogger(__name__)


# ── Direction / bearing helpers ──────────────────────────────────────────
_COMPASS_16 = [
    "N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
    "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW",
]


def bearing_deg(from_lat: float, from_lon: float, to_lat: float, to_lon: float) -> float:
    """Compass bearing in degrees from the FIRST point to the SECOND.
    Standard great-circle initial bearing formula. Returns 0-360."""
    lat1 = math.radians(from_lat)
    lat2 = math.radians(to_lat)
    dlon = math.radians(to_lon - from_lon)
    x = math.sin(dlon) * math.cos(lat2)
    y = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
    brg = math.degrees(math.atan2(x, y))
    return (brg + 360.0) % 360.0


def compass_16(bearing: float) -> str:
    """Convert 0-360 bearing to 16-point compass abbreviation."""
    idx = int((bearing + 11.25) // 22.5) % 16
    return _COMPASS_16[idx]


def format_body(
    magnitude: float,
    distance_km: float,
    depth_km: Optional[float],
    bearing_from_country: float,
    country_name: str,
) -> str:
    """Human-readable body text for a preview notification.

    Format: `M3.4 tremor — 62km SE of Malta, depth 11km. Test notification, no action needed.`
    """
    direction = compass_16(bearing_from_country)
    depth_str = f", depth {int(round(depth_km))}km" if depth_km is not None else ""
    return (
        f"M{magnitude:g} tremor — {int(round(distance_km))}km "
        f"{direction} of {country_name}{depth_str}. "
        f"Test notification, no action needed."
    )


# ── Trigger-tier evaluation ──────────────────────────────────────────────
def should_send_preview(
    evaluations: List[dict],
    country_code: str,
    trigger_tier: str,
) -> bool:
    """Given the event's evaluations list and a country's configured
    trigger_tier, return True iff a preview notification should fire.

    Rules:
      - `all_ingested`: fire for any event that got stored (i.e., any event
        the poll cutoffs let through). We return True unconditionally.
      - `quiet_tier` / `critical_tier` / any threshold_set name: fire only
        if the evaluation for THIS country and THAT tier has would_have_fired=True.
      - Unknown tier name: log warning, return False (safe default).
    """
    if trigger_tier == "all_ingested":
        return True
    for e in evaluations or []:
        if (e.get("country_code") == country_code and
            e.get("threshold_set") == trigger_tier and
            e.get("would_have_fired") is True):
            return True
    return False


# ── Rate limiting ────────────────────────────────────────────────────────
async def _recently_notified(
    db, device_id: str, rate_limit_minutes: int, now: datetime,
) -> bool:
    """True if this device received ANY preview notification within the
    last `rate_limit_minutes` minutes. Ordering doesn't matter — a swarm
    sequence must not flood a single device, so any preview counts."""
    cutoff = now - timedelta(minutes=rate_limit_minutes)
    row = await db.emsc_preview_notifications.find_one(
        {"device_id": device_id, "sent_at": {"$gte": cutoff}, "delivered": True},
        {"_id": 1},
    )
    return row is not None


# ── Main entry point ────────────────────────────────────────────────────
async def dispatch_preview_if_needed(
    *,
    db,
    apns_send_preview,           # callable: send_preview_alerts from apns.py
    emsc_event: dict,             # freshly-stored emsc_events row (dict, not RawEvent)
    country_config: dict,         # country_configs row
) -> Optional[dict]:
    """Called by the poller AFTER a new/revised emsc_events row lands.
    Decides whether to send a preview to any allowlisted device on this
    country_config, and if so, dispatches + logs the outcome.

    Returns None if nothing was sent (config disabled, tier didn't match,
    no eligible devices, all rate-limited); returns a summary dict if
    at least one send was attempted.
    """
    preview_cfg = (country_config or {}).get("preview_mode") or {}
    if not preview_cfg.get("enabled"):
        return None

    device_ids = list(preview_cfg.get("device_ids") or [])
    if not device_ids:
        return None

    trigger_tier = preview_cfg.get("trigger_tier") or "all_ingested"
    country_code = country_config.get("country_code")

    # Evaluate whether the tier's rule matched for THIS event.
    if not should_send_preview(
        evaluations=emsc_event.get("evaluations") or [],
        country_code=country_code,
        trigger_tier=trigger_tier,
    ):
        return None

    now = datetime.now(timezone.utc)
    rate_limit_minutes = int(preview_cfg.get("rate_limit_minutes") or 10)

    # Fetch push_devices rows for the allowlisted device_ids that
    # currently have iOS tokens. Preview mode is iOS-only for v1 (Paul's
    # device); Android will need its own path later via the Emergent
    # push relay.
    push_devices = await db.push_devices.find(
        {
            "user_id": {"$in": device_ids},
            "platform": {"$in": ["ios", "iOS"]},
            "device_token": {"$exists": True, "$ne": None},
        },
        {"_id": 0, "user_id": 1, "device_token": 1},
    ).to_list(50)

    if not push_devices:
        # Log a skipped row so operators can debug "why didn't I get it".
        await db.emsc_preview_notifications.insert_one({
            "sent_at": now,
            "device_id": None,
            "delivered": False,
            "skipped_reason": "no_push_devices_matched_allowlist",
            "emsc_event_ref": {"provider": emsc_event.get("provider"),
                               "external_id": emsc_event.get("external_id"),
                               "revision": emsc_event.get("revision")},
            "country_code": country_code,
            "trigger_tier": trigger_tier,
            "allowlist_size": len(device_ids),
        })
        return {"attempted": 0, "reason": "no_matching_devices"}

    # Compute the direction/distance chunk. We use the first matching
    # evaluation for this country to get an authoritative distance number.
    country_center = country_config.get("center") or {}
    c_lat = country_center.get("lat")
    c_lon = country_center.get("lon")
    distance_km = None
    for ev in emsc_event.get("evaluations") or []:
        if ev.get("country_code") == country_code:
            distance_km = ev.get("distance_km")
            break

    bearing = None
    if c_lat is not None and c_lon is not None:
        bearing = bearing_deg(
            c_lat, c_lon,
            emsc_event.get("latitude"), emsc_event.get("longitude"),
        )

    title = "PREVIEW · Seismic activity"
    body = format_body(
        magnitude=emsc_event.get("magnitude") or 0.0,
        distance_km=distance_km or 0.0,
        depth_km=emsc_event.get("depth_km"),
        bearing_from_country=bearing if bearing is not None else 0.0,
        country_name=country_config.get("country_name") or country_code,
    )

    # Rate-limit filter: only devices that haven't been notified in the
    # last N minutes get a real send. Others get logged as skipped.
    eligible: List[dict] = []
    skipped: List[dict] = []
    for pd in push_devices:
        did = pd.get("user_id") or ""
        if not did:
            continue
        limited = await _recently_notified(db, did, rate_limit_minutes, now)
        if limited:
            skipped.append(pd)
        else:
            eligible.append(pd)

    # Log the rate-limited ones so the volume is honestly visible.
    for pd in skipped:
        await db.emsc_preview_notifications.insert_one({
            "sent_at": now,
            "device_id": pd.get("user_id"),
            "delivered": False,
            "skipped_reason": "rate_limited",
            "rate_limit_minutes": rate_limit_minutes,
            "emsc_event_ref": {"provider": emsc_event.get("provider"),
                               "external_id": emsc_event.get("external_id"),
                               "revision": emsc_event.get("revision")},
            "country_code": country_code,
            "trigger_tier": trigger_tier,
        })

    if not eligible:
        return {"attempted": 0, "skipped_rate_limited": len(skipped), "reason": "all_rate_limited"}

    # Fire the pushes. NON-CRITICAL path — see apns._build_preview_payload.
    # Payload embeds `kind: "emsc_preview"` and full event details so the
    # mobile-app tap handler routes to /quake/[unid] (informational) and
    # NEVER to /alert (siren). This is the fix for BUG-2026-08-06-preview-tap-siren.
    idem = f"emsc-preview-{uuid.uuid4()}"
    unid = emsc_event.get("external_id")
    observed_at = emsc_event.get("observed_at")
    observed_at_iso = observed_at.isoformat() if hasattr(observed_at, "isoformat") else str(observed_at) if observed_at else None
    try:
        result = await apns_send_preview(
            db=db,
            devices=eligible,
            title=title,
            body=body,
            # action_url points at the preview detail screen — even if
            # kind is stripped by an intermediary, action_url routes
            # away from /alert as a second line of defence.
            action_url=f"/quake/{unid}" if unid else "/",
            idempotency_key=idem,
            magnitude=emsc_event.get("magnitude"),
            distance_km=distance_km,
            depth_km=emsc_event.get("depth_km"),
            latitude=emsc_event.get("latitude"),
            longitude=emsc_event.get("longitude"),
            region=emsc_event.get("region"),
            unid=unid,
            provider=emsc_event.get("provider"),
            observed_at=observed_at_iso,
        )
    except Exception as e:
        log.warning("Preview APNs send raised: %s", e)
        result = {"payload": None, "events": [
            {"user_id": d.get("user_id"), "delivered": False,
             "error": str(e)} for d in eligible
        ]}

    # Log every send attempt outcome into the audit-distinct collection.
    for evt in (result.get("events") or []):
        await db.emsc_preview_notifications.insert_one({
            "sent_at": now,
            "device_id": evt.get("user_id"),
            "delivered": bool(evt.get("delivered")),
            "apns_id": evt.get("apns_id"),
            "reason": evt.get("reason"),
            "error": evt.get("error"),
            "status_code": evt.get("status_code"),
            "environment": evt.get("environment"),
            "emsc_event_ref": {"provider": emsc_event.get("provider"),
                               "external_id": emsc_event.get("external_id"),
                               "revision": emsc_event.get("revision")},
            "country_code": country_code,
            "trigger_tier": trigger_tier,
            "title": title,
            "body": body,
            "idempotency_key": idem,
        })

    delivered_count = sum(1 for e in (result.get("events") or []) if e.get("delivered"))
    return {
        "attempted": len(eligible),
        "delivered": delivered_count,
        "skipped_rate_limited": len(skipped),
        "title": title,
        "body": body,
    }
