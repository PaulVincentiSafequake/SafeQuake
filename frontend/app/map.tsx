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
import { useCallback, useEffect, useState } from "react";
import {
  View, Text, StyleSheet, TouchableOpacity, ScrollView, ActivityIndicator, Platform, RefreshControl,
} from "react-native";
import { SafeAreaView } from "react-native-safe-area-context";
import { Ionicons } from "@expo/vector-icons";
import { useLocalSearchParams, useRouter } from "expo-router";
import { getDeviceId } from "@/src/utils/checkin";
import { parseUtc } from "@/src/utils/time";
import MapCanvas from "@/src/components/MapCanvas";
import type { MapCanvasEvent } from "@/src/components/MapCanvas.types";

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

type WindowChoice = 24 | 168 | 720;
const WINDOWS: { hours: WindowChoice; label: string }[] = [
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
  // #212 (Batch 7): the "Circle: ~N km around Malta" caption only
  // makes sense while the circle is actually on screen. As soon as the
  // user has panned or zoomed the map away from the initial view we
  // can no longer guarantee that, so the caption hides itself. It comes
  // back on a fresh mount / when the preset radius changes to Everything
  // (the 600 km poll radius always frames what data is on-screen).
  const [userMovedMap, setUserMovedMap] = useState(false);

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
      await Promise.all([fetchEvents(windowHours), fetchPreset()]);
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
    await Promise.all([fetchEvents(windowHours), fetchPreset()]);
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
      {presetRadiusM !== null && !userMovedMap && (
        <Text style={styles.attributionSub}>
          Circle: ~{Math.round(presetRadiusM/1000)} km around Malta
          {presetIsSolid ? " (poll radius)" : " (approximate — real felt area is intensity-shaped)"}
        </Text>
      )}
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
          events={events}
          center={MALTA}
          focus={focus}
          highlightExternalId={focusParams.focus_unid ?? null}
          radiusMeters={presetRadiusM}
          radiusIsSolid={presetIsSolid}
          onEventPress={goToEvent}
          onUserMoved={() => setUserMovedMap(true)}
        />
        {loading && (
          <View style={styles.mapLoader}>
            <ActivityIndicator color="#5DB1FF" />
          </View>
        )}
      </View>

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
});
