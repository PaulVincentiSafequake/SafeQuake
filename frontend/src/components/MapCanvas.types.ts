/**
 * Shared prop shape for MapCanvas. Both the web stub and the native
 * implementation import this so the seismic-map screen doesn't need
 * platform-conditional types at the call site.
 */

export type MapCanvasEvent = {
  provider: string;
  external_id: string;
  observed_at: string;
  magnitude: number | null;
  latitude: number;
  longitude: number;
  region?: string | null;
  /** #211 (Batch 7 D5): USGS convention — colour on the map is
   *  RECENCY relative to the selected time window, not magnitude.
   *  The parent computes this so a single window-aware ramp lives
   *  in one place and every renderer stays in sync. */
  recency_color?: string;
};

/** #249 (Batch 7 D): a user's saved place, drawn as a small labelled
 *  home-icon marker on the map so the person can see how their
 *  informational-notice geography relates to real seismic activity. */
export type MapCanvasPlace = {
  place_id: string;
  name: string;
  latitude: number;
  longitude: number;
};

export type MapCanvasProps = {
  events: MapCanvasEvent[];
  /** Circle centre (typically Malta). */
  center: { latitude: number; longitude: number };
  /** Radius in metres, or null to hide the circle. */
  radiusMeters: number | null;
  /** true = solid (real boundary); false = dashed (indicative). */
  radiusIsSolid: boolean;
  onEventPress: (ev: MapCanvasEvent) => void;
  /**
   * Centre the map here on first render instead of the default region.
   * Used when the map is opened from an event's detail screen ("see this
   * on the map"), so the user lands on the event rather than having to
   * hunt for it.
   */
  focus?: { latitude: number; longitude: number } | null;
  /** external_id of the event to draw emphasised (ring + larger dot). */
  highlightExternalId?: string | null;
  /** #249: places to draw as home-icon markers under their names. */
  places?: MapCanvasPlace[];
  /**
   * Called after any map region change — user gesture OR programmatic.
   * #212 (R4): supplies the current visible center so the parent can
   * decide whether the circle-around-Malta caption is still relevant.
   * All region changes report; the parent decides — the previous
   * gesture-only filter missed the "See on map" case.
   */
  onRegionChange?: (latitude: number, longitude: number) => void;
};

/** #243 (Batch 7 D6): imperative handle exposed by the native map so
 *  the parent screen can offer a "See wide view" button that animates
 *  back to the basin overview when the map is opened focused on an
 *  event. */
export type MapCanvasHandle = {
  animateToWideView: () => void;
};
