/**
 * ReadinessBanner — the first thing on the home screen when this phone
 * cannot do its job.
 *
 * #281 (2026-08-22 — Paul):
 *   "'Critical alerts turned off' sits inside the Notifications settings
 *    screen. Someone who declined the permission at setup will never go
 *    there. They will believe they are protected and they are not. It
 *    belongs on the home screen, permanently, until fixed. The most
 *    prominent thing on that screen, above the rescue code, not
 *    dismissible."
 *
 * Rules this component keeps:
 *   1. NOT dismissible. There is no close button and no snooze. It goes
 *      away when the phone can do the job, and only then.
 *   2. Above everything, including the rescue code.
 *   3. It never prints a reassurance. No problems found means it renders
 *      nothing at all — "we found no problem" is not "you are protected".
 *   4. Every line says what will happen and what to do next.
 *   5. Words come from src/utils/readiness.ts, the one place that decides.
 */
import { Ionicons } from "@expo/vector-icons";
import { Linking, StyleSheet, Text, TouchableOpacity, View } from "react-native";
import * as Location from "expo-location";
import * as Notifications from "expo-notifications";

import { type Problem, useReadiness } from "@/src/utils/readiness";

export default function ReadinessBanner() {
  const { problems, canAskAgain, refresh } = useReadiness();
  if (problems.length === 0) return null;

  const onPress = async (p: Problem) => {
    try {
      if (p.id === "notifications_off" || p.id === "siren_off") {
        // Ask once more if iOS will still let us. Otherwise the only route
        // is Settings, and the panel below says exactly what to tap there.
        if (p.id === "notifications_off" && canAskAgain) {
          await Notifications.requestPermissionsAsync({
            ios: { allowAlert: true, allowSound: true, allowCriticalAlerts: true },
          });
        } else {
          await Linking.openSettings();
        }
      } else if (p.id === "location_off") {
        const res = await Location.requestForegroundPermissionsAsync();
        if (!res.granted && !res.canAskAgain) await Linking.openSettings();
      }
    } catch { /* nothing to say to the user about a failed intent */ }
    refresh();
  };

  return (
    <View style={styles.wrap} testID="readiness-banner">
      {problems.map((p) => (
        <TouchableOpacity
          key={p.id}
          activeOpacity={0.85}
          onPress={() => onPress(p)}
          style={[styles.row, p.critical ? styles.rowCritical : styles.rowWarn]}
          accessibilityRole="button"
          accessibilityLabel={`${p.headline} ${p.action}`}
          testID={`readiness-${p.id}`}
        >
          <Ionicons
            name={p.critical ? "volume-mute" : "alert-circle"}
            size={24}
            color={p.critical ? "#FFD9D9" : "#F4C842"}
          />
          <View style={styles.textWrap}>
            <Text style={[styles.headline, !p.critical && styles.headlineWarn]}>
              {p.headline}
            </Text>
            <Text style={styles.action}>{p.action}</Text>
            {(p.id === "siren_off" ||
              (p.id === "notifications_off" && !canAskAgain)) && (
              <Text style={styles.steps}>
                Your phone opens on our settings page. Tap Notifications, then
                turn on Allow Notifications and Critical Alerts. iPhone does
                not let an app open that page directly.
              </Text>
            )}
          </View>
          <Ionicons name="chevron-forward" size={20} color="#FFFFFF" />
        </TouchableOpacity>
      ))}
    </View>
  );
}

const styles = StyleSheet.create({
  wrap: { paddingHorizontal: 16, paddingTop: 12, gap: 10 },
  row: {
    flexDirection: "row",
    alignItems: "flex-start",
    gap: 12,
    borderRadius: 14,
    borderWidth: 1,
    padding: 16,
    minHeight: 64,
  },
  rowCritical: { backgroundColor: "#4A0E0E", borderColor: "#E64545" },
  rowWarn: { backgroundColor: "#2A2216", borderColor: "#8A6B0F" },
  textWrap: { flex: 1 },
  headline: { color: "#FFFFFF", fontSize: 17, fontWeight: "700", lineHeight: 23 },
  headlineWarn: { color: "#F7E7B8" },
  action: { color: "#F2D2D2", fontSize: 15, lineHeight: 21, marginTop: 4 },
  steps: { color: "#D9C9C9", fontSize: 14, lineHeight: 20, marginTop: 8 },
});
