"""EMSC testimonies follow-up sweeper — Part 1a validation channel.

Every 15 minutes, batch-fetches EMS-98 felt-report intensities for
events ingested in the last 72 hours that don't yet have final
testimony data. Updates `intensity_estimates.from_emsc_testimonies`
in place — this is a running best-known value, NOT revision-tracked.

Methodological caveat (locked 2026-08-06):

  EMSC felt reports are SELF-SELECTED. People who felt something
  report; people who didn't, don't. That biases testimony-derived
  intensity upward, especially for marginal events. Treat these as
  a strong signal about the UPPER BOUND of what was felt, not an
  unbiased measurement. Confidence should be weighted by
  `report_count` — 5 reports is suggestive, 500 is solid.

  Where USGS mmi and EMSC testimonies both exist, we log BOTH and
  let the disagreement stand. Instrument-derived vs human-reported
  divergence is itself informative. Never merge into a single
  "ground truth" number.

Data source: https://www.seismicportal.eu/testimonies-ws/api/search
    unids=<comma-separated>&includeTestimonies=true
Licence: CC BY 4.0 (same as EMSC event data — already attributed).
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import List, Optional

import httpx


log = logging.getLogger(__name__)

TESTIMONIES_ENDPOINT = "https://www.seismicportal.eu/testimonies-ws/api/search"

# Sweep window — check events ingested in the last 72h. Past 72h,
# testimony data tends to have stabilised and re-checking is wasteful.
SWEEP_LOOKBACK_HOURS = 72

# Sweep cadence.
SWEEP_INTERVAL_SEC = 15 * 60


class TestimoniesSweeper:
    """Owns the sweep loop. Instantiated in server.py startup alongside
    the main poller; runs as its own asyncio task."""

    def __init__(self, db, client: Optional[httpx.AsyncClient] = None):
        self.db = db
        self._own_client = client is None
        self.client = client or httpx.AsyncClient(
            headers={"User-Agent": "QuakeAngel-Testimonies-Sweeper/1.0"},
            timeout=30.0,
        )
        self.task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        if self.task and not self.task.done():
            return
        self.task = asyncio.create_task(self._run(), name="emsc_testimonies_sweeper")
        log.info("EMSC testimonies sweeper started (%ss interval, %sh lookback)",
                 SWEEP_INTERVAL_SEC, SWEEP_LOOKBACK_HOURS)

    async def stop(self) -> None:
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except (asyncio.CancelledError, Exception):
                pass
        if self._own_client:
            await self.client.aclose()

    async def _run(self) -> None:
        # First sweep runs after a short delay so the poller has time
        # to populate emsc_events on cold start.
        await asyncio.sleep(30)
        while True:
            try:
                await self._sweep_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.exception("testimonies sweep failed: %s", e)
            await asyncio.sleep(SWEEP_INTERVAL_SEC)

    async def _sweep_once(self) -> None:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=SWEEP_LOOKBACK_HOURS)

        # Events from EMSC provider only — USGS event IDs don't map to
        # EMSC testimonies. We look at the LATEST revision per event
        # (the field we're writing is a running value, not per-revision).
        pipeline = [
            {"$match": {
                "ingested_at": {"$gte": cutoff},
                "provider": "EMSC",
            }},
            {"$sort": {"revision": -1}},
            {"$group": {
                "_id": "$external_id",
                "doc": {"$first": "$$ROOT"},
            }},
            {"$limit": 300},   # batch cap
        ]
        rows = [r["doc"] async for r in self.db.emsc_events.aggregate(pipeline)]
        if not rows:
            return

        # Batch-fetch testimonies for all in one HTTP call.
        unids = [r.get("external_id") for r in rows if r.get("external_id")]
        if not unids:
            return
        try:
            r = await self.client.get(
                TESTIMONIES_ENDPOINT,
                params={"unids": ",".join(unids), "includeTestimonies": "true"},
            )
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            log.warning("Testimonies fetch failed for %d unids: %s", len(unids), e)
            return

        # The API returns a list of event records; each record contains
        # `ev_nbtestimonies` (count) and (when includeTestimonies=true)
        # a `testimonies` array where each entry has raw intensity 1-12.
        by_unid = {}
        for entry in (data if isinstance(data, list) else []):
            unid = entry.get("ev_unid")
            if not unid:
                continue
            count = int(entry.get("ev_nbtestimonies") or 0)
            testimonies = entry.get("testimonies") or []
            # Extract intensities. EMSC schema:
            # each testimony has raw_intensity (1-12) OR intensity
            # (corrected). Prefer corrected, fall back to raw.
            intensities: List[float] = []
            for t in testimonies:
                v = t.get("intensity")
                if v is None:
                    v = t.get("raw_intensity")
                try:
                    if v is not None:
                        intensities.append(float(v))
                except (TypeError, ValueError):
                    continue
            max_intensity = max(intensities) if intensities else None
            by_unid[unid] = {
                "max_intensity": max_intensity,
                "report_count": count,
                "last_updated": datetime.now(timezone.utc),
            }

        # Write back — one update per event, only if data has changed
        # or was previously absent. `last_updated` is refreshed each
        # sweep so we can see the sweeper is alive from the docs.
        updated_count = 0
        for row in rows:
            unid = row.get("external_id")
            if not unid:
                continue
            new_data = by_unid.get(unid)
            if not new_data:
                # No testimony data yet — skip so we don't overwrite
                # a prior partial value with nothing.
                continue
            # Update ALL revisions of this event, so any downstream query
            # sees the same testimony data regardless of which revision
            # it hits.
            await self.db.emsc_events.update_many(
                {"provider": "EMSC", "external_id": unid},
                {"$set": {
                    "intensity_estimates.from_emsc_testimonies": new_data,
                }},
            )
            updated_count += 1

        log.info("Testimonies sweep: %d events processed, %d updated",
                 len(unids), updated_count)
