/**
 * Informational earthquake detail screen — the safe target for preview
 * notification taps and unknown-kind notification taps.
 *
 * BUG-2026-08-06-preview-tap-siren fix. Previously, tapping a preview
 * notification routed to /alert (siren + "Drop. Cover. Hold on."). That
 * is the exact alert-fatigue failure the preview-mode constraints exist
 * to prevent — if a routine tremor notice produces a siren, users turn
 * notifications off entirely and never receive the real alert.
 *
 * This screen renders event details in a calm, informational tone:
 *   - Magnitude, distance, depth
 *   - Approximate compass region
 *   - Time-ago
 *   - Coordinates (map integration deferred to Part 2 seismic-map work;
 *     text description is enough for the safety fix)
 *   - EMSC attribution (licence condition)
 *
 * Deliberately NOT here:
 *   - Any siren, alarm sound, or vibration
 *   - "EARTHQUAKE DETECTED" language
 *   - "Drop. Cover. Hold on." instructions
 *   - The "I'm Safe / Trapped" check-in flow
 *
 * Fail-safe target for any notification whose `kind` is missing or
 * unrecognised. A missed siren on tap is recoverable; a spurious siren
 * destroys trust permanently.
 */
import { useLocalSearchParams, useRouter } from "expo-router";
import { useEffect, useState } from "react";
import { View, Text, ScrollView, StyleSheet, TouchableOpacity, Linking } from "react-native";
import { SafeAreaView } from "react-native-safe-area-context";
import { Ionicons } from "@expo/vector-icons";
import * as Location from "expo-location";

// Fallback reference point when we have no user location: the Malta/Gozo
// archipelago centre (same coordinates as the MT country_config on the
// backend). ALWAYS labelled as such on screen — never silently substituted
// for the user's own position (batch 5, B3).
const MALTA_CENTER = { lat: 35.9375, lon: 14.3754 };
const MALTA_LABEL = "Malta";

function haversineKm(
  lat1: number, lon1: number, lat2: number, lon2: number,
): number {
  const R = 6371;
  const dLat = ((lat2 - lat1) * Math.PI) / 180;
  const dLon = ((lon2 - lon1) * Math.PI) / 180;
  const a =
    Math.sin(dLat / 2) ** 2 +
    Math.cos((lat1 * Math.PI) / 180) *
      Math.cos((lat2 * Math.PI) / 180) *
      Math.sin(dLon / 2) ** 2;
  return 2 * R * Math.asin(Math.sqrt(a));
}

const colors = {
  bg: "#0B1220",
  card: "#151E2F",
  text: "#E7EDF5",
  textDim: "#8FA0BC",
  accent: "#5DB1FF",
  border: "#25324A",
};

