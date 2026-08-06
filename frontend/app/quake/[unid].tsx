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
import { View, Text, ScrollView, StyleSheet, TouchableOpacity, Linking } from "react-native";
import { SafeAreaView } from "react-native-safe-area-context";
import { Ionicons } from "@expo/vector-icons";

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
          <Row label="Distance" value={distanceKm != null ? `${distanceKm} km` : "—"} />
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

        {/* Plain-language explanation */}
        <Text style={styles.section}>What this means</Text>
        <Text style={styles.explainer}>
          The epicentre — where the earthquake started underground — was approximately
          {distanceKm ? ` ${distanceKm} km ` : " "}
          from your location. The magnitude number describes how much energy
          was released at the source. Whether people feel it depends on
          magnitude, distance, and depth together.
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
