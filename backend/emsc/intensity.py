"""Intensity-based alerting — the reframe of what we measure.

Magnitude describes energy released at the source. Intensity (MMI/EMS-98)
describes what a person at a specific location actually feels. Every
mature system (ShakeAlert, JMA) alerts on predicted intensity. Ours does
too, starting Part 1a of the 2026-08-06 soak.

Data sources, in priority order:

  1. USGS `properties.mmi` (ShakeMap-derived instrumental intensity)
     — take directly. Available on ~14% of weekly M2.5+ events, ~100%
     of significant events. This is the best signal we have.

  2. USGS `properties.cdi` (DYFI community-reported)
     — take as secondary confirmation. Community-reported, so subject
     to the same self-selection bias as EMSC testimonies.

  3. GMPE + GMICE fallback for events without USGS intensity data
     — Faenza & Michelini 2010 IPE for Italy/Mediterranean:
        MMI = 1.68 + 1.71*M - 1.68*log10(R_hypo)
     Simple, single-step (M/R → MMI), well-cited for our region.
     Uncertainty ~0.5 MMI. We DELIBERATELY bias toward the alarming
     edge of that uncertainty band (see `mmi_predicted_upper_band`).

  4. EMSC Testimonies (via testimonies.py) — asynchronous ground-truth
     validation. Not used for tier decisions (self-selection bias +
     ~1h latency), but recorded per event so Day-14 tuning can compare
     predicted-vs-reported.

Asymmetric-cost bias (LOCKED — do not "optimise" this away):

  A false alarm irritates. A missed alert can kill someone. The
  ground-motion-to-intensity uncertainty band is ±0.5–1.0 MMI in the
  best case. We alert on the ALARMING edge — `mmi_predicted_upper_band`
  drives the tier decision, not `mmi_predicted`. If our best estimate
  is MMI 4.8 and the upper band is 5.3, the tier decision uses 5.3.

  A future optimisation pass will see this as excess sensitivity and
  want to "fix" it. It's not excess sensitivity. It's the deliberate
  reflection of the asymmetric cost of the two failure modes. Removing
  it would optimise for the wrong loss function. Do not remove.

Citation: Faenza, L. and Michelini, A. (2010). Regression analysis of
MCS intensity and ground motion parameters in Italy and its application
in ShakeMap. Geophysical Journal International, 180(3), 1138-1152.
"""
from __future__ import annotations

import math
from typing import Any, Optional


# Uncertainty envelope (MMI units) added to `mmi_predicted` to produce
# `mmi_predicted_upper_band` — the value that actually drives tier
# decisions. See asymmetric-cost note above.
GMPE_UNCERTAINTY_MMI = 0.5


def _to_float(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def mmi_from_faenza_michelini_2010(
    magnitude: float,
    distance_km: float,
    depth_km: Optional[float],
) -> float:
    """Predicted MMI at the given epicentral distance for an event of
    the given magnitude and depth. Italian-Mediterranean IPE.

        MMI = 1.68 + 1.71*M - 1.68*log10(R_hypo) - 0.00189*R_hypo

    where R_hypo = sqrt(R_epi^2 + depth^2). The final linear term is
    the anelastic attenuation (energy absorbed by the crust) — becomes
    significant at long distances (>100km). Omitting it makes the
    equation systematically over-predict at long range; including it
    keeps M6@400km around MMI 6-7 rather than 8+.

    If depth is unknown we assume 10km — the median crustal earthquake
    depth, and a conservative choice (shallower biases intensity upward).

    Clamped to [1.0, 12.0] because the equation extrapolates outside
    that range and would return nonsense (negative MMI at very large
    distances; MMI > 12 doesn't exist).

    Known limitation (2026-08-06): all IPEs of this form run hot at
    large distances because they were fitted mostly on near-field data.
    This is DELIBERATELY OK given the asymmetric-cost bias — we prefer
    over-prediction to under-prediction. Day-14 soak analysis against
    EMSC testimony ground truth will quantify the over-prediction and
    we can calibrate down (never up) from there.
    """
    depth = depth_km if depth_km is not None else 10.0
    r_epi = max(distance_km, 1.0)
    r_hypo = math.sqrt(r_epi * r_epi + depth * depth)
    mmi = 1.68 + 1.71 * magnitude - 1.68 * math.log10(r_hypo) - 0.00189 * r_hypo
    return max(1.0, min(12.0, mmi))


def compute_intensity_estimates(
    *,
    event_magnitude: float,
    event_lat: float,
    event_lon: float,
    event_depth_km: Optional[float],
    country_center_lat: float,
    country_center_lon: float,
    distance_km: float,
    raw_provider_payload: dict,
) -> dict:
    """Build the `intensity_estimates` sub-document for one event.

    Returns:
      {
        at_<country>_center: {
          mmi_from_usgs: float | None,   # properties.mmi if provider is USGS
          cdi_from_usgs: float | None,   # properties.cdi if provider is USGS
          mmi_predicted: float,           # GMPE/GMICE estimate
          mmi_predicted_upper_band: float,  # ALARMING-edge — drives tiers
          gmpe_used: str,                 # citation string
        },
        from_emsc_testimonies: {          # filled by testimonies sweeper
          max_intensity: None,
          report_count: 0,
          last_updated: None,
        }
      }

    The caller is responsible for placing this under
    `at_malta_center` (or the appropriate country-scoped key) in the
    emsc_events document. This function is country-agnostic.
    """
    # USGS pass-through (present as `properties.mmi` / `properties.cdi`
    # in the raw GeoJSON payload). Only extract if the provider payload
    # actually carries them — EMSC doesn't.
    mmi_from_usgs = None
    cdi_from_usgs = None
    props = (raw_provider_payload or {}).get("properties") or {}
    mmi_from_usgs = _to_float(props.get("mmi"))
    cdi_from_usgs = _to_float(props.get("cdi"))

    # GMPE prediction — always compute, even when USGS mmi is present.
    # Day-14 analysis wants to compare our GMPE against USGS ShakeMap to
    # measure how good our fallback is on events where the ground truth
    # exists.
    mmi_predicted = mmi_from_faenza_michelini_2010(
        magnitude=event_magnitude,
        distance_km=distance_km,
        depth_km=event_depth_km,
    )
    mmi_predicted_upper_band = min(12.0, mmi_predicted + GMPE_UNCERTAINTY_MMI)

    return {
        "mmi_from_usgs": round(mmi_from_usgs, 2) if mmi_from_usgs is not None else None,
        "cdi_from_usgs": round(cdi_from_usgs, 2) if cdi_from_usgs is not None else None,
        "mmi_predicted": round(mmi_predicted, 2),
        "mmi_predicted_upper_band": round(mmi_predicted_upper_band, 2),
        "gmpe_used": "Faenza-Michelini-2010",
    }


def effective_mmi_for_tier_decision(intensity_dict: dict) -> Optional[float]:
    """The single MMI value used to decide which intensity threshold_set
    matched. Priority: USGS mmi → USGS cdi → predicted upper band.

    USGS values are preferred because they are actual measurements /
    observations, not predictions. When we have real ShakeMap data
    there is no reason to defer to our own model. GMPE is the fallback
    for events without USGS coverage.

    Note this returns the ALARMING-edge value from the GMPE fallback
    (upper_band, not the best estimate). See asymmetric-cost note in
    the module docstring.
    """
    if intensity_dict is None:
        return None
    v = intensity_dict.get("mmi_from_usgs")
    if v is not None:
        return v
    v = intensity_dict.get("cdi_from_usgs")
    if v is not None:
        return v
    return intensity_dict.get("mmi_predicted_upper_band")
