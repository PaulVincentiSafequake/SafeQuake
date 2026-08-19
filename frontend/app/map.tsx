/**
 * In-app Seismic Map — Part 2 (2026-08-06).
 *
 * A Mediterranean-wide informational map of recent seismic activity,
 * intentionally scoped to be a *situational awareness* tool, not an
 * early-warning system.
 *
 * Design decisions locked with the user:
 *
 * 1. **Data is post-event, not predictive.** Every marker is an event
 *    that has already happened. The screen states this explicitly at
 *    the top (a red-outlined disclaimer chip) — legally and ethically
 *    critical. An earthquake app that pretends to be predictive gets
 *    people killed.
 *
 * 2. **Wide filter, always.** Per PRD:
 *       "Map always shows full Mediterranean regardless of notification
 *        preset. Preset governs only what interrupts."
 *    The user's notification preset feeds ONLY the indicative radius
 *    overlay, not the query.
 *
 * 3. **Radius circles: 600km solid, smaller dashed.** The 600km circle
 *    is the *real* poll-radius boundary — beyond it we don't ingest
 *    events at all. The 200/300km preset circles are UX communication
 *    only (real felt-area is intensity-shaped, not circular), so they
 *    render dashed to honestly signal "indicative".
 *
 * 4. **EMSC attribution required.** EMSC's data licence requires
 *    visible attribution — footer bar, not buried in settings.
 *
 * 5. **Web fallback is a list, not a fake map.** react-native-maps
 *    can't be imported on web (touches react-native internals Metro
 *    refuses to bundle). Rather than ship a broken visualization, web
 *    preview shows a chronological event list — genuinely useful during
 *    development, and matches the "informational" framing of the whole
 *    feature. The platform split lives in `src/components/MapCanvas.*`.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import {
  View, Text, StyleSheet, TouchableOpacity, ScrollView, ActivityIndicator, Platform, RefreshControl,
} from "react-native";
import { SafeAreaView } from "react-native-safe-area-context";
import { Ionicons } from "@expo/vector-icons";
import { useLocalSearchParams, useRouter } from "expo-router";
import { getDeviceId } from "@/src/utils/checkin";
import { parseUtc } from "@/src/utils/time";
import MapCanvas from "@/src/components/MapCanvas";
import type { MapCanvasEvent, MapCanvasHandle } from "@/src/components/MapCanvas.types";

const BACKEND_URL = process.env.EXPO_PUBLIC_BACKEND_URL ?? "";

// Malta reference point — matches backend country_configs.MT.center.
const MALTA = { latitude: 35.9375, longitude: 14.3754 };

type Preset = "off" | "significant" | "noticeable" | "everything";

// Preset -> indicative radius (metres). 600 km is the real poll radius
// (matches country_configs.MT.poll_radius_km) and renders SOLID. The
// smaller circles are UX communication only and render DASHED.
const PRESET_RADIUS_M: Record<Preset, number | null> = {
  off:          null,
  significant:  200_000,
  noticeable:   300_000,
  everything:   600_000,
};

type WindowChoice = 1 | 24 | 168 | 720;
const WINDOWS: { hours: WindowChoice; label: string }[] = [
  // #113 (Batch 7 D): 1-hour window. "What just happened?" is a
  // different question from "what's been going on this week?", and
  // the shortest window we offered was 24h which conflated the two.
  { hours: 1,   label: "1h" },
  { hours: 24,  label: "24h" },
  { hours: 168, label: "7d" },
  { hours: 720, label: "30d" },
];

type MapEvent = MapCanvasEvent & {
  magnitude_type?: string | null;
  depth_km?: number | null;
  providers?: string[];
  revision?: number;
};

/** #249 (Batch 7 D): a saved place fetched from the backend. Rendered
 *  as a small home marker on the native map so users can see how their
 *  informational-notice geography relates to seismic activity. */
type SavedPlace = {
  place_id: string;
  name: string;
  latitude: number;
  longitude: number;
};

