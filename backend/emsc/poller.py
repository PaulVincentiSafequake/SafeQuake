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

    def __init__(self, db):
        self.db = db
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
        except Exception as e:
            log.warning("emsc_events index creation failed: %s", e)

        self.task = asyncio.create_task(self._run(), name="emsc_poller")
        log.info("EMSC poller started (%s providers, %ss interval)", len(self.providers), POLL_INTERVAL_SEC)

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
            "evaluations": _evaluate_all_countries(ev, configs),
        }
        await self.db.emsc_events.insert_one(doc)
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


def _evaluate_all_countries(ev: RawEvent, configs: List[dict]) -> List[dict]:
    """For every country_config, run the event through every threshold_set
    and return one flat list of evaluation dicts. Embedded into the
    emsc_events document alongside the raw event.
    """
    out: List[dict] = []
    for cfg in configs:
        center = cfg.get("center") or {}
        c_lat = center.get("lat")
        c_lon = center.get("lon")
        if c_lat is None or c_lon is None:
            continue
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
        if not threshold_sets:
            continue
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
            out.append({
                "country_code": e.country_code,
                "threshold_set": e.threshold_set,
                "distance_km": e.distance_km,
                "severity_score": e.severity_score,
                "matched": e.matched,
                "would_have_fired": e.would_have_fired,
                "reason": e.reason,
                "thresholds_snapshot": e.thresholds_snapshot,
            })
    return out
