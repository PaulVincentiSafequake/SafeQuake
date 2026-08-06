"""Notification preset management for mobile devices.

Adds the "off" switch for informational tremor notifications that Paul
required as a safety feature (2026-08-06). The user MUST be able to
silence informational notifications easily — otherwise a frustrated
user reaches for iOS Settings' notification blanket switch and kills
CRITICAL alerts along with everything else.

The preset governs ONLY informational (preview) notifications. Real
critical alerts fire regardless of preset — that's enforced by the
separation of send paths: send_critical_alerts (bypass preset) vs
send_preview_alerts / preview dispatch (respects preset).

Presets:
  - "off":         no informational notifications, ever
  - "significant": only MMI IV+ (predicted)
  - "noticeable":  MMI III+ (default)
  - "everything":  every event inside country radius

The mapping of preset → MMI threshold lives here so backend and mobile
never drift. Mobile displays the labels; backend enforces the numbers.
"""
from typing import Optional

VALID_PRESETS = {"off", "significant", "noticeable", "everything"}
DEFAULT_PRESET = "noticeable"

# preset name → minimum effective MMI required to fire.
# `off` short-circuits before this map is consulted.
# `everything` uses 0.0 (any MMI fires, effectively "all events in radius").
PRESET_MMI_THRESHOLD = {
    "significant": 4.0,   # MMI IV+
    "noticeable":  3.0,   # MMI III+
    "everything":  0.0,   # every event within radius
}


def preset_would_fire(preset: str, effective_mmi: Optional[float]) -> tuple[bool, Optional[str]]:
    """Given a device's preset and the event's effective MMI, would a
    preview notification fire for this device? Returns (bool, reason_if_skipped).

    Note the effective_mmi may be None when the event has no USGS/GMPE
    signal at all — very rare. In that case, treat as MMI 0 for
    threshold comparison ("everything" fires, others don't) to preserve
    the honesty rule: never claim intensity we don't have.
    """
    if preset == "off":
        return False, "user_preset_off"
    if preset not in PRESET_MMI_THRESHOLD:
        # Unknown preset falls back to default rather than firing on
        # everything — safer default for corrupt/legacy client values.
        preset = DEFAULT_PRESET
    threshold = PRESET_MMI_THRESHOLD[preset]
    mmi = effective_mmi if effective_mmi is not None else 0.0
    if mmi < threshold:
        return False, f"below_preset_threshold ({mmi:.2f} < {threshold} for '{preset}')"
    return True, None
