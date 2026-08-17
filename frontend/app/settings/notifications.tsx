/**
 * Notification-sensitivity settings screen.
 *
 * Requirement 1 from 2026-08-06: the user MUST be able to silence
 * informational (preview / tremor) notifications easily. If they can't,
 * a frustrated user reaches for iOS Settings' notification blanket
 * switch and kills CRITICAL alerts along with everything else — the
 * exact failure mode this feature exists to prevent.
 *
 * Absolute rule (SAFETY-critical, cannot be overridden by ANY setting):
 * Critical / emergency alerts fire regardless of preset. The user
 * controls informational notifications ONLY. This screen must state
 * that plainly, in language a stressed non-technical user cannot
 * misread.
 *
 * The three choices (batch 5, B4 — merged from four on 2026-08-17):
 *   - Off              : no informational notifications at all
 *   - Only what I'd
 *     likely feel      : predicted MMI III+ (recommended default)
 *   - Everything nearby: every event inside country radius, including
 *                        tremors too small to feel
 *
 * "Significant only" (MMI IV+) and "Noticeable" (MMI III+) were one point
 * apart on an intensity scale and indistinguishable to lay users — Paul,
 * who commissioned the feature, could not tell them apart. They are merged
 * into the middle option at the MMI III floor (the previous default, so
 * behaviour is preserved). Devices still stored as "significant" are
 * migrated to "noticeable" on first open of this screen, so the stored
 * value always matches what the UI says.
 *
 * Also detects the OS-level Critical-Alerts permission and shows a
 * persistent (non-dismissable) banner if it has been revoked. Someone
 * who has silently lost critical alerts must be told.
 */
import { useEffect, useState } from "react";
import {
  View, Text, StyleSheet, TouchableOpacity, ScrollView, ActivityIndicator, Linking, Alert, Pressable,
} from "react-native";
import { SafeAreaView } from "react-native-safe-area-context";
import { Ionicons } from "@expo/vector-icons";
import { useRouter } from "expo-router";
import * as Notifications from "expo-notifications";
import { getDeviceId } from "@/src/utils/checkin";

const BACKEND_URL = process.env.EXPO_PUBLIC_BACKEND_URL ?? "";

// Wire values understood by the backend. "significant" is legacy-only: the
// UI no longer offers it, and any device still on it is migrated to
// "noticeable" (same MMI III floor as the merged middle option).
type Preset = "off" | "significant" | "noticeable" | "everything";
// What the user actually chooses from, post-merge.
type Choice = "off" | "noticeable" | "everything";

const toChoice = (p: Preset): Choice =>
  p === "significant" ? "noticeable" : (p as Choice);

const PRESET_OPTIONS: {
  value: Choice;
  title: string;
  subtitle: string;
  helper: string;
}[] = [
  {
    value: "off",
    title: "Off",
    subtitle: "No tremor notifications",
    helper: "You'll still receive critical earthquake alerts — those cannot be switched off.",
  },
  {
    value: "noticeable",
    title: "Only what I'd likely feel",
    subtitle: "Tremors you'd probably notice",
    helper: "Recommended. Notifications for tremors likely to be felt at all (predicted intensity III or above).",
  },
  {
    value: "everything",
    title: "Everything nearby",
    subtitle: "Including tremors too small to feel",
    helper: "Every recorded tremor in the region, including ones you will not feel at all. Expect a few notifications a day.",
  },
];

