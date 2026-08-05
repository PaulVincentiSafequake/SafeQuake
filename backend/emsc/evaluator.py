"""Threshold-set evaluation for the EMSC Phase 1 shadow-mode poller.

Each country_config carries a list of named threshold_sets — the
`quiet_tier`, `critical_tier`, `neo_original` etc. that we're trying to
calibrate during soak. For every raw provider event, we evaluate the
event against EVERY threshold_set of EVERY country_config and store the
outcomes side by side. That way after two weeks we can answer
directly: "the quiet_tier would have fired 23 times, the critical_tier
twice" — without re-running or re-polling.

Severity score
    A simple deterministic function of magnitude, distance, and depth.
    Higher = more relevant to the target location. Log-decay on distance
    (an M4 at 10km is roughly as impactful as an M5 at 100km) plus a
    modest depth penalty.

    severity = magnitude - log10(max(distance_km, 1) / 10) - depth_km / 200

    Rationale for these coefficients:
      - The /10 divisor inside the log means "distance in units of 10km";
        anything closer than 10km gets a positive boost, farther loses
        one point per 10× distance. Matches USGS ShakeMap intuition to
        ~1 significant figure without pretending to be physically exact.
      - The /200 depth penalty is deliberately gentle — a 100km-deep
        M5 at 50km still scores ~4.5, which we WANT to log during
        shadow mode. Depth is genuinely relevant (a shallow M4 at 20km
        feels stronger than a 300km-deep M6 at 20km) but the effect is
        secondary to distance-decay for our use case.

    We'll almost certainly tune the coefficients during soak; the
    formula lives here as a single function so tuning is one-line.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class ThresholdSet:
    """A named candidate rule for firing an alert.

    All min/max fields are OPTIONAL — an unset field is treated as "no
    constraint on this axis". A threshold_set with all-None fields
    matches every event, which is useful as a sanity-check baseline
    ("literally every event" tier).
    """
    name: str
    min_magnitude: Optional[float] = None
    max_distance_km: Optional[float] = None
    min_severity_score: Optional[float] = None
    enabled: bool = True


@dataclass
class Evaluation:
    """The result of applying one threshold_set to one event."""
    country_code: str
    threshold_set: str
    distance_km: float
    severity_score: float
    matched: bool
    would_have_fired: bool                   # matched AND threshold_set.enabled
    reason: Optional[str] = None              # e.g. "below min_magnitude"
    thresholds_snapshot: dict = field(default_factory=dict)


# ── Geo ──────────────────────────────────────────────────────────────────
EARTH_RADIUS_KM = 6371.0


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in kilometres. Standard haversine. Accurate
    to sub-metre for any distance we care about."""
    lat1_r = math.radians(lat1)
    lat2_r = math.radians(lat2)
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1_r) * math.cos(lat2_r) * math.sin(dlon / 2) ** 2
    c = 2 * math.asin(math.sqrt(a))
    return EARTH_RADIUS_KM * c


# ── Severity ─────────────────────────────────────────────────────────────
def severity_score(magnitude: float, distance_km: float, depth_km: Optional[float]) -> float:
    """Composite score. See module docstring for the formula rationale."""
    d = max(distance_km, 1.0)
    depth_penalty = ((depth_km or 0.0) / 200.0)
    return magnitude - math.log10(d / 10.0) - depth_penalty


# ── Per-event, per-country evaluation ────────────────────────────────────
def evaluate_event_against_country(
    *,
    magnitude: float,
    latitude: float,
    longitude: float,
    depth_km: Optional[float],
    country_center_lat: float,
    country_center_lon: float,
    country_code: str,
    threshold_sets: List[ThresholdSet],
) -> List[Evaluation]:
    """Return one Evaluation per threshold_set. Called once per event
    per country_config — Phase 1 has one config (Malta) but the shape
    is multi-country from the start.

    Each Evaluation records both `matched` (would the rule have fired
    based on thresholds) and `would_have_fired` (matched AND the set is
    marked enabled). That distinction lets us disable a threshold_set
    for real firing later while continuing to log what it *would* have
    done — useful for A/B-style comparisons during ongoing operation.
    """
    distance = haversine_km(latitude, longitude, country_center_lat, country_center_lon)
    score = severity_score(magnitude, distance, depth_km)

    results: List[Evaluation] = []
    for ts in threshold_sets:
        reason: Optional[str] = None
        matched = True
        if ts.min_magnitude is not None and magnitude < ts.min_magnitude:
            matched, reason = False, f"below min_magnitude ({magnitude} < {ts.min_magnitude})"
        elif ts.max_distance_km is not None and distance > ts.max_distance_km:
            matched, reason = False, f"beyond max_distance_km ({distance:.1f} > {ts.max_distance_km})"
        elif ts.min_severity_score is not None and score < ts.min_severity_score:
            matched, reason = False, f"below min_severity_score ({score:.2f} < {ts.min_severity_score})"

        results.append(Evaluation(
            country_code=country_code,
            threshold_set=ts.name,
            distance_km=round(distance, 2),
            severity_score=round(score, 3),
            matched=matched,
            would_have_fired=(matched and ts.enabled),
            reason=reason,
            thresholds_snapshot={
                "min_magnitude": ts.min_magnitude,
                "max_distance_km": ts.max_distance_km,
                "min_severity_score": ts.min_severity_score,
                "enabled": ts.enabled,
            },
        ))
    return results
