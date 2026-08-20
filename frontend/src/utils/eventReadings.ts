/**
 * The single source of truth for how magnitude, distance, depth,
 * intensity, region and coordinates are extracted from a notification
 * payload OR an in-app search-param bag.
 *
 * Neo 2026-08-20 (§1 #174 fix):
 *
 * Before this file existed, /alert and /quake/[unid] each parsed the
 * same payload keys with their own inline object. When #205's
 * single-source-of-truth fix landed on /alert but not on /quake/[unid],
 * a real EMSC preview notification arrived, was tapped, and the
 * detail screen showed "—" everywhere despite the notification body
 * carrying the full reading. Pattern #1 — "fix in one place, not
 * everywhere".
 *
 * From now on there is ONE resolver. Every surface that displays a
 * magnitude, depth, distance, intensity or coordinate for an event
 * MUST read from this. Two invariants enforced:
 *
 *   1. If a value is missing we say so plainly (`missing: true`) —
 *      callers must render "Unknown" or an explicit "the notification
 *      didn't carry this" sentence, NEVER a bare "—" that looks like
 *      data. Rule 9.4 (never present nothing as data).
 *
 *   2. The presence of coordinates is a separate check from the
 *      presence of other readings. A screen that lost magnitude
 *      still knows where the epicentre is and must offer the map
 *      button (§2 #256).
 */
import type { LocalSearchParams } from "expo-router";

/** Loose payload shape — any of alert.tsx's `params`, quake/[unid]'s
 *  params, an APNs data blob, or an activeAlert stash. */
export type EventPayloadLike =
  | Record<string, unknown>
  | LocalSearchParams<any>
  | null
  | undefined;

export type EventReadings = {
  /** Magnitude as a display string ("2.9", "M5.1") or null if missing. */
  magnitude: string | null;
  /** Depth in km as a number; null if missing. */
  depth_km: number | null;
  /** Distance in km (as carried in the payload — measured from Malta
   *  by the backend for saved-place notices, or from user for own-
   *  location notices). null if missing. */
  distance_km: number | null;
  /** Mercalli intensity string ("VII"), or null if missing (informational
   *  tremor notices don't carry intensity). */
  intensity: string | null;
  /** Region string ("Modica", "Athens"), or null if missing. */
  region: string | null;
  /** Epicentre latitude as a number, or null if missing. */
  latitude: number | null;
  /** Epicentre longitude as a number, or null if missing. */
  longitude: number | null;
  /** The event's unique id ("emsc-12345"), or null if missing. */
  unid: string | null;
  /** Observed-at ISO timestamp string, or null if missing. */
  observed_at: string | null;
  /** True when at least one of magnitude/depth/distance/intensity is
   *  missing, so the display layer can render a "Some details are
   *  missing" line instead of dashes. */
  hasMissingFields: boolean;
  /** True when latitude AND longitude are usable. Separate from
   *  hasMissingFields on purpose (§2 #256). */
  hasCoords: boolean;
};

function firstNonNull(...vs: unknown[]): unknown {
  for (const v of vs) {
    if (v !== undefined && v !== null && v !== "") return v;
  }
  return null;
}

function toNumber(v: unknown): number | null {
  if (v == null || v === "") return null;
  const n = typeof v === "number" ? v : Number(v);
  return Number.isFinite(n) ? n : null;
}

function toDisplayString(v: unknown): string | null {
  if (v == null || v === "") return null;
  return String(v);
}

/**
 * Resolve a payload into typed readings. Accepts:
 *   - The URL params from /alert or /quake/[unid]
 *   - The `data` blob from a notification
 *   - The activeAlert stash
 * plus an optional secondary source (e.g. aftershock state) that takes
 * precedence over the primary. This preserves the /alert screen's
 * "aftershock over params" behaviour while keeping the resolver
 * position-agnostic.
 */
export function resolveEventReadings(
  primary: EventPayloadLike,
  overlay?: EventPayloadLike,
): EventReadings {
  const p = (primary ?? {}) as Record<string, unknown>;
  const o = (overlay ?? {}) as Record<string, unknown>;

  const magnitude = toDisplayString(firstNonNull(o.magnitude, p.magnitude));
  const depth_km = toNumber(firstNonNull(o.depth_km, p.depth_km));
  const distance_km = toNumber(firstNonNull(o.distance_km, p.distance_km));
  const intensity = toDisplayString(firstNonNull(o.intensity, p.intensity));
  const region = toDisplayString(firstNonNull(o.region, p.region));
  const latitude = toNumber(firstNonNull(o.latitude, p.latitude));
  const longitude = toNumber(firstNonNull(o.longitude, p.longitude));
  const unid = toDisplayString(firstNonNull(o.unid, p.unid));
  const observed_at = toDisplayString(firstNonNull(o.observed_at, p.observed_at));

  const hasMissingFields =
    magnitude == null || depth_km == null || distance_km == null;
  // Deliberately NOT including `intensity == null` in the missing
  // trigger: EMSC previews never carry intensity (only real critical
  // alerts do), so counting intensity as "missing" would fire the
  // notice on every routine preview tap. If the caller wants to
  // enforce intensity presence it can read `intensity` directly.
  const hasCoords = latitude != null && longitude != null;

  return {
    magnitude, depth_km, distance_km, intensity, region,
    latitude, longitude, unid, observed_at,
    hasMissingFields, hasCoords,
  };
}
