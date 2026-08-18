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


def iso_utc(value) -> Optional[str]:
    """ISO-8601 string that is UNAMBIGUOUSLY UTC.

    Motor returns naive datetimes for BSON dates, and
    `naive.isoformat()` yields "2026-08-18T08:07:10.750000" with no
    offset. ECMAScript parses an offset-less date-time as LOCAL time, so
    a Malta phone (CEST, UTC+2) rendered an 08:07 UTC quake as "08:07"
    instead of "10:07" — a silent two-hour error on the one timestamp a
    user compares against when the notification arrived, which made an
    11-minute delivery look like a three-hour one (Paul, 2026-08-18).

    Every timestamp we hand to a client must carry its offset.
    """
    if value is None:
        return None
    if not hasattr(value, "isoformat"):
        return str(value)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


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
    distance_km: Optional[float],
    poll_radius_km: Optional[float],
) -> tuple[bool, Optional[str]]:
    """Given the event's evaluations list, a country's configured
    trigger_tier, the event's distance from country center, and the
    country's radius cap, return (should_fire, skip_reason).

    Hard radius gate (fix for BUG-2026-08-06-preview-worldwide):
    Every tier — INCLUDING all_ingested — MUST honour the country's
    poll_radius_km. An event 10,834km away must never generate a Malta
    preview under any setting. The tier controls sensitivity WITHIN the
    region, never whether the region applies at all.

    Rules:
      - Beyond `poll_radius_km` → skip regardless of tier.
      - `all_ingested`: fire for every event inside the radius.
      - Named threshold_set: fire only if evaluation for THIS country
        and THAT tier has would_have_fired=True (evaluator already
        applied its own tighter distance cap inside the radius).
      - Unknown tier: skip (safe default).

    Returns (True, None) if we should fire, (False, reason_string) if not.
    reason_string is stored on the skip row in emsc_preview_notifications
    so operators can audit why previews didn't fire.
    """
    # Hard radius gate — applies to ALL tiers.
    if poll_radius_km is not None and distance_km is not None and distance_km > poll_radius_km:
        return False, f"beyond_country_radius ({distance_km:.0f}km > {poll_radius_km:.0f}km)"

    if trigger_tier == "all_ingested":
        return True, None

    for e in evaluations or []:
        if (e.get("country_code") == country_code and
            e.get("threshold_set") == trigger_tier and
            e.get("would_have_fired") is True):
            return True, None

    return False, f"tier_did_not_match ({trigger_tier})"


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

    # Compute distance from event to country center — needed by the
    # radius gate below, and by the notification body formatter later.
    # Preferred: read from an evaluation for this country (already
    # computed by the evaluator). Fallback: recompute from event coords.
    distance_km: Optional[float] = None
    for ev in emsc_event.get("evaluations") or []:
        if ev.get("country_code") == country_code and ev.get("distance_km") is not None:
            distance_km = ev.get("distance_km")
            break
    if distance_km is None:
        # Recompute — this covers the edge case where an event has no
        # evaluations at all (e.g., no threshold_sets configured for the
        # country). We still want the radius gate to work.
        country_center = country_config.get("center") or {}
        c_lat = country_center.get("lat")
        c_lon = country_center.get("lon")
        e_lat = emsc_event.get("latitude")
        e_lon = emsc_event.get("longitude")
        if c_lat is not None and c_lon is not None and e_lat is not None and e_lon is not None:
            from .evaluator import haversine_km
            distance_km = haversine_km(e_lat, e_lon, c_lat, c_lon)

    poll_radius_km = country_config.get("poll_radius_km")

    # ── Preview-only radius override (2026-08-07) ────────────────────────
    # An operator may temporarily widen the preview radius to test with
    # more events (e.g., 2000 km from Malta to catch Greek/Turkish quakes).
    # The override applies ONLY to the preview path — the real-alert path
    # (evaluator's critical branch) continues to use poll_radius_km. If
    # the override has expired (auto-clears after 7 days), we ignore it
    # silently and log a diagnostic skip once so operators know why they
    # stopped seeing wide-radius previews.
    #
    # `effective_radius_km` is what gets fed into should_send_preview.
    # `override_active` tells us later whether to prefix the body text
    # with the "beyond alert zone" warning.
    preview_cfg_snapshot = preview_cfg  # already isolated above; alias for clarity
    override_km = preview_cfg_snapshot.get("preview_radius_km_override")
    override_expires = preview_cfg_snapshot.get("preview_radius_km_override_expires_at")
    now_for_expiry = datetime.now(timezone.utc)
    override_active = False
    override_expired = False
    if override_km is not None:
        # Coerce override_expires to timezone-aware if it came back naive
        # from Mongo (older documents may not have tzinfo attached).
        if override_expires is not None and override_expires.tzinfo is None:
            override_expires = override_expires.replace(tzinfo=timezone.utc)
        if override_expires is None or override_expires > now_for_expiry:
            override_active = True
        else:
            override_expired = True

    if override_active:
        effective_radius_km = float(override_km)
    else:
        effective_radius_km = poll_radius_km

    # Evaluate whether the tier's rule matched — INCLUDING the hard
    # radius gate that applies to every tier, all_ingested included.
    # Fix for BUG-2026-08-06-preview-worldwide: previously all_ingested
    # returned True unconditionally, so worldwide events (10,000+km
    # away) generated Malta previews.
    should_fire, skip_reason = should_send_preview(
        evaluations=emsc_event.get("evaluations") or [],
        country_code=country_code,
        trigger_tier=trigger_tier,
        distance_km=distance_km,
        poll_radius_km=effective_radius_km,
    )
    if override_expired and skip_reason:
        # Attach diagnostic so the operator can spot "why did previews
        # stop 7 days after I set 2000 km" without digging through
        # audit history.
        skip_reason = f"{skip_reason} [override_expired_at={override_expires.isoformat() if override_expires else 'unknown'}]"
    if not should_fire:
        # Log every skip so operators can audit "why didn't I get it"
        # and — critically — spot false negatives during Day-14 review.
        # A distant-event skip is expected; a near-event skip would be a bug.
        await db.emsc_preview_notifications.insert_one({
            "sent_at": datetime.now(timezone.utc),
            "device_id": None,
            "delivered": False,
            "skipped_reason": skip_reason,
            "emsc_event_ref": {"provider": emsc_event.get("provider"),
                               "external_id": emsc_event.get("external_id"),
                               "revision": emsc_event.get("revision"),
                               "magnitude": emsc_event.get("magnitude"),
                               "distance_km": distance_km},
            "country_code": country_code,
            "trigger_tier": trigger_tier,
        })
        return None

    now = datetime.now(timezone.utc)
    rate_limit_minutes = int(preview_cfg.get("rate_limit_minutes") or 10)

    # ── Freshness gate (2026-08-18, batch 6 A0) ─────────────────────────
    # An informational notice must be about something that just happened.
    # There was no age check at all, so any event newly ingested — or
    # newly ELIGIBLE because an operator widened the radius or loosened
    # the tier — produced a notice that reads as current no matter how old
    # the quake was. That is how a tremor timestamped 08:07 was announced
    # just before 11:00: the config changed, and the backlog went out.
    max_age_minutes = int(preview_cfg.get("max_event_age_minutes") or 90)
    observed_dt = emsc_event.get("observed_at")
    if isinstance(observed_dt, str):
        try:
            observed_dt = datetime.fromisoformat(observed_dt.replace("Z", "+00:00"))
        except ValueError:
            observed_dt = None
    if observed_dt is not None:
        if observed_dt.tzinfo is None:
            observed_dt = observed_dt.replace(tzinfo=timezone.utc)
        age_minutes = (now - observed_dt).total_seconds() / 60.0
        if age_minutes > max_age_minutes:
            await db.emsc_preview_notifications.insert_one({
                "sent_at": now,
                "device_id": None,
                "delivered": False,
                "skipped_reason": f"event_too_old ({age_minutes:.0f} min > {max_age_minutes} min)",
                "emsc_event_ref": {"provider": emsc_event.get("provider"),
                                   "external_id": emsc_event.get("external_id"),
                                   "revision": emsc_event.get("revision"),
                                   "magnitude": emsc_event.get("magnitude")},
                "country_code": country_code,
                "trigger_tier": trigger_tier,
            })
            return None

    # Fetch push_devices rows for the allowlisted device_ids that
    # currently have iOS tokens. Preview mode is iOS-only for v1.
    # We ALSO pull notification_preset so we can enforce per-device
    # user preferences (Requirement 1, 2026-08-06 — the in-app off
    # switch that keeps users from reaching for iOS's blanket
    # notification-disable and killing critical alerts too).
    push_devices = await db.push_devices.find(
        {
            "user_id": {"$in": device_ids},
            "platform": {"$in": ["ios", "iOS"]},
            "device_token": {"$exists": True, "$ne": None},
        },
        {"_id": 0, "user_id": 1, "device_token": 1, "notification_preset": 1},
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

    # Bearing for the notification body. distance_km was already
    # computed above for the radius gate — reuse it.
    country_center = country_config.get("center") or {}
    c_lat = country_center.get("lat")
    c_lon = country_center.get("lon")
    bearing = None
    if c_lat is not None and c_lon is not None:
        bearing = bearing_deg(
            c_lat, c_lon,
            emsc_event.get("latitude"), emsc_event.get("longitude"),
        )

    # ── Revisions must never look like new earthquakes (batch 6 A0) ─────
    # EMSC republishes an event as its magnitude is refined, and each
    # revision stored a new emsc_events row, which dispatched a second
    # notice. Two notices minutes apart — "M3.3, 249km" then "M3.7,
    # 251km" — read as two earthquakes to any user. They were one.
    #
    # Rule: for an event we have ALREADY notified about,
    #   * material change (magnitude moved >= 0.3) -> send, but explicitly
    #     labelled as an update, with the previous figure;
    #   * anything smaller -> suppress and log why.
    # A first notice for an event is unaffected.
    prior = await db.emsc_preview_notifications.find_one(
        {
            "emsc_event_ref.external_id": emsc_event.get("external_id"),
            "emsc_event_ref.provider": emsc_event.get("provider"),
            "delivered": True,
        },
        {"_id": 0, "emsc_event_ref": 1, "sent_at": 1},
        sort=[("sent_at", -1)],
    )
    is_update = False
    prior_magnitude = None
    if prior:
        prior_magnitude = (prior.get("emsc_event_ref") or {}).get("magnitude")
        new_magnitude = emsc_event.get("magnitude")
        moved = (
            prior_magnitude is not None and new_magnitude is not None
            and abs(float(new_magnitude) - float(prior_magnitude)) >= 0.3
        )
        if not moved:
            await db.emsc_preview_notifications.insert_one({
                "sent_at": now,
                "device_id": None,
                "delivered": False,
                "skipped_reason": "revision_no_material_change",
                "emsc_event_ref": {"provider": emsc_event.get("provider"),
                                   "external_id": emsc_event.get("external_id"),
                                   "revision": emsc_event.get("revision"),
                                   "magnitude": new_magnitude,
                                   "prior_magnitude": prior_magnitude},
                "country_code": country_code,
                "trigger_tier": trigger_tier,
            })
            return None
        is_update = True

    title = (
        "PREVIEW · Updated seismic reading" if is_update
        else "PREVIEW · Seismic activity"
    )
    body = format_body(
        magnitude=emsc_event.get("magnitude") or 0.0,
        distance_km=distance_km or 0.0,
        depth_km=emsc_event.get("depth_km"),
        bearing_from_country=bearing if bearing is not None else 0.0,
        country_name=country_config.get("country_name") or country_code,
    )
    if is_update:
        body = (
            f"Updated: now measured at M{emsc_event.get('magnitude'):g} "
            f"(first reported M{float(prior_magnitude):g}). Same earthquake, "
            f"not a new one. " + body
        )
    # When the operator's radius override is what allowed this preview
    # through (event is beyond the real 600 km data boundary), prepend a
    # visible marker so a preview at 1800 km can never be mistaken for a
    # real 600 km-boundary alert. Real users never see previews at all,
    # but this defends the calibration channel for the operator too —
    # "PREVIEW ·" alone starts to blur if you get twenty a day.
    if (override_active and poll_radius_km is not None
            and distance_km is not None and distance_km > poll_radius_km):
        body = f"⚠️ Beyond alert zone — {body}"

    # Rate-limit filter + notification-preset filter. Two separate gates
    # applied per device:
    #   (1) preset: does this user want notifications at this MMI level?
    #   (2) rate limit: has this device been notified too recently?
    # A device that fails either check is logged as skipped with the
    # honest reason — never silently dropped.
    #
    # `effective_mmi` for preset comparison is derived once here from
    # the event's intensity_estimates block. Same value used across all
    # eligible devices for consistency.
    from notification_presets import preset_would_fire, DEFAULT_PRESET as _DEFAULT_PRESET
    from .intensity import effective_mmi_for_tier_decision
    intensity_at_country = (emsc_event.get("intensity_estimates") or {}).get(f"at_{country_code}_center") or {}
    effective_mmi = effective_mmi_for_tier_decision(intensity_at_country)

    eligible: List[dict] = []
    skipped: List[dict] = []
    skipped_by_preset: List[tuple[dict, str]] = []
    for pd in push_devices:
        did = pd.get("user_id") or ""
        if not did:
            continue
        # Preset gate — user's own off-switch / sensitivity choice.
        user_preset = pd.get("notification_preset") or _DEFAULT_PRESET
        preset_fire, preset_reason = preset_would_fire(user_preset, effective_mmi)
        if not preset_fire:
            skipped_by_preset.append((pd, preset_reason or "user_preset"))
            continue
        # Rate-limit gate — swarm-flood defence.
        limited = await _recently_notified(db, did, rate_limit_minutes, now)
        if limited:
            skipped.append(pd)
        else:
            eligible.append(pd)

    # Log preset-blocked ones with their specific reason.
    for pd, preset_reason in skipped_by_preset:
        await db.emsc_preview_notifications.insert_one({
            "sent_at": now,
            "device_id": pd.get("user_id"),
            "delivered": False,
            "skipped_reason": preset_reason,
            "user_preset": pd.get("notification_preset") or _DEFAULT_PRESET,
            "effective_mmi_at_dispatch": effective_mmi,
            "emsc_event_ref": {"provider": emsc_event.get("provider"),
                               "external_id": emsc_event.get("external_id"),
                               "revision": emsc_event.get("revision")},
            "country_code": country_code,
            "trigger_tier": trigger_tier,
        })

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
    observed_at_iso = iso_utc(observed_at)
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
    # Tag whether the operator's radius override was in effect for this
    # dispatch — makes it trivial to answer "which of these previews rode
    # the wider test radius?" during audit review. `radius_override_active`
    # is True only when the override was BOTH set AND non-expired at the
    # time the dispatch decision was made.
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
            # magnitude is recorded here so a later revision of the SAME
            # event can be compared against what the user was actually
            # told (batch 6 A0 — revisions must not read as new quakes).
            "emsc_event_ref": {"provider": emsc_event.get("provider"),
                               "external_id": emsc_event.get("external_id"),
                               "revision": emsc_event.get("revision"),
                               "magnitude": emsc_event.get("magnitude"),
                               # Recorded on DELIVERED rows too (2026-08-18).
                               # It was only stored on skipped rows, so a
                               # "closest event we've seen" query over this
                               # collection could only ever return the closest
                               # event we DIDN'T notify about — which is how a
                               # 4,829 km figure got quoted while 249 km
                               # notices were arriving on the phone.
                               "distance_km": distance_km,
                               "observed_at": emsc_event.get("observed_at")},
            "country_code": country_code,
            "trigger_tier": trigger_tier,
            "title": title,
            "body": body,
            "idempotency_key": idem,
            "radius_override_active": override_active,
            "effective_radius_km": effective_radius_km,
            "poll_radius_km": poll_radius_km,
        })

    delivered_count = sum(1 for e in (result.get("events") or []) if e.get("delivered"))
    return {
        "attempted": len(eligible),
        "delivered": delivered_count,
        "skipped_rate_limited": len(skipped),
        "title": title,
        "body": body,
    }