export default function NotificationSettingsScreen() {
  const router = useRouter();
  const [preset, setPreset] = useState<Choice>("noticeable");
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState<Choice | null>(null);
  const [criticalAlertsRevoked, setCriticalAlertsRevoked] = useState(false);

  // Load current preset + check OS-level Critical Alerts permission.
  useEffect(() => {
    (async () => {
      try {
        const did = await getDeviceId();
        const r = await fetch(`${BACKEND_URL}/api/devices/${encodeURIComponent(did)}/notification-preset`);
        if (r.ok) {
          const data = await r.json();
          if (data.preset && ["off","significant","noticeable","everything"].includes(data.preset)) {
            const stored = data.preset as Preset;
            setPreset(toChoice(stored));
            // One-way migration of the retired "significant" tier. Never
            // lands anyone on Off — it moves them to the merged middle
            // option, which is what the screen now shows them.
            if (stored === "significant") {
              save("noticeable", { silent: true });
            }
          }
        }
      } catch { /* offline is fine — keep default */ }
      // Check OS-level notification permission and specifically whether
      // Critical Alerts have been revoked. iOS-only concept.
      try {
        const perm = await Notifications.getPermissionsAsync();
        // On iOS `allowsCriticalAlerts` reflects whether the user has
        // toggled it OFF in iOS Settings even though we still have
        // basic notification permission.
        const iosPerm = (perm as any).ios;
        if (iosPerm && iosPerm.allowsCriticalAlerts === false) {
          setCriticalAlertsRevoked(true);
        }
      } catch { /* non-iOS or permissions API changed — quietly ignore */ }
      setLoading(false);
    })();
  }, []);

  const save = async (next: Choice, opts?: { silent?: boolean }) => {
    if (!opts?.silent) setSaving(next);
    try {
      const did = await getDeviceId();
      const r = await fetch(`${BACKEND_URL}/api/devices/notification-preset`, {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({device_id: did, preset: next}),
      });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      setPreset(next);
    } catch {
      if (!opts?.silent) {
        Alert.alert("Could not save", "Please check your connection and try again.");
      }
    } finally {
      if (!opts?.silent) setSaving(null);
    }
  };

  return (
    <SafeAreaView style={styles.container} edges={["top"]}>
      <View style={styles.header}>
        <TouchableOpacity
          style={styles.backBtn}
          onPress={() => router.back()}
          hitSlop={{top:12,bottom:12,left:12,right:12}}
          accessibilityRole="button" accessibilityLabel="Back"
        >
          <Ionicons name="chevron-back" size={26} color="#E7EDF5" />
        </TouchableOpacity>
        <Text style={styles.title}>Notifications</Text>
        <View style={{width: 26}} />
      </View>

      <ScrollView contentContainerStyle={styles.scroll} showsVerticalScrollIndicator={false}>

        {/* Critical-alerts sticky banner — persistent, non-dismissable */}
        {criticalAlertsRevoked && (
          <TouchableOpacity
            style={styles.criticalBanner}
            onPress={() => Linking.openSettings()}
            accessibilityRole="button"
            accessibilityLabel="Open iOS settings to re-enable critical alerts"
          >
            <Ionicons name="warning" size={22} color="#8A0F0F" />
            <View style={{flex: 1, marginLeft: 10}}>
              <Text style={styles.criticalBannerTitle}>
                Critical Alerts turned OFF
              </Text>
              <Text style={styles.criticalBannerBody}>
                You&apos;ll miss earthquake alerts even in silent mode. Tap to re-enable in iOS Settings.
              </Text>
            </View>
            <Ionicons name="chevron-forward" size={20} color="#8A0F0F" />
          </TouchableOpacity>
        )}

        {/* The always-on rule — first thing users see, plain language */}
        <View style={styles.rulePanel}>
          <Ionicons name="shield-checkmark" size={22} color="#1F8A3A" />
          <Text style={styles.ruleText}>
            These settings control informational notifications about nearby tremors.{"\n"}
            <Text style={styles.ruleTextBold}>
              Alerts for dangerous earthquakes are always on and cannot be switched off.
            </Text>
          </Text>
        </View>

        {loading ? (
          <ActivityIndicator style={{marginTop: 40}} color="#5DB1FF" />
        ) : (
          <View style={styles.options}>
            {PRESET_OPTIONS.map(opt => {
              const active = preset === opt.value;
              const isSaving = saving === opt.value;
              return (
                <TouchableOpacity
                  key={opt.value}
                  style={[styles.option, active && styles.optionActive]}
                  onPress={() => !isSaving && save(opt.value)}
                  disabled={isSaving}
                  accessibilityRole="radio"
                  accessibilityState={{selected: active}}
                >
                  <View style={styles.optionHeader}>
                    <View style={styles.optionText}>
                      <Text style={[styles.optionTitle, active && styles.optionTitleActive]}>
                        {opt.title}
                      </Text>
                      <Text style={styles.optionSubtitle}>{opt.subtitle}</Text>
                    </View>
                    {isSaving ? (
                      <ActivityIndicator color="#5DB1FF" />
                    ) : active ? (
                      <Ionicons name="checkmark-circle" size={26} color="#1F8A3A" />
                    ) : (
                      <View style={styles.radioEmpty} />
                    )}
                  </View>
                  <Text style={styles.optionHelper}>{opt.helper}</Text>
                </TouchableOpacity>
              );
            })}
          </View>
        )}

        <Pressable
          onPress={() => router.push("/settings/places" as any)}
          style={({ pressed }) => [styles.placesLink, pressed && { opacity: 0.85 }]}
          testID="places-link"
          accessibilityRole="button"
        >
          <Ionicons name="location-outline" size={22} color="#5DB1FF" />
          <View style={{ flex: 1 }}>
            <Text style={styles.placesLinkTitle}>Places I care about</Text>
            <Text style={styles.placesLinkBody}>
              Optional. Also get tremor notices for somewhere else — family in
              Sicily, a second home.
            </Text>
          </View>
          <Ionicons name="chevron-forward" size={20} color="#8FA0BC" />
        </Pressable>

        <Text style={styles.footer}>
          Turning informational notifications off does not affect emergency alerts.
          You will still be alerted by the siren for a dangerous earthquake.
        </Text>
      </ScrollView>
    </SafeAreaView>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, backgroundColor: "#0B1220" },
  header: {
    flexDirection: "row", alignItems: "center", justifyContent: "space-between",
    paddingHorizontal: 16, paddingVertical: 12,
    borderBottomWidth: 1, borderBottomColor: "#25324A",
  },
  backBtn: { padding: 4 },
  title: { color: "#E7EDF5", fontSize: 18, fontWeight: "700" },
  scroll: { padding: 20, paddingBottom: 40 },

  criticalBanner: {
    flexDirection: "row", alignItems: "center",
    backgroundColor: "#FFE5E5", borderColor: "#C11414", borderWidth: 2,
    borderRadius: 12, padding: 14, marginBottom: 16,
  },
  criticalBannerTitle: { color: "#8A0F0F", fontSize: 15, fontWeight: "800" },
  criticalBannerBody: { color: "#8A0F0F", fontSize: 13, marginTop: 2, lineHeight: 18 },

  rulePanel: {
    flexDirection: "row", gap: 10, alignItems: "flex-start",
    backgroundColor: "#0F2818", borderColor: "#1F8A3A", borderWidth: 1,
    borderRadius: 12, padding: 14, marginBottom: 20,
  },
  ruleText: { flex: 1, color: "#B3E5C4", fontSize: 13, lineHeight: 20 },
  ruleTextBold: { fontWeight: "700", color: "#E7EDF5" },

  options: { gap: 10 },
  option: {
    backgroundColor: "#151E2F", borderRadius: 12, padding: 16,
    borderWidth: 1, borderColor: "#25324A",
  },
  optionActive: { borderColor: "#1F8A3A", borderWidth: 2 },
  optionHeader: { flexDirection: "row", alignItems: "center", gap: 12 },
  optionText: { flex: 1 },
  optionTitle: { color: "#E7EDF5", fontSize: 16, fontWeight: "700" },
  optionTitleActive: { color: "#5DB1FF" },
  optionSubtitle: { color: "#8FA0BC", fontSize: 13, marginTop: 2 },
  optionHelper: { color: "#8FA0BC", fontSize: 12, marginTop: 10, lineHeight: 17 },
  radioEmpty: {
    width: 24, height: 24, borderRadius: 12,
    borderWidth: 2, borderColor: "#8FA0BC",
  },

  footer: {
    color: "#8FA0BC", fontSize: 12, fontStyle: "italic",
    textAlign: "center", marginTop: 24, lineHeight: 18,
  },
  placesLink: {
    flexDirection: "row", alignItems: "center", gap: 12,
    backgroundColor: "#151E2F", borderRadius: 12, padding: 16,
    borderWidth: 1, borderColor: "#25324A", marginTop: 20, minHeight: 48,
  },
  placesLinkTitle: { color: "#E7EDF5", fontSize: 15, fontWeight: "700" },
  placesLinkBody: { color: "#8FA0BC", fontSize: 12, marginTop: 2, lineHeight: 17 },
});