// -----------------------------------------------------------------------------
// Presentation helpers (also used by the web-fallback list).
// -----------------------------------------------------------------------------

function magnitudeColor(m: number | null): string {
  if (m == null) return "#8FA0BC";
  if (m >= 5.0) return "#E64545";
  if (m >= 4.0) return "#F08A2E";
  if (m >= 3.0) return "#F4C842";
  if (m >= 2.5) return "#7BAEF7";
  return "#5D8AB8";
}

// #211 (Batch 7 D5): USGS-style recency ramp. Colour is a function of
// how RECENT the event is relative to the currently-selected window,
// not its magnitude. Red = brand new (top quarter of the window),
// green = oldest still in view. Ramp is RELATIVE to the window, so a
// "red" pin at 7d means the last day and a bit; at 1h it means the
// last fifteen minutes. The visible legend at the bottom of the map
// spells this out with the window's actual name.
const RECENCY_STOPS = [
  { color: "#D9251C", label: "Just now" },        // top ~25% of the window
  { color: "#F08A2E", label: "Recent" },          // 25–50%
  { color: "#F4C842", label: "A while back" },    // 50–75%
  { color: "#2E7D32", label: "Oldest in view" },  // 75–100%
];

function recencyColor(observedIso: string, windowHours: number): string {
  const then = parseUtc(observedIso)?.getTime();
  if (!then || !Number.isFinite(then)) return RECENCY_STOPS[3].color;
  const ageHours = Math.max(0, (Date.now() - then) / 3_600_000);
  const frac = Math.min(1, ageHours / Math.max(0.0001, windowHours));
  if (frac < 0.25) return RECENCY_STOPS[0].color;
  if (frac < 0.50) return RECENCY_STOPS[1].color;
  if (frac < 0.75) return RECENCY_STOPS[2].color;
  return RECENCY_STOPS[3].color;
}

function windowLabel(hours: number): string {
  if (hours <= 1) return "the last hour";
  if (hours <= 24) return "the last 24 hours";
  if (hours <= 168) return "the last 7 days";
  return "the last 30 days";
}

