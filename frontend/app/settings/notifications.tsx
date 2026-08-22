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
 *   - Everything nearby — including tremors too small to feel:
 *                        every event inside country radius. The "including
 *                        tremors too small to feel" clause is in the TITLE,
 *                        not the subtitle (Paul, batch 6 B3): the option must
 *                        state what it costs you in the line you choose it by,
 *                        because that is the only line a scanning reader
 *                        actually reads.
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
import { getDeviceId } from "@/src/utils/checkin";
import { useReadiness } from "@/src/utils/readiness";
import { markPresetChosenByUser } from "@/src/utils/tremorNotices";

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
    helper: "This only turns off the quiet tremor notices.",
  },
  {
    value: "noticeable",
    // #284: every line here describes a tremor that has ALREADY happened.
    // Nothing in this app may read as a warning ahead of an earthquake.
    title: "Only shakes I would have felt",
    subtitle: "Tremors most people would have noticed",
    helper: "Recommended. You hear about a tremor after it happens, if it was strong enough that most people indoors would have felt it.",
  },
  {
    value: "everything",
    title: "Everything nearby — including tremors too small to feel",
    subtitle: "Every recorded tremor in the region",
    helper: "Includes tremors nobody felt. Each one is a shake that has already happened. Expect a few messages a day.",
  },
];

