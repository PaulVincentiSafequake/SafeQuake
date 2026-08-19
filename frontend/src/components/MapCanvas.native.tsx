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
import { forwardRef, useImperativeHandle, useRef } from "react";
import { View, StyleSheet, Text as RNText } from "react-native";
import MapView, { Marker, Circle, PROVIDER_DEFAULT } from "react-native-maps";
import type { MapCanvasProps, MapCanvasEvent, MapCanvasHandle } from "./MapCanvas.types";
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
  // Fallback only — used when the parent hasn't computed a recency
  // colour (e.g. web-list fallback where colour still tracks size).
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

export default forwardRef<MapCanvasHandle, MapCanvasProps>(function MapCanvas(props, ref) {
  const {
    events, center, radiusMeters, radiusIsSolid, onEventPress,
    focus = null, highlightExternalId = null, onRegionChange,
    places = [],
  } = props;
  const mapRef = useRef<MapView | null>(null);
  // #243 (Batch 7 D6): imperative API used by the "See wide view"
  // button in map.tsx to animate back to the basin overview from a
  // deep event focus.
  useImperativeHandle(ref, () => ({
    animateToWideView: () => {
      try { mapRef.current?.animateToRegion(INITIAL_REGION, 400); } catch { /* non-fatal */ }
    },
  }), []);
  // Opened from an event ("See this on the map"): start tight on the
  // epicentre so the user doesn't have to hunt for it. #243 (Batch 7
  // D6): the previous 4° delta (~440 km) landed as a wide Malta-and-
  // Sicily view — the person had to guess which pin was theirs. The
  // spec is "Zoom in to where it happened (epicentre)", so we frame
  // roughly 1.4° (~155 km) which sits an event pin comfortably in
  // the centre with enough context to see neighbouring pins and the
  // nearest coastline. Zooming BACK out to the basin is a one-tap
  // button in the parent screen.
  const initialRegion = focus
    ? {
        latitude: focus.latitude,
        longitude: focus.longitude,
        latitudeDelta: 1.4,
        longitudeDelta: 1.4,
      }
    : INITIAL_REGION;
  return (
    <MapView
      ref={mapRef}
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
                  // #211 (Batch 7 D5): map colour is RECENCY (parent
                  // computed against the visible window) — magnitude
                  // drives SIZE, following USGS convention. If no
                  // recency colour arrived (shouldn't happen), fall
                  // back to the magnitude palette so pins never turn
                  // grey silently.
                  backgroundColor: ev.recency_color ?? magnitudeColor(ev.magnitude),
                },
                highlighted && styles.markerHighlighted,
              ]}
            />
          </Marker>
        );
      })}

      {/* #249 (Batch 7 D): saved-place markers render UNDER the event
          pins in DOM order by convention (react-native-maps has no
          real z-index for markers on Android, but a small home dot
          with a name label reads clearly against event circles). */}
      {places.map((p) => (
        <Marker
          key={`place-${p.place_id}`}
          coordinate={{ latitude: p.latitude, longitude: p.longitude }}
          tracksViewChanges={false}
          anchor={{x: 0.5, y: 0.5}}
          accessibilityLabel={`Saved place: ${p.name}`}
        >
          <View style={styles.placeMarker}>
            <View style={styles.placeDot} />
            <View style={styles.placeLabel}>
              <RNText
                style={styles.placeLabelText}
                numberOfLines={1}
              >
                {p.name}
              </RNText>
            </View>
          </View>
        </Marker>
      ))}
    </MapView>
  );
});

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
  // #249 (Batch 7 D): saved-place marker — deliberately not colourful
  // so it never competes visually with an event pin. A small teal dot
  // under a compact label the user chose ("Mum's house").
  placeMarker: {
    alignItems: "center",
    justifyContent: "center",
  },
  placeDot: {
    width: 12, height: 12, borderRadius: 6,
    backgroundColor: "#5DB1FF",
    borderWidth: 2, borderColor: "#0B1220",
  },
  placeLabel: {
    marginTop: 2,
    backgroundColor: "rgba(11,18,32,0.85)",
    borderRadius: 4,
    paddingHorizontal: 5, paddingVertical: 1,
    maxWidth: 100,
  },
  placeLabelText: {
    color: "#E7EDF5", fontSize: 10, fontWeight: "700",
  },
});