# ── B8: "places I care about" — informational notices for saved places ──
#
# Approved 2026-08-17 (task #158) in place of a user-set radius slider.
# Each saved place is evaluated with the SAME intensity logic as the
# user's own location — never a raw radius. See the block comment above
# the /api/devices/{id}/places endpoints in server.py for the full
# rationale, and note the hard safety constraint:
#
#   NOTHING HERE CAN AFFECT THE CRITICAL ALERT FOR THE USER'S OWN
#   LOCATION. This module is only ever called from the informational
#   preview path. The critical path (send_critical_alerts via
#   /api/trigger-alert, and the evaluator's critical branch) never reads
#   `user_places`. That's a structural guarantee, not a flag.
#
# Distance cap: an event must be within the country's poll_radius_km of
# the PLACE as well, so the "everything nearby" preset (MMI floor 0)
# can't turn a saved place into a worldwide firehose.
async def dispatch_place_notices(
    *,
    db,
    apns_send_preview,
    emsc_event: dict,
    country_config: dict,
) -> Optional[dict]:
    """Send one informational notice per (device, saved place) that clears
    the device's own preset threshold at that place's coordinates.

    Returns None when nothing was sent, else a summary dict.
    """
    from notification_presets import preset_would_fire, DEFAULT_PRESET as _DEFAULT_PRESET
    from .evaluator import haversine_km
    from .intensity import mmi_from_faenza_michelini_2010

    preview_cfg = (country_config or {}).get("preview_mode") or {}
    if not preview_cfg.get("enabled"):
        return None
    device_ids = list(preview_cfg.get("device_ids") or [])
    if not device_ids:
        return None

    e_lat = emsc_event.get("latitude")
    e_lon = emsc_event.get("longitude")
    magnitude = emsc_event.get("magnitude")
    if e_lat is None or e_lon is None or magnitude is None:
        return None

    places = await db.user_places.find(
        {"device_id": {"$in": device_ids}}, {"_id": 0},
    ).to_list(200)
    if not places:
        return None

    push_devices = await db.push_devices.find(
        {
            "user_id": {"$in": device_ids},
            "platform": {"$in": ["ios", "iOS"]},
            "device_token": {"$exists": True, "$ne": None},
        },
        {"_id": 0, "user_id": 1, "device_token": 1, "notification_preset": 1,
         "places_enabled": 1},
    ).to_list(50)
    by_id = {d.get("user_id"): d for d in push_devices}

    # Same freshness gate as the own-location path (batch 6 A0). Flagged by
    # the test agent: without it, a stale event handed to the places path
    # could still announce a hours-old tremor near someone's family as if it
    # had just happened.
    max_age_minutes = int(preview_cfg.get("max_event_age_minutes") or 90)
    observed_dt = emsc_event.get("observed_at")
    if isinstance(observed_dt, str):
        try:
            observed_dt = datetime.fromisoformat(observed_dt.replace("Z", "+00:00"))
        except ValueError:
            observed_dt = None
    if observed_dt is not None:
        if observed_dt.tzinfo is None:
            observed_dt = observed_dt.replace(tzinfo=timezone.utc)
        if (datetime.now(timezone.utc) - observed_dt).total_seconds() / 60.0 > max_age_minutes:
            return None

    radius_cap_km = country_config.get("poll_radius_km") or 600.0
    rate_limit_minutes = int(preview_cfg.get("rate_limit_minutes") or 10)
    now = datetime.now(timezone.utc)
    attempted = 0

    for place in places:
        dev = by_id.get(place.get("device_id"))
        if not dev:
            continue
        # Whole-feature switch — silences every place without deleting any.
        if dev.get("places_enabled") is False:
            continue

        p_lat = place.get("latitude")
        p_lon = place.get("longitude")
        if p_lat is None or p_lon is None:
            continue
        distance_km = haversine_km(e_lat, e_lon, p_lat, p_lon)
        if distance_km > radius_cap_km:
            continue

        mmi_at_place = mmi_from_faenza_michelini_2010(
            magnitude=magnitude,
            distance_km=distance_km,
            depth_km=emsc_event.get("depth_km"),
        )
        user_preset = dev.get("notification_preset") or _DEFAULT_PRESET
        fires, skip_reason = preset_would_fire(user_preset, mmi_at_place)
        if not fires:
            continue

        # Rate limit per (device, place) so a swarm near one place can't
        # flood, while a genuinely different place can still get through.
        cutoff = now - timedelta(minutes=rate_limit_minutes)
        recent = await db.emsc_preview_notifications.find_one(
            {
                "device_id": place["device_id"],
                "place_id": place.get("place_id"),
                "sent_at": {"$gte": cutoff},
                "delivered": True,
            },
            {"_id": 1},
        )
        if recent:
            await db.emsc_preview_notifications.insert_one({
                "sent_at": now,
                "device_id": place["device_id"],
                "place_id": place.get("place_id"),
                "place_name": place.get("name"),
                "delivered": False,
                "skipped_reason": "rate_limited",
                "country_code": country_config.get("country_code"),
            })
            continue

        # Copy must name the place unambiguously — the user has to know at
        # a glance this is about Sicily and NOT about them.
        name = place.get("name") or "your saved place"
        bearing = bearing_deg(p_lat, p_lon, e_lat, e_lon)
        title = f"PREVIEW · Seismic activity near {name}"
        body = (
            f"{name}: M{magnitude:g} tremor — {int(round(distance_km))}km "
            f"{compass_16(bearing)} of {name}"
            + (f", depth {int(round(emsc_event.get('depth_km')))}km"
               if emsc_event.get("depth_km") is not None else "")
            + ". This is one of your saved places, not your own location."
        )

        idem = f"place-notice-{uuid.uuid4()}"
        unid = emsc_event.get("external_id")
        observed_at = emsc_event.get("observed_at")
        observed_at_iso = iso_utc(observed_at)
        try:
            result = await apns_send_preview(
                db=db,
                devices=[dev],
                title=title,
                body=body,
                action_url=f"/quake/{unid}" if unid else "/",
                idempotency_key=idem,
                magnitude=magnitude,
                distance_km=round(distance_km, 1),
                depth_km=emsc_event.get("depth_km"),
                latitude=e_lat,
                longitude=e_lon,
                region=emsc_event.get("region"),
                unid=unid,
                provider=emsc_event.get("provider"),
                observed_at=observed_at_iso,
            )
        except Exception as e:
            log.warning("Place-notice APNs send raised: %s", e)
            result = {"events": [{"user_id": dev.get("user_id"),
                                  "delivered": False, "error": str(e)}]}

        attempted += 1
        for evt in (result.get("events") or []):
            await db.emsc_preview_notifications.insert_one({
                "sent_at": now,
                "device_id": evt.get("user_id"),
                "place_id": place.get("place_id"),
                "place_name": name,
                "delivered": bool(evt.get("delivered")),
                "apns_id": evt.get("apns_id"),
                "reason": evt.get("reason"),
                "error": evt.get("error"),
                "status_code": evt.get("status_code"),
                "kind": "place_notice",
                "mmi_at_place": round(mmi_at_place, 2),
                "distance_km": round(distance_km, 1),
                "user_preset": user_preset,
                "emsc_event_ref": {"provider": emsc_event.get("provider"),
                                   "external_id": emsc_event.get("external_id"),
                                   "revision": emsc_event.get("revision"),
                                   "magnitude": emsc_event.get("magnitude")},
                "country_code": country_config.get("country_code"),
                "title": title,
                "body": body,
                "idempotency_key": idem,
            })

    if attempted == 0:
        return None
    return {"attempted": attempted}