export default function NotificationSettingsScreen() {
  const router = useRouter();
  const [preset, setPreset] = useState<Choice>("noticeable");
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState<Choice | null>(null);
  // #280: one source for every claim about the siren on this screen.
  const readiness = useReadiness();
  const sirenOff = !readiness.loading && !readiness.sirenWillSound;

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
      // #280: the permission read used to live here as well. It does not
      // any more — useReadiness() owns it, so this screen cannot drift from
      // the home screen or from the panel three lines below.
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
      // #305: a person who has chosen for themselves is never asked
      // whether they want fewer notices. They have decided. `silent` is
      // the automatic migration of the retired tier, which is not a choice.
      if (!opts?.silent) markPresetChosenByUser().catch(() => {});
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

        {/* #280: this banner and the panel below it read the SAME source
            (useReadiness). Before, the banner read the live iOS permission
            and the panel printed a hard-coded promise, so the screen could
            say "the siren is off" and "the siren cannot be switched off"
            one under the other. */}
        {sirenOff && (
          <TouchableOpacity
            style={styles.criticalBanner}
            onPress={() => Linking.openSettings()}
            accessibilityRole="button"
            accessibilityLabel="Open iOS settings to re-enable critical alerts"
          >
            <Ionicons name="warning" size={22} color="#8A0F0F" />
            <View style={{flex: 1, marginLeft: 10}}>
              <Text style={styles.criticalBannerTitle}>
                Your phone will not sound the siren
              </Text>
              <Text style={styles.criticalBannerBody}>
                An earthquake alert will arrive quietly, or not at all. Tap
                here, then tap Notifications and turn on Allow Notifications
                and Critical Alerts.
              </Text>
            </View>
            <Ionicons name="chevron-forward" size={20} color="#8A0F0F" />
          </TouchableOpacity>
        )}

        {/* #280: one sentence about the siren, written in
            src/utils/readiness.ts, printed here and nowhere else invented. */}
        <View style={[styles.rulePanel, sirenOff && styles.rulePanelOff]}>
          <Ionicons
            name={sirenOff ? "alert-circle" : "shield-checkmark"}
            size={22}
            color={sirenOff ? "#F4C842" : "#1F8A3A"}
          />
          <Text style={[styles.ruleText, sirenOff && styles.ruleTextOff]}>
            These settings control the quiet notices about nearby tremors.{"\n"}
            <Text style={styles.ruleTextBold}>{readiness.sirenSentence}</Text>
          </Text>
        </View>

        {/* #279 (2026-08-21 — Paul, after his own check-in request landed
            silently inside a Focus mode): "Better they decide knowingly
            than discover it in an earthquake." An earthquake alert breaks
            through Focus because it carries Apple's critical entitlement.
            A check-in question deliberately does not — so say so, here,
            with the way to change it one tap away. */}
        <View style={styles.focusPanel}>
          <Ionicons name="moon-outline" size={22} color="#C9A227" />
          <View style={{ flex: 1, marginLeft: 10 }}>
            <Text style={styles.focusTitle}>
              Focus and Do Not Disturb can silence check-ins
            </Text>
            <Text style={styles.focusBody}>
              After an earthquake we may ask you to check in. That question is
              not an alarm, so a Focus mode can hide it without a sound.
              {"\n\n"}
              {sirenOff
                ? "An earthquake alert should come through anything — but not on this phone until you fix the warning above."
                : "An earthquake alert is different. It is set to come through Focus and silent — it still needs signal and the permissions above."}
              {"\n\n"}
              To let check-in questions through, turn on Time Sensitive
              Notifications for Quake Angel.
            </Text>
            <TouchableOpacity
              style={styles.focusBtn}
              onPress={() => Linking.openSettings()}
              accessibilityRole="button"
              accessibilityLabel="Open iPhone settings for Quake Angel notifications"
            >
              <Text style={styles.focusBtnText}>Open my phone settings</Text>
              <Ionicons name="chevron-forward" size={18} color="#0F1115" />
            </TouchableOpacity>
          </View>
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
  criticalBannerBody: { color: "#8A0F0F", fontSize: 14, marginTop: 2, lineHeight: 18 },

  rulePanel: {
    flexDirection: "row", gap: 10, alignItems: "flex-start",
    backgroundColor: "#0F2818", borderColor: "#1F8A3A", borderWidth: 1,
    borderRadius: 12, padding: 14, marginBottom: 20,
  },
  rulePanelOff: { backgroundColor: "#2A2216", borderColor: "#8A6B0F" },
  ruleText: { flex: 1, color: "#B3E5C4", fontSize: 14, lineHeight: 20 },
  ruleTextOff: { color: "#F7E7B8" },
  ruleTextBold: { fontWeight: "700", color: "#E7EDF5" },

  // #279 Focus disclosure.
  focusPanel: {
    flexDirection: "row", alignItems: "flex-start",
    backgroundColor: "#241E08", borderColor: "#7A6414", borderWidth: 1,
    borderRadius: 12, padding: 14, marginBottom: 20,
  },
  focusTitle: { color: "#F2D96B", fontSize: 15, fontWeight: "700", marginBottom: 6 },
  focusBody: { color: "#D8D2B8", fontSize: 14, lineHeight: 21 },
  focusBtn: {
    flexDirection: "row", alignItems: "center", justifyContent: "center",
    gap: 6, marginTop: 14, minHeight: 48, borderRadius: 10,
    backgroundColor: "#F2D96B", paddingHorizontal: 16,
  },
  focusBtnText: { color: "#0F1115", fontSize: 15, fontWeight: "700" },


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
  optionSubtitle: { color: "#8FA0BC", fontSize: 14, marginTop: 2 },
  optionHelper: { color: "#8FA0BC", fontSize: 14, marginTop: 10, lineHeight: 17 },
  radioEmpty: {
    width: 24, height: 24, borderRadius: 12,
    borderWidth: 2, borderColor: "#8FA0BC",
  },

  footer: {
    color: "#8FA0BC", fontSize: 14, fontStyle: "italic",
    textAlign: "center", marginTop: 24, lineHeight: 18,
  },
  placesLink: {
    flexDirection: "row", alignItems: "center", gap: 12,
    backgroundColor: "#151E2F", borderRadius: 12, padding: 16,
    borderWidth: 1, borderColor: "#25324A", marginTop: 20, minHeight: 48,
  },
  placesLinkTitle: { color: "#E7EDF5", fontSize: 15, fontWeight: "700" },
  placesLinkBody: { color: "#8FA0BC", fontSize: 14, marginTop: 2, lineHeight: 17 },
});