export default function QuakeDetailScreen() {
  const router = useRouter();
  const params = useLocalSearchParams<{
    unid?: string;
    provider?: string;
    magnitude?: string;
    distance_km?: string;
    depth_km?: string;
    latitude?: string;
    longitude?: string;
    region?: string;
    observed_at?: string;
    preview?: string;
  }>();

  const isPreview = params.preview === "true" || params.preview === "1";
  const observedAt = params.observed_at ? new Date(params.observed_at) : null;
  const observedAtValid = observedAt && !isNaN(observedAt.getTime());

  const timeAgo = (() => {
    if (!observedAtValid) return "—";
    const s = Math.floor((Date.now() - (observedAt as Date).getTime()) / 1000);
    if (s < 60) return `${s}s ago`;
    if (s < 3600) return `${Math.floor(s / 60)} min ago`;
    if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
    return `${Math.floor(s / 86400)} days ago`;
  })();

  const magnitude = params.magnitude ?? null;
  const distanceKm = params.distance_km ?? null;
  const depthKm = params.depth_km ?? null;
  const region = params.region ?? null;
  const lat = params.latitude ?? null;
  const lon = params.longitude ?? null;

  // ── Distance (batch 5, B3) ───────────────────────────────────────────
  // Was rendering "—" whenever the screen was opened from the in-app
  // seismic map (map.tsx passes no distance_km), which ALSO left the
  // plain-language sentence with a hole in the middle of it.
  //
  // Resolution order, each labelled honestly on screen:
  //   1. Distance from the user's own position — only if location
  //      permission is ALREADY granted. This screen never prompts; it is
  //      informational and a permission dialog here would be ambush-y.
  //   2. Distance from Malta, explicitly stated as such.
  //   3. Nothing at all — the row is omitted and the sentence drops the
  //      distance clause entirely. It must never render with a gap.
  const [distance, setDistance] = useState<{ km: number; from: string } | null>(
    null,
  );

  useEffect(() => {
    const eLat = lat != null ? Number(lat) : NaN;
    const eLon = lon != null ? Number(lon) : NaN;
    let cancelled = false;

    (async () => {
      if (!Number.isFinite(eLat) || !Number.isFinite(eLon)) {
        // No epicentre coords — fall back to whatever the notification
        // payload carried (that value is measured from Malta).
        const fromPayload = distanceKm != null ? Number(distanceKm) : NaN;
        if (Number.isFinite(fromPayload) && !cancelled) {
          setDistance({ km: fromPayload, from: MALTA_LABEL });
        }
        return;
      }

      try {
        const perm = await Location.getForegroundPermissionsAsync();
        if (perm.granted) {
          const pos =
            (await Location.getLastKnownPositionAsync({ maxAge: 600_000 })) ??
            (await Location.getCurrentPositionAsync({
              accuracy: Location.Accuracy.Balanced,
            }));
          if (pos && !cancelled) {
            setDistance({
              km: haversineKm(
                pos.coords.latitude, pos.coords.longitude, eLat, eLon,
              ),
              from: "you",
            });
            return;
          }
        }
      } catch {
        // fall through to the Malta fallback
      }

      if (!cancelled) {
        setDistance({
          km: haversineKm(MALTA_CENTER.lat, MALTA_CENTER.lon, eLat, eLon),
          from: MALTA_LABEL,
        });
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [lat, lon, distanceKm]);

  const distanceLabel = distance
    ? `${Math.round(distance.km)} km from ${distance.from}`
    : null;

  return (
    <SafeAreaView style={styles.container} edges={["top"]}>
      <ScrollView contentContainerStyle={styles.scrollContent} showsVerticalScrollIndicator={false}>
        {/* Header */}
        <View style={styles.header}>
          <TouchableOpacity
            style={styles.closeBtn}
            onPress={() => router.replace("/")}
            hitSlop={{ top: 12, bottom: 12, left: 12, right: 12 }}
            accessibilityRole="button"
            accessibilityLabel="Close"
          >
            <Ionicons name="close" size={26} color={colors.text} />
          </TouchableOpacity>
          {isPreview && (
            <View style={styles.previewBadge}>
              <Text style={styles.previewBadgeText}>PREVIEW</Text>
            </View>
          )}
        </View>

        {/* Title — deliberately non-alarming */}
        <Text style={styles.title}>Seismic activity</Text>
        {region && <Text style={styles.subtitle}>{region}</Text>}
        <Text style={styles.timeAgo}>{timeAgo}</Text>

        {/* Preview explainer */}
        {isPreview && (
          <View style={styles.previewNotice}>
            <Ionicons name="information-circle" size={20} color={colors.accent} />
            <Text style={styles.previewNoticeText}>
              This is a preview notification — a test of the detection pipeline.
              No action is needed. Genuine safety alerts sound differently.
            </Text>
          </View>
        )}

        {/* Details grid */}
        <View style={styles.card}>
          <Row label="Magnitude" value={magnitude ?? "—"} />
          {distanceLabel && <Row label="Distance" value={distanceLabel} />}
          <Row label="Depth" value={depthKm != null ? `${depthKm} km` : "—"} />
          <Row
            label="Location"
            value={
              lat != null && lon != null
                ? `${Number(lat).toFixed(3)}°, ${Number(lon).toFixed(3)}°`
                : "—"
            }
          />
          {observedAtValid && (
            <Row
              label="Time"
              value={(observedAt as Date).toLocaleString()}
            />
          )}
        </View>

        {/* Plain-language explanation. The distance clause is either fully
            present or fully absent — it must never render as a gap
            ("...was approximately  from your location"), which is what
            happened when distance_km was missing (batch 5, B3). */}
        <Text style={styles.section}>What this means</Text>
        <Text style={styles.explainer}>
          {distance
            ? `The epicentre — where the earthquake started underground — was approximately ${Math.round(distance.km)} km ${distance.from === "you" ? "from your location" : `from ${distance.from}`}. `
            : "The epicentre is where the earthquake started underground. "}
          The magnitude number describes how much energy was released at the
          source. Whether people feel it depends on magnitude, distance, and
          depth together.
        </Text>

        {/* EMSC attribution — licence condition */}
        <View style={styles.attribution}>
          <Text style={styles.attributionText}>
            Data © EMSC (European-Mediterranean Seismological Centre)
            {params.provider ? ` · via ${params.provider}` : ""}
          </Text>
          <TouchableOpacity
            onPress={() => Linking.openURL("https://www.seismicportal.eu/").catch(() => {})}
            accessibilityRole="link"
          >
            <Text style={styles.attributionLink}>seismicportal.eu</Text>
          </TouchableOpacity>
        </View>

        {/* Nothing-to-do reassurance */}
        <View style={styles.footer}>
          <Text style={styles.footerText}>
            This is a record of what has already happened. Earthquake detection is
            not early warning.
          </Text>
        </View>
      </ScrollView>
    </SafeAreaView>
  );
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <View style={styles.row}>
      <Text style={styles.rowLabel}>{label}</Text>
      <Text style={styles.rowValue}>{value}</Text>
    </View>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, backgroundColor: colors.bg },
  scrollContent: { paddingHorizontal: 20, paddingBottom: 40 },
  header: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
    paddingTop: 4,
    paddingBottom: 8,
  },
  closeBtn: { padding: 4 },
  previewBadge: {
    backgroundColor: colors.accent,
    paddingHorizontal: 10,
    paddingVertical: 4,
    borderRadius: 999,
  },
  previewBadgeText: {
    color: "#0B1220",
    fontSize: 11,
    fontWeight: "800",
    letterSpacing: 1.2,
  },
  title: {
    color: colors.text,
    fontSize: 28,
    fontWeight: "700",
    marginTop: 8,
  },
  subtitle: {
    color: colors.textDim,
    fontSize: 16,
    marginTop: 4,
  },
  timeAgo: {
    color: colors.textDim,
    fontSize: 13,
    marginTop: 2,
    marginBottom: 16,
  },
  previewNotice: {
    flexDirection: "row",
    gap: 10,
    backgroundColor: colors.card,
    borderColor: colors.accent,
    borderWidth: 1,
    borderRadius: 12,
    padding: 12,
    marginBottom: 16,
  },
  previewNoticeText: {
    color: colors.text,
    fontSize: 13,
    lineHeight: 19,
    flex: 1,
  },
  card: {
    backgroundColor: colors.card,
    borderRadius: 12,
    padding: 16,
    marginBottom: 20,
  },
  row: {
    flexDirection: "row",
    justifyContent: "space-between",
    paddingVertical: 10,
    borderBottomWidth: 1,
    borderBottomColor: colors.border,
  },
  rowLabel: { color: colors.textDim, fontSize: 14 },
  rowValue: { color: colors.text, fontSize: 14, fontWeight: "600" },
  section: {
    color: colors.text,
    fontSize: 15,
    fontWeight: "700",
    marginTop: 8,
    marginBottom: 6,
  },
  explainer: {
    color: colors.textDim,
    fontSize: 14,
    lineHeight: 21,
    marginBottom: 20,
  },
  attribution: {
    backgroundColor: colors.card,
    borderRadius: 8,
    padding: 12,
    marginBottom: 16,
  },
  attributionText: {
    color: colors.textDim,
    fontSize: 12,
    marginBottom: 4,
  },
  attributionLink: {
    color: colors.accent,
    fontSize: 12,
    textDecorationLine: "underline",
  },
  footer: {
    marginTop: 8,
    paddingHorizontal: 8,
  },
  footerText: {
    color: colors.textDim,
    fontSize: 12,
    fontStyle: "italic",
    textAlign: "center",
    lineHeight: 18,
  },
});
