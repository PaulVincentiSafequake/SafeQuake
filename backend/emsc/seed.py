"""Country-config seed for EMSC Phase 1.

On first boot we ensure at least one country_config exists so the poller
has something to evaluate against. Malta is baked in because that's our
launch market; adding a second country later is `db.country_configs.insertOne(...)`
or a call to the future admin endpoint.

The seed is idempotent — it only inserts if no document with the given
country_code exists. Later edits (via /api/admin/emsc/config/{cc}) are
never overwritten by a redeploy.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any


log = logging.getLogger(__name__)


# Wide-net polling defaults per user direction (2026-08):
#   - min_magnitude 2.5 (not 4.0): we WANT to see the quiet-tier events
#     during soak, so we can measure how often they'd fire.
#   - radius 600km (not 300): captures every plausibly-relevant event
#     around Malta including Sicilian, Ionian, and North African activity.
#   - no depth filter: depth is recorded but not filtered during soak.
#
# The per-tier evaluation cutoffs live in threshold_sets below — those
# are what would_have_fired hangs on, not the poll cutoffs.
MALTA_CONFIG: dict[str, Any] = {
    "country_code": "MT",
    "country_name": "Malta",
    "center": {"lat": 35.9375, "lon": 14.3754},   # Valletta
    "poll_radius_km": 600,
    "poll_min_magnitude": 2.5,
    "poll_max_depth_km": None,
    "shadow_mode": True,
    "enabled": True,
    "threshold_sets": [
        {
            # Frequent low-mag near events. This is what would drive the
            # quiet notification tier once we go live.
            "name": "quiet_tier",
            "min_magnitude": 3.0,
            "max_distance_km": 100.0,
            "min_severity_score": 2.0,
            "enabled": True,
        },
        {
            # Rare high-impact events. This is what would drive the
            # critical-alert tier once we go live.
            "name": "critical_tier",
            "min_magnitude": 5.0,
            "max_distance_km": 300.0,
            "min_severity_score": 4.0,
            "enabled": True,
        },
        {
            # The threshold set derived from Neolithic-earthquake research
            # in the original product design doc — kept as a comparison
            # baseline against the two operational tiers.
            "name": "neo_original",
            "min_magnitude": 4.0,
            "max_distance_km": 300.0,
            "min_severity_score": 3.0,
            "enabled": True,
        },
    ],
}


async def seed_country_configs(db) -> None:
    """Ensure the Malta config exists. Idempotent, never overwrites."""
    try:
        await db.country_configs.create_index("country_code", unique=True)
    except Exception as e:
        log.warning("country_configs index creation failed: %s", e)

    existing = await db.country_configs.find_one({"country_code": MALTA_CONFIG["country_code"]})
    if existing:
        return

    doc = dict(MALTA_CONFIG)
    doc["created_at"] = datetime.now(timezone.utc)
    doc["updated_at"] = doc["created_at"]
    doc["created_by"] = "bootstrap"
    await db.country_configs.insert_one(doc)
    log.info("Seeded country_config for %s", MALTA_CONFIG["country_code"])
