"""Provider abstractions for the EMSC Phase 1 shadow-mode poller.

Two independent providers are polled concurrently and stored side-by-side
in the same collection, tagged by `provider`. Phase 1 does NOT attempt
cross-provider deduplication — divergence between the two feeds is
precisely the signal we're trying to observe during soak (which one
reports first, how often they disagree on magnitude, coverage gaps
around Malta, etc.). Cross-matching is a Phase 2 concern.

Each provider returns a list of `RawEvent`s — a normalized shape that
strips the provider's transport format but preserves the source payload
verbatim under `raw` for post-hoc analysis.
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, List, Optional

import httpx


log = logging.getLogger(__name__)


@dataclass
class RawEvent:
    """Normalized event shape used across both providers.

    All fields the evaluator needs are surfaced at the top level; the
    original provider payload is preserved verbatim under `raw` so any
    field we didn't foresee remains queryable during soak analysis.

    `external_id` is the provider's own primary key. Combined with
    `provider`, it forms the (provider, external_id) uniqueness key we
    use for revision tracking — same external_id polled twice with
    different magnitude/location/depth counts as a revision, not a
    duplicate.
    """
    provider: str                            # "EMSC" | "USGS"
    external_id: str                         # provider's event id (unid / id)
    observed_at: datetime                    # event origin time (UTC)
    magnitude: float                         # e.g. 3.4
    magnitude_type: Optional[str]            # e.g. "mb", "ML", "Mw"
    latitude: float
    longitude: float
    depth_km: Optional[float]                # nullable — depth is sometimes missing
    region: Optional[str] = None             # human-readable region name if provided
    raw: dict = field(default_factory=dict)  # verbatim provider payload


class Provider(ABC):
    """Base class. Subclasses implement fetch() to return a list of
    RawEvent objects for the requested time window. Errors bubble up so
    the poller can log them into emsc_poller_health.
    """
    name: str = "abstract"

    @abstractmethod
    async def fetch(self, since: datetime, min_magnitude: float) -> List[RawEvent]:
        raise NotImplementedError


# ── EMSC seismicportal FDSN endpoint ─────────────────────────────────────
#
# API docs: https://www.seismicportal.eu/fdsnws/event/1/
# Format: GeoJSON FeatureCollection.
# License: CC-BY 4.0 (attribution required — captured in PRD.md).
# No API key. Free. Public interest / research feed.
#
# A single request pulls up to `limit` events matching the filter; we
# request format=json (GeoJSON), a 60-minute window with overlap for
# safety, and a magnitude cutoff matching the wide-net phase-1 config.
class EMSCProvider(Provider):
    name = "EMSC"
    endpoint = "https://www.seismicportal.eu/fdsnws/event/1/query"

    def __init__(self, client: httpx.AsyncClient):
        self.client = client

    async def fetch(self, since: datetime, min_magnitude: float) -> List[RawEvent]:
        params = {
            "format": "json",
            "start": since.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", ""),
            "minmag": str(min_magnitude),
            "limit": "500",
            "orderby": "time-asc",
        }
        r = await self.client.get(self.endpoint, params=params, timeout=20.0)
        # EMSC returns 204 No Content when no events match — treat as empty.
        if r.status_code == 204:
            return []
        r.raise_for_status()
        payload = r.json() or {}
        features = payload.get("features") or []
        events: List[RawEvent] = []
        for f in features:
            props = f.get("properties") or {}
            geom = f.get("geometry") or {}
            coords = geom.get("coordinates") or [None, None, None]
            try:
                observed_at = _parse_iso(props.get("time"))
                if observed_at is None:
                    continue
                lat = _to_float(props.get("lat")) if props.get("lat") is not None else _to_float(coords[1])
                lon = _to_float(props.get("lon")) if props.get("lon") is not None else _to_float(coords[0])
                depth = _to_float(props.get("depth"))
                if depth is None and len(coords) >= 3:
                    # GeoJSON depth is expressed in negative km below sea level
                    # for some feeds; EMSC uses positive km below surface. We
                    # take the positive magnitude.
                    depth = abs(_to_float(coords[2]) or 0.0)
                mag = _to_float(props.get("mag"))
                if mag is None or lat is None or lon is None:
                    continue
                events.append(RawEvent(
                    provider=self.name,
                    external_id=str(f.get("id") or props.get("unid") or props.get("source_id") or ""),
                    observed_at=observed_at,
                    magnitude=mag,
                    magnitude_type=(props.get("magtype") or None),
                    latitude=lat,
                    longitude=lon,
                    depth_km=depth,
                    region=(props.get("flynn_region") or None),
                    raw=f,
                ))
            except Exception as e:
                log.warning("EMSC feature parse failed: %s -- feature id=%s", e, f.get("id"))
                continue
        return events


# ── USGS earthquake.usgs.gov all_hour GeoJSON feed ───────────────────────
#
# API docs: https://earthquake.usgs.gov/earthquakes/feed/v1.0/geojson.php
# License: public domain (17 U.S.C. §105).
# No API key. Free.
#
# The `all_hour.geojson` feed contains every event USGS knows about in
# the last 60 minutes worldwide, revised in place. We poll it every
# 60s and let the revision-detection layer handle the fact that we'll
# see the same event repeatedly.
class USGSProvider(Provider):
    name = "USGS"
    endpoint = "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/all_hour.geojson"

    def __init__(self, client: httpx.AsyncClient):
        self.client = client

    async def fetch(self, since: datetime, min_magnitude: float) -> List[RawEvent]:
        r = await self.client.get(self.endpoint, timeout=20.0)
        r.raise_for_status()
        payload = r.json() or {}
        features = payload.get("features") or []
        cutoff = since.astimezone(timezone.utc)
        events: List[RawEvent] = []
        for f in features:
            props = f.get("properties") or {}
            geom = f.get("geometry") or {}
            coords = geom.get("coordinates") or [None, None, None]
            try:
                mag = _to_float(props.get("mag"))
                if mag is None or mag < min_magnitude:
                    continue
                # USGS time is epoch milliseconds UTC
                ts_ms = props.get("time")
                if ts_ms is None:
                    continue
                observed_at = datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc)
                if observed_at < cutoff:
                    continue
                if len(coords) < 2:
                    continue
                lon, lat = _to_float(coords[0]), _to_float(coords[1])
                depth = _to_float(coords[2]) if len(coords) >= 3 else None
                if lat is None or lon is None:
                    continue
                events.append(RawEvent(
                    provider=self.name,
                    external_id=str(f.get("id") or props.get("code") or ""),
                    observed_at=observed_at,
                    magnitude=mag,
                    magnitude_type=(props.get("magType") or None),
                    latitude=lat,
                    longitude=lon,
                    depth_km=depth,
                    region=(props.get("place") or None),
                    raw=f,
                ))
            except Exception as e:
                log.warning("USGS feature parse failed: %s -- feature id=%s", e, f.get("id"))
                continue
        return events


# ── Helpers ──────────────────────────────────────────────────────────────
def _to_float(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _parse_iso(s: Any) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp into an aware UTC datetime. Returns
    None if unparseable. Handles both trailing-Z and +00:00 variants."""
    if not s:
        return None
    if isinstance(s, datetime):
        return s if s.tzinfo else s.replace(tzinfo=timezone.utc)
    try:
        if isinstance(s, str) and s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(str(s))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None
