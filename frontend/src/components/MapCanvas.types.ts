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
};
