/**
 * Native (iOS/Android) map canvas using react-native-maps.
 *
 * See MapCanvas.tsx (web stub) for why this is a separate file.
 *
 * Design notes:
 * - `showsCompass`, `rotateEnabled`, `pitchEnabled` are all off. The map
 *   is a data visualization, not a navigation tool — allowing the user
 *   to tilt/rotate it just makes the map harder to read at a glance
 *   without adding any real value.
 * - `tracksViewChanges={false}` on markers is important for performance
 *   when there are 100+ dots. Without it react-native-maps re-renders
 *   marker textures on every camera change and the map stutters.
 * - The indicative radius circle uses lineDashPattern on iOS. Android
 *   ignores it silently (a react-native-maps limitation, tracked
 *   upstream) — the circle still appears at the right radius, just
 *   solid on Android. This is deliberately accepted rather than
 *   worked-around with a client-drawn SVG overlay because the
 *   iOS-first delivery target treats iOS behaviour as authoritative.
 */
import { View, StyleSheet } from "react-native";
import MapView, { Marker, Circle, PROVIDER_DEFAULT } from "react-native-maps";
import type { MapCanvasProps, MapCanvasEvent } from "./MapCanvas.types";
import { parseUtc } from "@/src/utils/time";

// Initial camera framing: shows central-and-eastern Mediterranean
// (where soak coverage is densest) centered on Malta.
const INITIAL_REGION = {
  latitude: 37.5,
  longitude: 17.0,
  latitudeDelta: 16.0,
  longitudeDelta: 22.0,
};

function magnitudeColor(m: number | null): string {
  if (m == null) return "#8FA0BC";
  if (m >= 5.0) return "#E64545";
  if (m >= 4.0) return "#F08A2E";
  if (m >= 3.0) return "#F4C842";
  if (m >= 2.5) return "#7BAEF7";
  return "#5D8AB8";
}

function magnitudeSize(m: number | null): number {
  if (m == null) return 10;
  return Math.max(10, Math.min(30, 8 + m * 3));
}

function timeAgoShort(iso: string): string {
  const then = parseUtc(iso)?.getTime() ?? NaN;
  if (!Number.isFinite(then)) return "";
  const s = Math.max(0, Math.floor((Date.now() - then) / 1000));
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.floor(s/60)}m ago`;
  if (s < 86400) return `${Math.floor(s/3600)}h ago`;
  return `${Math.floor(s/86400)}d ago`;
}

export default function MapCanvas(props: MapCanvasProps) {
  const {
    events, center, radiusMeters, radiusIsSolid, onEventPress,
    focus = null, highlightExternalId = null, onRegionChange,
  } = props;
  // Opened from an event ("see this on the map"): start tight on that
  // event instead of the whole basin, so it doesn't have to be hunted for.
  const initialRegion = focus
    ? {
        latitude: focus.latitude,
        longitude: focus.longitude,
        latitudeDelta: 4.0,
        longitudeDelta: 4.0,
      }
    : INITIAL_REGION;
  return (
    <MapView
      style={StyleSheet.absoluteFillObject}
      provider={PROVIDER_DEFAULT}
      initialRegion={initialRegion}
      showsCompass={false}
      showsUserLocation={false}
      rotateEnabled={false}
      pitchEnabled={false}
      toolbarEnabled={false}
      // #212 (Batch 7 R4, corrected 2026-08-19 night):
      // The previous cut only listened for `isGesture=true` region
      // changes so the initial programmatic settle wouldn't hide the
      // caption. But the MOST COMMON way the map ends up far from
      // Malta is `See on map` on an event — which is a PROGRAMMATIC
      // move. Filtering programmatic moves out was exactly wrong: the
      // one path that takes you away was the one the listener ignored.
      //
      // New rule: report EVERY region change, and let the parent
      // decide whether the circle is still on screen. Center of the
      // visible region is the honest signal — how the map got there
      // doesn't matter.
      onRegionChangeComplete={(region) => {
        if (onRegionChange) {
          try { onRegionChange(region.latitude, region.longitude); } catch { /* non-fatal */ }
        }
      }}
    >
      {radiusMeters !== null && (
        <Circle
          center={center}
          radius={radiusMeters}
          strokeWidth={2}
          strokeColor={radiusIsSolid ? "rgba(93, 177, 255, 0.90)" : "rgba(93, 177, 255, 0.75)"}
          fillColor="rgba(93, 177, 255, 0.06)"
          lineDashPattern={radiusIsSolid ? undefined : [8, 6]}
        />
      )}

      {events.map((ev: MapCanvasEvent) => {
        const highlighted = highlightExternalId != null
          && ev.external_id === highlightExternalId;
        const size = magnitudeSize(ev.magnitude) * (highlighted ? 1.6 : 1);
        return (
          <Marker
            key={`${ev.provider}-${ev.external_id}`}
            coordinate={{ latitude: ev.latitude, longitude: ev.longitude }}
            onPress={() => onEventPress(ev)}
            tracksViewChanges={false}
            anchor={{x: 0.5, y: 0.5}}
            accessibilityLabel={`Magnitude ${ev.magnitude} ${ev.region ?? ""} ${timeAgoShort(ev.observed_at)}`}
          >
            <View
              style={[
                styles.markerDot,
                {
                  width: size,
                  height: size,
                  borderRadius: size / 2,
                  backgroundColor: magnitudeColor(ev.magnitude),
                },
                highlighted && styles.markerHighlighted,
              ]}
            />
          </Marker>
        );
      })}
    </MapView>
  );
}

const styles = StyleSheet.create({
  markerHighlighted: {
    borderWidth: 3,
    borderColor: "#FFFFFF",
    shadowColor: "#000",
    shadowOpacity: 0.5,
    shadowRadius: 4,
    shadowOffset: { width: 0, height: 1 },
  },
  markerDot: {
    borderWidth: 1.5, borderColor: "#0B1220",
    shadowColor: "#000", shadowOpacity: 0.4, shadowRadius: 2, shadowOffset: {width:0, height:1},
  },
});
