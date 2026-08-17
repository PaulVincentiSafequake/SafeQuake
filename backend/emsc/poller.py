"""In-process asyncio poll loop for EMSC Phase 1 shadow mode.

Runs alongside the FastAPI app (started in `startup`, cancelled in
`shutdown`). Every POLL_INTERVAL_SEC seconds, in parallel:

  1. Fetch recent events from every registered provider.
  2. For each raw event, compare to the last stored row keyed on
     (provider, external_id). If mag/lat/lon/depth all match, skip.
     If they differ (or no prior row exists), insert a new row with
     revision = prev.revision + 1 (or 0).
  3. For each new row, evaluate against every country_config's
     threshold_sets and embed the results.
  4. Update emsc_poller_health for the provider (last_poll_attempt_at,
     last_success_at, consecutive_failures, last_error, events_this_poll).

Deliberately Phase-1 constrained:
  - No push firing. `would_have_fired` is logged; nothing acts on it.
  - No cross-provider dedup — we log EMSC and USGS side by side.
  - No circuit breaker. If a provider fails 5×, we log it in health and
    keep trying. If it fails permanently we'll notice via /admin/emsc/health.

Failure isolation:
  Each provider poll is wrapped in try/except. One provider failing
  never affects the other. The poll loop itself is wrapped in a broader
  try/except so a fatal bug in the evaluator can't kill the FastAPI app.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import List, Optional

import httpx

from .evaluator import ThresholdSet, evaluate_event_against_country
from .intensity import compute_intensity_estimates, effective_mmi_for_tier_decision
from .preview import dispatch_place_notices, dispatch_preview_if_needed
from .providers import EMSCProvider, Provider, RawEvent, USGSProvider


log = logging.getLogger(__name__)

POLL_INTERVAL_SEC = 60
# Window overlap: fetch the last WINDOW_MINUTES minutes each poll. The
# overlap tolerates poll-loop delays and lets us pick up events whose
# origin time is inside the window even if the provider only just added
# them to their feed.
WINDOW_MINUTES = 60


class EMSCPoller:
    """Owns the poll loop + shared HTTP client + provider instances.

    Not a singleton by construction — one is created in `server.py`'s
    startup and its lifecycle is tied to the FastAPI app.
    """

    def __init__(self, db, apns_send_preview=None):
        """`apns_send_preview` is an optional callable matching the
        signature of apns.send_preview_alerts. Passed in from server.py
        so the poller can dispatch preview notifications without directly
        importing the apns module (keeps the emsc/ subpackage
        transport-agnostic and testable in isolation)."""
        self.db = db
        self.apns_send_preview = apns_send_preview
        self.client = httpx.AsyncClient(
            headers={"User-Agent": "QuakeAngel-Phase1-Shadow/1.0 (contact: pmvincenti@gmail.com)"},
            timeout=20.0,
        )
        self.providers: List[Provider] = [
            EMSCProvider(self.client),
            USGSProvider(self.client),
        ]
        self.task: Optional[asyncio.Task] = None
        self.started_at: Optional[datetime] = None

    async def start(self) -> None:
        if self.task and not self.task.done():
            log.info("EMSC poller already running")
            return
        self.started_at = datetime.now(timezone.utc)
        # Idempotent indexes for the two collections we write into.
        try:
            await self.db.emsc_events.create_index(
                [("provider", 1), ("external_id", 1), ("revision", 1)],
                unique=True,
            )
            await self.db.emsc_events.create_index("ingested_at")
            await self.db.emsc_events.create_index("observed_at")
            await self.db.emsc_poller_gaps.create_index("provider")
            await self.db.emsc_poller_gaps.create_index("gap_end")
        except Exception as e:
            log.warning("emsc_events index creation failed: %s", e)

        # Continuity check — if the persisted `last_success_at` for a
        # provider is more than 3× poll interval old, we were dead for
        # that gap. Record it in emsc_poller_gaps so continuity is
        # queryable AFTER the fact. Without this, silent gaps (pod
        # suspension, credit exhaustion, deploy pauses) look identical
        # to "genuinely quiet seismic period" and destroy soak-data
        # trustworthiness.
        try:
            await self._detect_and_record_gaps_on_startup()
        except Exception as e:
            log.warning("startup gap detection failed: %s", e)

        # Ensure a soak_meta document exists so /continuity can report
        # authoritative soak_started_at. Never overwrites an existing
        # value — reset is a deliberate admin action, not a startup
        # side effect.
        try:
            await self.db.emsc_soak_meta.update_one(
                {"_id": "singleton"},
                {"$setOnInsert": {
                    "soak_started_at": datetime.now(timezone.utc),
                    "created_at": datetime.now(timezone.utc),
                    "reset_history": [],
                }},
                upsert=True,
            )
        except Exception as e:
            log.warning("emsc_soak_meta initialization failed: %s", e)

        self.task = asyncio.create_task(self._run(), name="emsc_poller")
        log.info("EMSC poller started (%s providers, %ss interval)", len(self.providers), POLL_INTERVAL_SEC)

    async def _detect_and_record_gaps_on_startup(self) -> None:
        """For each provider, if the last_success_at persisted from a
        previous run is more than GAP_THRESHOLD_SEC old, log a gap.
        Called once per start(). Idempotent — a gap that overlaps an
        existing recorded gap is deduped by (provider, gap_start)."""
        now = datetime.now(timezone.utc)
        gap_threshold_sec = POLL_INTERVAL_SEC * 3   # 3 minutes @ 60s cadence

        try:
            await self.db.emsc_poller_gaps.create_index(
                [("provider", 1), ("gap_start", 1)],
                unique=True,
            )
        except Exception:
            pass

        for provider in self.providers:
            row = await self.db.emsc_poller_health.find_one({"_id": provider.name})
            if not row:
                continue
            last = row.get("last_success_at")
            if not last:
                continue
            if isinstance(last, datetime) and last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            gap_sec = (now - last).total_seconds()
            if gap_sec < gap_threshold_sec:
                continue

            gap_doc = {
                "provider": provider.name,
                "gap_start": last,
                "gap_end": now,
                "gap_seconds": int(gap_sec),
                "detected_at": now,
                "detection_reason": "startup_continuity_check",
                "poll_interval_sec": POLL_INTERVAL_SEC,
            }
            try:
                await self.db.emsc_poller_gaps.insert_one(gap_doc)
                log.warning(
                    "EMSC continuity gap detected for %s: %d seconds (%.1f hours) — "
                    "gap_start=%s gap_end=%s",
                    provider.name, int(gap_sec), gap_sec / 3600, last, now,
                )
            except Exception as e:
                # Duplicate-key means this gap is already logged — fine.
                if "duplicate key" not in str(e).lower():
                    log.warning("gap insert failed for %s: %s", provider.name, e)

    async def stop(self) -> None:
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except (asyncio.CancelledError, Exception):
                pass
        await self.client.aclose()
        log.info("EMSC poller stopped")

    async def _run(self) -> None:
        """Main loop. Never exits under normal operation — the FastAPI
        shutdown handler cancels the task."""
        while True:
            try:
                await self._poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                # Broad safety net — never let a bug in one poll cycle
                # kill the loop. Health record will show the error.
                log.exception("EMSC poller cycle failed: %s", e)
            await asyncio.sleep(POLL_INTERVAL_SEC)

    async def _poll_once(self) -> None:
        # Load country configs once per cycle so admin edits apply on
        # the next poll without a restart.
        configs = await self.db.country_configs.find({"enabled": True}).to_list(50)
        if not configs:
            log.warning("EMSC poller: no enabled country_configs — nothing to evaluate")
            return

        # Widest poll_min_magnitude across all countries — one fetch feeds
        # all evaluations, so we take the minimum threshold.
        wide_min_mag = min(
            (c.get("poll_min_magnitude") or 2.5) for c in configs
        )
        since = datetime.now(timezone.utc) - timedelta(minutes=WINDOW_MINUTES)

        # Fan out to providers concurrently.
        results = await asyncio.gather(
            *[self._poll_provider(p, since, wide_min_mag, configs) for p in self.providers],
            return_exceptions=True,
        )
        # Log each provider's outcome individually — asyncio.gather results
        # is in provider order.
        for provider, outcome in zip(self.providers, results):
            if isinstance(outcome, Exception):
                log.warning("Provider %s failed: %s", provider.name, outcome)

    async def _poll_provider(
        self,
        provider: Provider,
        since: datetime,
        min_magnitude: float,
        configs: List[dict],
    ) -> None:
        started = datetime.now(timezone.utc)
        try:
            events = await provider.fetch(since, min_magnitude)
        except Exception as e:
            await self._record_health(provider.name, success=False, error=str(e)[:500])
            raise

        new_rows = 0
        for ev in events:
            try:
                if await self._store_if_new_or_revision(ev, configs):
                    new_rows += 1
            except Exception as e:
                log.warning("emsc_events insert failed for %s/%s: %s",
                            provider.name, ev.external_id, e)
                continue

        await self._record_health(
            provider.name,
            success=True,
            fetched=len(events),
            new_rows=new_rows,
            duration_ms=int((datetime.now(timezone.utc) - started).total_seconds() * 1000),
        )

    # ── Revision detection ───────────────────────────────────────────────
    async def _store_if_new_or_revision(self, ev: RawEvent, configs: List[dict]) -> bool:
        """Store this event if it's new OR if it's a revision (any of
        magnitude/lat/lon/depth changed since last stored row for the
        same (provider, external_id)). Returns True if a row was written.

        Revisions carry monotonically-increasing `revision` numbers so
        the timeline of a single event's refinement can be reconstructed
        from the collection alone.
        """
        if not ev.external_id:
            # No stable ID — we can't do revision detection, and
            # duplicate-storming is worse than skipping. Rare in practice
            # (both providers always emit an id).
            log.debug("Skipping %s event with no external_id", ev.provider)
            return False

        latest = await self.db.emsc_events.find_one(
            {"provider": ev.provider, "external_id": ev.external_id},
            sort=[("revision", -1)],
        )
        if latest and _same_content(latest, ev):
            return False

        revision = (latest.get("revision") + 1) if latest else 0
        # Evaluations + intensity computed together for efficiency
        # (both need per-country distance; compute once, reuse).
        evaluations, intensity_estimates = _evaluate_all_countries(ev, configs)
        doc = {
            "provider": ev.provider,
            "external_id": ev.external_id,
            "revision": revision,
            "observed_at": ev.observed_at,
            "ingested_at": datetime.now(timezone.utc),
            "magnitude": ev.magnitude,
            "magnitude_type": ev.magnitude_type,
            "latitude": ev.latitude,
            "longitude": ev.longitude,
            "depth_km": ev.depth_km,
            "region": ev.region,
            "shadow_mode": True,           # Phase 1 invariant
            "fired": False,                # Phase 1 invariant
            "raw": ev.raw,
            "evaluations": evaluations,
            "intensity_estimates": intensity_estimates,
        }
        await self.db.emsc_events.insert_one(doc)

        # ── Preview dispatch (P2.5 landing) ──────────────────────────
        # Preview mode is completely separate from shadow_mode: it can
        # be enabled per country_config to send NON-CRITICAL previews to
        # an allowlisted device, without disturbing the shadow-mode
        # guarantee for every other device. Dispatch runs synchronously
        # (awaited) but is wrapped in try/except so a preview-path bug
        # cannot break the core soak logging.
        if self.apns_send_preview is not None:
            for cfg in configs:
                try:
                    await dispatch_preview_if_needed(
                        db=self.db,
                        apns_send_preview=self.apns_send_preview,
                        emsc_event=doc,
                        country_config=cfg,
                    )
                except Exception as e:
                    log.warning(
                        "Preview dispatch failed for %s/%s (country=%s): %s",
                        ev.provider, ev.external_id,
                        cfg.get("country_code"), e,
                    )
                # B8 — informational notices for the user's saved places.
                # Separate try/except: a bug in the places path must never
                # take down the own-location notice above it, and neither
                # can touch the critical-alert path (different module).
                try:
                    await dispatch_place_notices(
                        db=self.db,
                        apns_send_preview=self.apns_send_preview,
                        emsc_event=doc,
                        country_config=cfg,
                    )
                except Exception as e:
                    log.warning(
                        "Place-notice dispatch failed for %s/%s (country=%s): %s",
                        ev.provider, ev.external_id,
                        cfg.get("country_code"), e,
                    )
        return True

    # ── Health tracking ──────────────────────────────────────────────────
    async def _record_health(
        self,
        provider_name: str,
        *,
        success: bool,
        error: Optional[str] = None,
        fetched: int = 0,
        new_rows: int = 0,
        duration_ms: int = 0,
    ) -> None:
        """Upsert emsc_poller_health for this provider. One document per
        provider, updated on every poll attempt. This is the single
        source of truth for 'is the poller alive?' — the admin health
        endpoint reads from here.
        """
        now = datetime.now(timezone.utc)
        set_ops = {
            "last_poll_attempt_at": now,
            "last_poll_duration_ms": duration_ms,
            "last_error": None if success else (error or "unknown"),
            "poller_started_at": self.started_at,
            "poll_interval_sec": POLL_INTERVAL_SEC,
        }
        inc_ops = {
            "total_polls": 1,
            "total_events_fetched": fetched,
            "total_new_rows": new_rows,
        }
        if success:
            set_ops["last_success_at"] = now
            set_ops["last_fetched_count"] = fetched
            set_ops["last_new_rows_count"] = new_rows
            set_ops["consecutive_failures"] = 0
        else:
            inc_ops["consecutive_failures"] = 1
            inc_ops["total_failures"] = 1

        await self.db.emsc_poller_health.update_one(
            {"_id": provider_name},
            {"$set": set_ops, "$inc": inc_ops, "$setOnInsert": {"created_at": now}},
            upsert=True,
        )


# ── Helpers (module-level, testable) ─────────────────────────────────────
def _same_content(stored: dict, ev: RawEvent) -> bool:
    """Return True if the previously-stored row's material fields match
    the newly-fetched event. Any change triggers a new revision row."""
    def _close(a: Optional[float], b: Optional[float], tol: float) -> bool:
        if a is None and b is None:
            return True
        if a is None or b is None:
            return False
        return abs(a - b) < tol

    return (
        _close(stored.get("magnitude"), ev.magnitude, 0.05) and
        _close(stored.get("latitude"), ev.latitude, 0.0001) and
        _close(stored.get("longitude"), ev.longitude, 0.0001) and
        _close(stored.get("depth_km"), ev.depth_km, 0.5)
    )


def _evaluate_all_countries(ev: RawEvent, configs: List[dict]) -> tuple[List[dict], dict]:
    """For every country_config, run the event through every threshold_set
    and return one flat list of evaluation dicts, PLUS the country-scoped
    intensity_estimates block for embedding into the event doc.

    Returns (evaluations_list, intensity_estimates_dict).

    Part 1a addition (2026-08-06): each event also picks up
    `intensity_estimates.at_<country_code>_center` computed via
    emsc/intensity.py, and three additional intensity-based
    threshold_sets are evaluated alongside the existing magnitude ones.
    Magnitude sets keep running in parallel — the Day-14 comparison
    against EMSC testimony ground-truth is the whole point.
    """
    out_evaluations: List[dict] = []
    out_intensity: dict = {}

    for cfg in configs:
        center = cfg.get("center") or {}
        c_lat = center.get("lat")
        c_lon = center.get("lon")
        if c_lat is None or c_lon is None:
            continue

        # Distance (used both by magnitude threshold_sets and by the
        # intensity computation below).
        from .evaluator import haversine_km
        distance_km = haversine_km(ev.latitude, ev.longitude, c_lat, c_lon)

        # Magnitude-based threshold_sets (unchanged from Phase 1).
        threshold_sets = [
            ThresholdSet(
                name=ts.get("name", "unnamed"),
                min_magnitude=ts.get("min_magnitude"),
                max_distance_km=ts.get("max_distance_km"),
                min_severity_score=ts.get("min_severity_score"),
                enabled=ts.get("enabled", True),
            )
            for ts in (cfg.get("threshold_sets") or [])
        ]
        if threshold_sets:
            evals = evaluate_event_against_country(
                magnitude=ev.magnitude,
                latitude=ev.latitude,
                longitude=ev.longitude,
                depth_km=ev.depth_km,
                country_center_lat=c_lat,
                country_center_lon=c_lon,
                country_code=cfg.get("country_code", "??"),
                threshold_sets=threshold_sets,
            )
            for e in evals:
                out_evaluations.append({
                    "country_code": e.country_code,
                    "threshold_set": e.threshold_set,
                    "distance_km": e.distance_km,
                    "severity_score": e.severity_score,
                    "matched": e.matched,
                    "would_have_fired": e.would_have_fired,
                    "reason": e.reason,
                    "thresholds_snapshot": e.thresholds_snapshot,
                })

        # Intensity computation (Part 1a).
        intensity = compute_intensity_estimates(
            event_magnitude=ev.magnitude,
            event_lat=ev.latitude,
            event_lon=ev.longitude,
            event_depth_km=ev.depth_km,
            country_center_lat=c_lat,
            country_center_lon=c_lon,
            distance_km=distance_km,
            raw_provider_payload=ev.raw,
        )
        # Placeholder for the testimonies sweeper — populated later.
        intensity["from_emsc_testimonies_placeholder"] = None
        cc = cfg.get("country_code", "??")
        out_intensity[f"at_{cc}_center"] = intensity
        out_intensity["from_emsc_testimonies"] = {
            "max_intensity": None, "report_count": 0, "last_updated": None,
        }

        # Intensity-based threshold_sets (Part 1a). Fixed thresholds
        # derived from the tier definitions locked 2026-08-06:
        #   informational  MMI III+ (III–IV band)
        #   standard       MMI V+
        #   critical       MMI VI+   (siren tier)
        effective_mmi = effective_mmi_for_tier_decision(intensity)
        for tier_name, threshold in [
            ("intensity_informational", 3.0),
            ("intensity_standard",      5.0),
            ("intensity_critical",      6.0),
        ]:
            matched = effective_mmi is not None and effective_mmi >= threshold
            reason = None
            if effective_mmi is None:
                reason = "no_intensity_signal_available"
            elif not matched:
                reason = f"effective_mmi {effective_mmi:.2f} < {threshold}"
            out_evaluations.append({
                "country_code": cc,
                "threshold_set": tier_name,
                "distance_km": round(distance_km, 2),
                "effective_mmi": effective_mmi,
                "mmi_source": _mmi_source(intensity),
                "matched": bool(matched),
                "would_have_fired": bool(matched),
                "reason": reason,
                "thresholds_snapshot": {
                    "min_effective_mmi": threshold,
                    "gmpe_used": intensity.get("gmpe_used"),
                },
            })

    return out_evaluations, out_intensity


def _mmi_source(intensity: dict) -> str:
    """Which of the three intensity sources was used for the tier decision.
    Recorded on every intensity evaluation so Day-14 analysis can separate
    "USGS-derived accuracy" from "GMPE-derived accuracy" — different
    questions, must not be blended."""
    if intensity.get("mmi_from_usgs") is not None:
        return "usgs_mmi"
    if intensity.get("cdi_from_usgs") is not None:
        return "usgs_cdi"
    return "gmpe_predicted_upper_band"