function timeAgo(iso: string): string {
  const then = parseUtc(iso)?.getTime() ?? NaN;
  if (!Number.isFinite(then)) return "";
  const s = Math.max(0, Math.floor((Date.now() - then) / 1000));
  if (s < 60)    return `${s}s ago`;
  if (s < 3600)  return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

// -----------------------------------------------------------------------------
// Screen.
// -----------------------------------------------------------------------------

export default function SeismicMapScreen() {
  const router = useRouter();
  // Opened from an event's detail screen ("see this on the map") — centre
  // on that event and emphasise its pin (#173/B5). Absent on the normal
  // entry from Home, where the whole-basin view is right.
  const focusParams = useLocalSearchParams<{
    focus_lat?: string;
    focus_lon?: string;
    focus_unid?: string;
  }>();
  const focus =
    focusParams.focus_lat && focusParams.focus_lon
      ? {
          latitude: Number(focusParams.focus_lat),
          longitude: Number(focusParams.focus_lon),
        }
      : null;
  const [events, setEvents] = useState<MapEvent[]>([]);
  const [attribution, setAttribution] = useState<string>("Data: EMSC & USGS");
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [errText, setErrText] = useState<string | null>(null);
  const [windowHours, setWindowHours] = useState<WindowChoice>(168);
  const [preset, setPreset] = useState<Preset>("noticeable");
  // #212 (Batch 7 R4, corrected 2026-08-19 night): the "Circle: ~N km
  // around Malta" caption is honest ONLY while the circle is actually
  // on screen. We track the visible map center in state and hide the
  // caption when it is more than the circle radius away from Malta —
  // regardless of how the map got there (user gesture, "See on map",
  // programmatic pan). Previous cut only listened for user gestures
  // and so kept the caption while the "See on map" button had panned
  // the map to Sicily with no circle in sight.
  const [mapCenter, setMapCenter] = useState<{ lat: number; lng: number } | null>(null);
  // #249 (Batch 7 D): user's saved places, rendered as small markers so
  // "the place I care about" is visible in relation to real activity.
  const [places, setPlaces] = useState<SavedPlace[]>([]);
  // #243 (Batch 7 D6): a ref to the native map so the "See wide view"
  // button can animate back out to the basin overview after the user
  // opens the map focused on a single event.
  const mapRef = useRef<MapCanvasHandle>(null);
  const [wideView, setWideView] = useState<boolean>(!focus);

  const fetchEvents = useCallback(async (hours: WindowChoice) => {
    setErrText(null);
    try {
      const r = await fetch(`${BACKEND_URL}/api/seismic-map/events?window_hours=${hours}&limit=500`);
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const data = await r.json();
      setEvents(Array.isArray(data.events) ? data.events : []);
      if (data.attribution) setAttribution(data.attribution);
    } catch (e: any) {
      setErrText(e?.message || "Could not load events");
      setEvents([]);
    }
  }, []);

  const fetchPlaces = useCallback(async () => {
    // Non-fatal: places are optional. Show the map with events even if
    // the places call fails offline or the device has never saved any.
    try {
      const did = await getDeviceId();
      const r = await fetch(`${BACKEND_URL}/api/devices/${encodeURIComponent(did)}/places`);
      if (r.ok) {
        const data = await r.json();
        const enabled = data.enabled !== false;
        setPlaces(enabled && Array.isArray(data.places) ? data.places : []);
      }
    } catch { /* offline — hide places */ }
  }, []);

  const fetchPreset = useCallback(async () => {
    try {
      const did = await getDeviceId();
      const r = await fetch(`${BACKEND_URL}/api/devices/${encodeURIComponent(did)}/notification-preset`);
      if (r.ok) {
        const data = await r.json();
        if (data.preset && ["off","significant","noticeable","everything"].includes(data.preset)) {
          setPreset(data.preset as Preset);
        }
      }
    } catch { /* offline — keep default */ }
  }, []);

  useEffect(() => {
    (async () => {
      setLoading(true);
      await Promise.all([fetchEvents(windowHours), fetchPreset(), fetchPlaces()]);
      setLoading(false);
    })();
    // Intentional: initial load only. Subsequent refreshes triggered by
    // pull-to-refresh or the window-toggle.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const onWindowChange = async (next: WindowChoice) => {
    if (next === windowHours) return;
    setWindowHours(next);
    setLoading(true);
    await fetchEvents(next);
    setLoading(false);
  };

  const onRefresh = async () => {
    setRefreshing(true);
    await Promise.all([fetchEvents(windowHours), fetchPreset(), fetchPlaces()]);
    setRefreshing(false);
  };

  const goToEvent = useCallback((ev: MapCanvasEvent) => {
    // Reuse the informational quake detail screen. Deliberately does
    // NOT trigger the alert flow.
    const src = events.find(e =>
      e.provider === ev.provider && e.external_id === ev.external_id
    );
    router.push({
      pathname: "/quake/[unid]" as any,
      params: {
        unid: ev.external_id,
        provider: ev.provider,
        observed_at: ev.observed_at,
        magnitude: String(ev.magnitude ?? ""),
        magnitude_type: src?.magnitude_type ?? "",
        // #173: tells the detail screen where it was opened from, so its
        // back control returns HERE (with this pan/zoom/time window intact)
        // instead of resetting to Home.
        from: "map",
        depth_km: String(src?.depth_km ?? ""),
        region: ev.region ?? "",
        latitude: String(ev.latitude),
        longitude: String(ev.longitude),
      },
    });
  }, [events, router]);

  const presetRadiusM = PRESET_RADIUS_M[preset];
  const presetIsSolid = preset === "everything";

  // #211 (Batch 7 D5): tag each event with its recency colour once,
  // window-aware, so both the native map and the web-fallback list
  // read from the same computation.
  const eventsColored: MapEvent[] = events.map((ev) => ({
    ...ev,
    recency_color: recencyColor(ev.observed_at, windowHours),
  }));

  const headerNode = (
    <View style={styles.header}>
      <TouchableOpacity
        style={styles.backBtn}
        onPress={() => router.back()}
        hitSlop={{top:12,bottom:12,left:12,right:12}}
        accessibilityRole="button" accessibilityLabel="Back"
      >
        <Ionicons name="chevron-back" size={26} color="#E7EDF5" />
      </TouchableOpacity>
      <Text style={styles.title}>Seismic activity</Text>
      <View style={{width: 26}} />
    </View>
  );

  const disclaimerNode = (
    <View style={styles.disclaimer}>
      <Ionicons name="information-circle" size={16} color="#E64545" />
      <Text style={styles.disclaimerText}>
        Post-event data. Not an early-warning system.
      </Text>
    </View>
  );

  const windowToggleNode = (
    <View style={styles.windowRow}>
      {WINDOWS.map(w => {
        const active = w.hours === windowHours;
        return (
          <TouchableOpacity
            key={w.hours}
            onPress={() => onWindowChange(w.hours)}
            style={[styles.windowChip, active && styles.windowChipActive]}
            accessibilityRole="button"
            accessibilityState={{selected: active}}
          >
            <Text style={[styles.windowChipText, active && styles.windowChipTextActive]}>
              {w.label}
            </Text>
          </TouchableOpacity>
        );
      })}
      <View style={{flex: 1}} />
      <Text style={styles.countText}>
        {loading ? "…" : `${events.length} event${events.length === 1 ? "" : "s"}`}
      </Text>
    </View>
  );

  const attributionNode = (
    <View style={styles.attributionBar}>
      <Text style={styles.attributionText} numberOfLines={2}>
        {attribution}
      </Text>
      {(() => {
        // #212 (R4): show the caption only when the map's current
        // center is within the circle's own radius of Malta — i.e.
        // when the circle is genuinely on screen. Small great-circle
        // distance approx via haversine, in km.
        if (presetRadiusM === null) return null;
        if (!mapCenter) return null;   // pre-region-report: hide by default
        const R = 6371;
        const toRad = (d: number) => (d * Math.PI) / 180;
        const dLat = toRad(mapCenter.lat - MALTA.latitude);
        const dLng = toRad(mapCenter.lng - MALTA.longitude);
        const a = Math.sin(dLat / 2) ** 2
                + Math.cos(toRad(MALTA.latitude))
                  * Math.cos(toRad(mapCenter.lat))
                  * Math.sin(dLng / 2) ** 2;
        const km = 2 * R * Math.asin(Math.min(1, Math.sqrt(a)));
        const radiusKm = presetRadiusM / 1000;
        // A little slack so the caption doesn't flicker at the boundary.
        const visible = km <= radiusKm * 1.1;
        if (!visible) return null;
        return (
          <Text style={styles.attributionSub}>
            Circle: ~{Math.round(radiusKm)} km around Malta
            {presetIsSolid
              ? " (poll radius)"
              : " (approximate — real felt area is intensity-shaped)"}
          </Text>
        );
      })()}
    </View>
  );

  // #211 (Batch 7 D5): always-visible key. Circle SIZE = magnitude,
  // COLOUR = recency (USGS convention). Names the current window so
  // "red" is honest: at 7d it means the last day and a half, at 1h
  // it means the last fifteen minutes. The one-line summary at the
  // top answers "what do these colours mean?" before the reader has
  // to read the swatches.
  const legendNode = (
    <View
      style={styles.legend}
      accessibilityRole="summary"
      accessibilityLabel={`Map key: bigger circles are stronger, redder circles are more recent, within ${windowLabel(windowHours)}`}
    >
      <Text style={styles.legendHeadline}>
        Bigger = stronger. Redder = more recent, within {windowLabel(windowHours)}.
      </Text>
      <View style={styles.legendRow}>
        <Text style={styles.legendCaption}>Size (magnitude):</Text>
        <View style={styles.legendSizeSwatch}>
          <View style={[styles.legendSizeDot, { width: 8,  height: 8,  borderRadius: 4  }]} />
          <View style={[styles.legendSizeDot, { width: 12, height: 12, borderRadius: 6  }]} />
          <View style={[styles.legendSizeDot, { width: 18, height: 18, borderRadius: 9  }]} />
          <View style={[styles.legendSizeDot, { width: 24, height: 24, borderRadius: 12 }]} />
        </View>
        <Text style={styles.legendCaptionRight}>M2 · M3 · M4 · M5+</Text>
      </View>
      <View style={styles.legendRow}>
        <Text style={styles.legendCaption}>Colour (age within {windowLabel(windowHours)}):</Text>
      </View>
      <View style={styles.legendRampRow}>
        {RECENCY_STOPS.map((s) => (
          <View key={s.color} style={styles.legendRampCell}>
            <View style={[styles.legendRampSwatch, { backgroundColor: s.color }]} />
            <Text style={styles.legendRampLabel}>{s.label}</Text>
          </View>
        ))}
      </View>
    </View>
  );

  // ----- Web fallback -----
  if (Platform.OS === "web") {
    return (
      <SafeAreaView style={styles.container} edges={["top"]}>
        {headerNode}
        {disclaimerNode}
        {windowToggleNode}

        {loading ? (
          <ActivityIndicator style={{marginTop: 40}} color="#5DB1FF" />
        ) : errText ? (
          <View style={styles.emptyPanel}>
            <Text style={styles.emptyText}>Could not load: {errText}</Text>
            <TouchableOpacity onPress={() => onRefresh()} style={styles.retryBtn}>
              <Text style={styles.retryBtnText}>Try again</Text>
            </TouchableOpacity>
          </View>
        ) : events.length === 0 ? (
          <View style={styles.emptyPanel}>
            <Ionicons name="pulse" size={28} color="#5DB1FF" />
            <Text style={styles.emptyText}>
              No recorded activity in the Mediterranean in this window.
            </Text>
          </View>
        ) : (
          <ScrollView
            contentContainerStyle={styles.listContent}
            refreshControl={<RefreshControl refreshing={refreshing} onRefresh={onRefresh} tintColor="#5DB1FF" />}
          >
            <View style={styles.webNoticeBox}>
              <Ionicons name="phone-portrait-outline" size={16} color="#8FA0BC" />
              <Text style={styles.webNoticeText}>
                Full map view available on iOS and Android. Showing a chronological list here.
              </Text>
            </View>

            {events.map(ev => (
              <TouchableOpacity
                key={`${ev.provider}-${ev.external_id}`}
                style={styles.listRow}
                onPress={() => goToEvent(ev)}
                accessibilityRole="button"
                accessibilityLabel={`Magnitude ${ev.magnitude} in ${ev.region ?? "unknown region"}`}
              >
                <View style={[styles.magBadge, {backgroundColor: magnitudeColor(ev.magnitude)}]}>
                  <Text style={styles.magBadgeText}>
                    {ev.magnitude != null ? ev.magnitude.toFixed(1) : "—"}
                  </Text>
                </View>
                <View style={{flex: 1}}>
                  <Text style={styles.listRegion} numberOfLines={1}>
                    {ev.region ?? "Unknown region"}
                  </Text>
                  <Text style={styles.listMeta} numberOfLines={1}>
                    {timeAgo(ev.observed_at)} · depth {ev.depth_km != null ? `${Math.round(ev.depth_km)} km` : "—"} · {(ev.providers ?? [ev.provider]).join("+")}
                  </Text>
                </View>
                <Ionicons name="chevron-forward" size={18} color="#8FA0BC" />
              </TouchableOpacity>
            ))}
          </ScrollView>
        )}

        {legendNode}
        {attributionNode}
      </SafeAreaView>
    );
  }

  // ----- Native -----
  return (
    <SafeAreaView style={styles.container} edges={["top"]}>
      {headerNode}
      {disclaimerNode}
      {windowToggleNode}

      <View style={{flex: 1}}>
        <MapCanvas
          ref={mapRef}
          events={eventsColored}
          center={MALTA}
          focus={focus}
          highlightExternalId={focusParams.focus_unid ?? null}
          radiusMeters={presetRadiusM}
          radiusIsSolid={presetIsSolid}
          onEventPress={goToEvent}
          onRegionChange={(lat, lng) => setMapCenter({ lat, lng })}
          places={places}
        />
        {/* #243 (Batch 7 D6): "See wide view" pill appears when we
            landed focused on an event. Spec wording: "Zoom in to where
            it happened (epicentre)" is what the source button says;
            here we offer the reverse. */}
        {focus && !wideView ? (
          <View style={styles.wideBtnWrap} pointerEvents="box-none">
            <TouchableOpacity
              style={styles.wideBtn}
              onPress={() => {
                mapRef.current?.animateToWideView();
                setWideView(true);
              }}
              accessibilityRole="button"
              accessibilityLabel="See wider view of the Mediterranean"
              testID="map-wide-view-btn"
            >
              <Ionicons name="contract-outline" size={16} color="#0B1220" />
              <Text style={styles.wideBtnText}>See wider view</Text>
            </TouchableOpacity>
            <Text style={styles.wideBtnCaption}>
              Zoomed in to where it happened (epicentre)
            </Text>
          </View>
        ) : null}
        {loading && (
          <View style={styles.mapLoader}>
            <ActivityIndicator color="#5DB1FF" />
          </View>
        )}
      </View>

      {legendNode}
      {attributionNode}
    </SafeAreaView>
  );
}

// -----------------------------------------------------------------------------
// Styles.
// -----------------------------------------------------------------------------

const styles = StyleSheet.create({
  container: { flex: 1, backgroundColor: "#0B1220" },

  header: {
    flexDirection: "row", alignItems: "center", justifyContent: "space-between",
    paddingHorizontal: 16, paddingVertical: 12,
    borderBottomWidth: 1, borderBottomColor: "#25324A",
  },
  backBtn: { padding: 4 },
  title: { color: "#E7EDF5", fontSize: 18, fontWeight: "700" },

  disclaimer: {
    flexDirection: "row", alignItems: "center", gap: 8,
    marginHorizontal: 16, marginTop: 12, marginBottom: 8,
    paddingVertical: 8, paddingHorizontal: 12,
    borderRadius: 8, borderWidth: 1, borderColor: "#E64545",
    backgroundColor: "#3A1919",
  },
  disclaimerText: {
    color: "#FFB4B4", fontSize: 12, fontWeight: "600", flexShrink: 1,
  },

  windowRow: {
    flexDirection: "row", alignItems: "center", gap: 8,
    paddingHorizontal: 16, paddingBottom: 10,
  },
  windowChip: {
    paddingHorizontal: 12, paddingVertical: 6,
    borderRadius: 999, borderWidth: 1, borderColor: "#25324A",
    backgroundColor: "#151E2F",
  },
  windowChipActive: { borderColor: "#5DB1FF", backgroundColor: "#0F2540" },
  windowChipText: { color: "#8FA0BC", fontSize: 12, fontWeight: "600" },
  windowChipTextActive: { color: "#5DB1FF" },
  countText: { color: "#8FA0BC", fontSize: 12 },

  mapLoader: {
    position: "absolute", top: 12, alignSelf: "center",
    backgroundColor: "rgba(11,18,32,0.75)", paddingHorizontal: 12, paddingVertical: 8,
    borderRadius: 999,
  },

  // #243 (Batch 7 D6): "See wider view" pill overlaid at the bottom
  // of the map when we landed focused on an event, plus the caption
  // that confirms why we're zoomed in.
  wideBtnWrap: {
    position: "absolute", bottom: 16, left: 0, right: 0,
    alignItems: "center", gap: 6,
  },
  wideBtn: {
    flexDirection: "row", alignItems: "center", gap: 6,
    backgroundColor: "#5DB1FF", borderRadius: 999,
    paddingHorizontal: 14, paddingVertical: 8,
    shadowColor: "#000", shadowOpacity: 0.35, shadowRadius: 4, shadowOffset: {width: 0, height: 2},
    elevation: 4,
  },
  wideBtnText: { color: "#0B1220", fontWeight: "800", fontSize: 13 },
  wideBtnCaption: {
    color: "#E7EDF5", fontSize: 11, fontWeight: "600",
    backgroundColor: "rgba(11,18,32,0.75)",
    paddingHorizontal: 8, paddingVertical: 3, borderRadius: 6,
  },

  listContent: { paddingHorizontal: 16, paddingBottom: 20 },
  webNoticeBox: {
    flexDirection: "row", alignItems: "center", gap: 8,
    padding: 10, marginTop: 4, marginBottom: 12,
    borderRadius: 8, backgroundColor: "#151E2F", borderWidth: 1, borderColor: "#25324A",
  },
  webNoticeText: { color: "#8FA0BC", fontSize: 12, flexShrink: 1 },

  listRow: {
    flexDirection: "row", alignItems: "center", gap: 12,
    paddingVertical: 12, paddingHorizontal: 12,
    marginBottom: 8, borderRadius: 10,
    backgroundColor: "#151E2F", borderWidth: 1, borderColor: "#25324A",
  },
  magBadge: {
    width: 44, height: 44, borderRadius: 22,
    alignItems: "center", justifyContent: "center",
  },
  magBadgeText: { color: "#0B1220", fontWeight: "800", fontSize: 15 },
  listRegion: { color: "#E7EDF5", fontSize: 14, fontWeight: "700" },
  listMeta: { color: "#8FA0BC", fontSize: 12, marginTop: 2 },

  emptyPanel: {
    alignItems: "center", justifyContent: "center",
    padding: 32, marginTop: 24, gap: 12,
  },
  emptyText: { color: "#8FA0BC", fontSize: 14, textAlign: "center" },
  retryBtn: {
    backgroundColor: "#0F2540", borderColor: "#5DB1FF", borderWidth: 1,
    borderRadius: 8, paddingVertical: 10, paddingHorizontal: 18,
  },
  retryBtnText: { color: "#5DB1FF", fontWeight: "700" },

  attributionBar: {
    paddingHorizontal: 16, paddingTop: 8, paddingBottom: 10,
    borderTopWidth: 1, borderTopColor: "#25324A", backgroundColor: "#0B1220",
  },
  attributionText: { color: "#8FA0BC", fontSize: 11, fontWeight: "600" },
  attributionSub: { color: "#5A6B85", fontSize: 10, marginTop: 2 },

  // #211 (Batch 7 D5): always-visible map key. Sits above the
  // attribution bar so it never scrolls off. Compact but honest —
  // headline sentence first, then swatches.
  legend: {
    backgroundColor: "#0B1220",
    borderTopWidth: 1, borderTopColor: "#25324A",
    paddingHorizontal: 12, paddingTop: 8, paddingBottom: 8, gap: 6,
  },
  legendHeadline: {
    color: "#E7EDF5", fontSize: 12, fontWeight: "700", lineHeight: 16,
  },
  legendRow: {
    flexDirection: "row", alignItems: "center", gap: 8,
  },
  legendCaption: {
    color: "#8FA0BC", fontSize: 10, fontWeight: "600",
  },
  legendCaptionRight: {
    color: "#8FA0BC", fontSize: 10, marginLeft: "auto",
  },
  legendSizeSwatch: {
    flexDirection: "row", alignItems: "center", gap: 6,
  },
  legendSizeDot: {
    backgroundColor: "#F4C842", borderWidth: 1, borderColor: "#0B1220",
  },
  legendRampRow: {
    flexDirection: "row", gap: 6, marginTop: 2,
  },
  legendRampCell: {
    flex: 1, alignItems: "center", gap: 2,
  },
  legendRampSwatch: {
    width: "100%", height: 8, borderRadius: 2,
  },
  legendRampLabel: {
    color: "#8FA0BC", fontSize: 9, textAlign: "center",
  },
});
