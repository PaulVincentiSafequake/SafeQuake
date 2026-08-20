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
import { parseUtc } from "@/src/utils/time";
import { resolveEventReadings } from "@/src/utils/eventReadings";

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
    /** Where this screen was opened from — labels the back control (#173). */
    from?: string;
  }>();

  const isPreview = params.preview === "true" || params.preview === "1";
  // parseUtc, not new Date(): an offset-less backend timestamp would be read
  // as local time and render two hours early on a Malta phone.
  const observedAt = parseUtc(params.observed_at);
  const observedAtValid = observedAt !== null;

  const timeAgo = (() => {
    if (!observedAtValid) return "—";
    const s = Math.floor((Date.now() - (observedAt as Date).getTime()) / 1000);
    if (s < 60) return `${s}s ago`;
    if (s < 3600) return `${Math.floor(s / 60)} min ago`;
    if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
    return `${Math.floor(s / 86400)} days ago`;
  })();

  // §1 #174 (Neo 2026-08-20): read from the SAME resolver /alert uses.
  // Before this, /alert had the single-source fix from #205 but this
  // screen didn't — so a tapped preview notification showed dashes even
  // though the payload had the values. Pattern #1 recurrence, now closed.
  const readings = resolveEventReadings(params);
  const magnitude = readings.magnitude;
  const depthKm = readings.depth_km;
  const region = readings.region;
  const lat = readings.latitude;
  const lon = readings.longitude;

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
        const fromPayload = readings.distance_km;
        if (fromPayload != null && !cancelled) {
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
  }, [lat, lon, readings.distance_km]);

  const distanceLabel = distance
    ? `${Math.round(distance.km)} km from ${distance.from}`
    : null;

  // Sets the expectation BEFORE the tap — "you'll return to the map", not
  // "this closes" (Paul, 2026-08-18).
  const backLabel =
    params.from === "map" ? "Map" : router.canGoBack() ? "Back" : "Close";

  // §2 #256 (Neo 2026-08-20): map button visibility depends ONLY on
  // whether we have usable coordinates for this event. It MUST NOT be
  // hidden because magnitude/depth/distance failed to arrive — those
  // are separate concerns and the operator/user reaches for the map
  // exactly when the numbers didn't help. Rule 9.5 (worse-but-working).
  const hasCoords = readings.hasCoords;

  // B5 — see this event on the map. Pushes the map ON TOP of this screen, so
  // backing out returns here rather than looping: notification → detail →
  // map → (back) → detail → (back) → wherever the detail came from.
  const openOnMap = () => {
    router.push({
      pathname: "/map" as any,
      params: {
        focus_lat: String(lat),
        focus_lon: String(lon),
        focus_unid: String(params.unid ?? ""),
      },
    });
  };

  return (
    <SafeAreaView style={styles.container} edges={["top"]}>
      <ScrollView contentContainerStyle={styles.scrollContent} showsVerticalScrollIndicator={false}>
        {/* Header */}
        <View style={styles.header}>
          {/* #173 — RETURN TO WHERE YOU CAME FROM.
              This was `router.replace("/")`, which tore down the stack and
              dumped the user on Home. Browsing several events (the whole
              point of the seismic feed, #107) meant re-entering the map,
              re-picking the time window and re-panning after every single
              pin. router.back() pops this screen off instead, so the map
              underneath keeps its pan, zoom and time window untouched.
              `replace("/")` survives only as the cold-start fallback, when
              a notification tap means there is genuinely no stack to pop. */}
          <TouchableOpacity
            style={styles.closeBtn}
            onPress={() => {
              if (router.canGoBack()) router.back();
              else router.replace("/");
            }}
            hitSlop={{ top: 12, bottom: 12, left: 12, right: 12 }}
            accessibilityRole="button"
            accessibilityLabel={backLabel}
          >
            <Ionicons
              name={router.canGoBack() ? "chevron-back" : "close"}
              size={26}
              color={colors.text}
            />
            {router.canGoBack() && (
              <Text style={styles.backLabel}>{backLabel}</Text>
            )}
          </TouchableOpacity>
          {isPreview && (
            <View style={styles.previewBadge}>
              <Text style={styles.previewBadgeText}>PREVIEW</Text>
            </View>
          )}
        </View>

        {/* Title — deliberately non-alarming */}
        <Text style={styles.title}>Seismic activity</Text>
        {region ? (
          <Text style={styles.subtitle}>{region}</Text>
        ) : (
          // §1 #174 (Neo 2026-08-20): never render an empty subtitle
          // that looks like data. If the notification didn't carry a
          // place name, say so plainly rather than showing a bare "—".
          <Text style={styles.subtitleMissing}>Location not given by the report</Text>
        )}
        <Text style={styles.timeAgo}>{timeAgo}</Text>

        {/* §1 #174: if the notification arrived without the readings, say
            so with a full sentence rather than a screenful of dashes. */}
        {readings.hasMissingFields && (
          <View style={styles.missingNotice}>
            <Ionicons name="information-circle-outline" size={16} color={colors.textDim} />
            <Text style={styles.missingNoticeText}>
              Some details didn't arrive with this notification. Anything
              below marked &ldquo;Unknown&rdquo; is missing, not zero.
            </Text>
          </View>
        )}

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

        {/* Details grid — every missing value reads &ldquo;Unknown&rdquo;
            in plain words, never a bare dash (rule 9.4). */}
        <View style={styles.card}>
          <Row label="Magnitude" value={magnitude ?? "Unknown"} />
          <Row label="Distance" value={distanceLabel ?? "Unknown"} />
          <Row label="Depth" value={depthKm != null ? `${depthKm} km` : "Unknown"} />
          <Row
            label="Location"
            value={
              hasCoords
                ? `${(lat as number).toFixed(3)}°, ${(lon as number).toFixed(3)}°`
                : "Unknown"
            }
          />
          {observedAtValid && (
            <Row
              label="Time"
              value={(observedAt as Date).toLocaleString()}
            />
          )}
        </View>

        {/* §2 #256 (Neo 2026-08-20): map button visibility now depends
            ONLY on hasCoords. Missing magnitude/depth/distance must
            never take away the map button too. If we don't have
            coordinates, we say so instead of quietly dropping the
            control. */}
        {hasCoords ? (
          <TouchableOpacity
            style={styles.mapBtn}
            onPress={openOnMap}
            accessibilityRole="button"
            accessibilityLabel="See this location on the map"
            testID="see-on-map-btn"
          >
            <Ionicons name="map-outline" size={20} color={colors.text} />
            <Text style={styles.mapBtnText}>Zoom in to where it happened (epicentre)</Text>
            <Ionicons name="chevron-forward" size={18} color={colors.textDim} />
          </TouchableOpacity>
        ) : (
          <View style={styles.noCoordsNotice}>
            <Ionicons name="map-outline" size={18} color={colors.textDim} />
            <Text style={styles.noCoordsText}>
              The notification didn't include an epicentre location, so
              there's nothing to show on the map.
            </Text>
          </View>
        )}

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
  backLabel: { color: colors.text, fontSize: 15, fontWeight: "600", marginLeft: 2 },
  mapBtn: {
    flexDirection: "row",
    alignItems: "center",
    gap: 10,
    minHeight: 52,
    paddingHorizontal: 14,
    marginTop: 18,
    borderRadius: 12,
    borderWidth: 1,
    borderColor: "rgba(255,255,255,0.16)",
    backgroundColor: "rgba(255,255,255,0.06)",
  },
  mapBtnText: { flex: 1, color: colors.text, fontSize: 15, fontWeight: "700" },
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
  subtitleMissing: {
    color: colors.textDim,
    fontSize: 15,
    marginTop: 4,
    fontStyle: "italic",
  },
  missingNotice: {
    flexDirection: "row",
    alignItems: "flex-start",
    gap: 8,
    padding: 10,
    marginTop: 12,
    borderRadius: 8,
    backgroundColor: "rgba(143,160,188,0.12)",
    borderWidth: 1,
    borderColor: "rgba(143,160,188,0.25)",
  },
  missingNoticeText: {
    color: colors.text,
    fontSize: 14,
    lineHeight: 18,
    flex: 1,
  },
  noCoordsNotice: {
    flexDirection: "row",
    alignItems: "center",
    gap: 10,
    marginTop: 12,
    padding: 12,
    borderRadius: 8,
    borderWidth: 1,
    borderColor: colors.border,
    backgroundColor: colors.card,
  },
  noCoordsText: {
    color: colors.textDim,
    fontSize: 14,
    lineHeight: 20,
    flex: 1,
  },
  timeAgo: {
    color: colors.textDim,
    fontSize: 14,
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
    fontSize: 14,
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
    fontSize: 14,
    fontStyle: "italic",
    textAlign: "center",
    lineHeight: 18,
  },
});
