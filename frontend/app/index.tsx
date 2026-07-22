import { useRouter } from "expo-router";
import { StatusBar } from "expo-status-bar";
import { Image } from "expo-image";
import { LinearGradient } from "expo-linear-gradient";
import { Ionicons } from "@expo/vector-icons";
import * as Haptics from "expo-haptics";
import { useState } from "react";
import {
  ActivityIndicator,
  Pressable,
  ScrollView,
  StyleSheet,
  Text,
  View,
} from "react-native";
import { SafeAreaView, useSafeAreaInsets } from "react-native-safe-area-context";

import { colors, radius, spacing } from "@/src/theme";
import { postStatus } from "@/src/utils/checkin";
import { broadcastAlert } from "@/src/utils/push";
import {
  cancelCheckInReminders,
  ensureNotificationSetup,
  scheduleCheckInReminders,
} from "@/src/utils/reminders";

const HERO_IMG =
  "https://images.unsplash.com/photo-1772050137595-0116f8dba498?crop=entropy&cs=srgb&fm=jpg&ixid=M3w4NjY2NjV8MHwxfHNlYXJjaHwxfHxlYXJ0aHF1YWtlJTIwc2Vpc21vZ3JhcGglMjBkYXJrfGVufDB8fHx8MTc4NDcwNTQ2MHww&ixlib=rb-4.1.0&q=85";

type Tip = {
  icon: keyof typeof Ionicons.glyphMap;
  title: string;
  body: string;
};

const TIPS: Tip[] = [
  {
    icon: "arrow-down-circle",
    title: "DROP",
    body: "Drop to your hands and knees before the shaking knocks you down.",
  },
  {
    icon: "shield",
    title: "COVER",
    body: "Take cover under a sturdy desk. Protect your head and neck.",
  },
  {
    icon: "hand-left",
    title: "HOLD ON",
    body: "Hold on until the shaking stops. Be ready to move with your shelter.",
  },
  {
    icon: "medkit",
    title: "AFTER",
    body: "Check for injuries. Expect aftershocks. Stay away from damaged areas.",
  },
];

export default function HomeScreen() {
  const router = useRouter();
  const insets = useSafeAreaInsets();
  const [triggering, setTriggering] = useState(false);

  const handleTrigger = async () => {
    if (triggering) return;
    setTriggering(true);
    await Haptics.impactAsync(Haptics.ImpactFeedbackStyle.Heavy).catch(() => {});

    // 1) Immediately mark this device 'not_responding' on the dashboard so it
    //    turns red the instant the alert starts (before the user has a chance
    //    to tap 'I'm Safe').
    postStatus({ status: "not_responding" }).catch(() => {});

    // 2) Fan out a push notification to every OTHER registered device so
    //    they get the same alert simultaneously (server broadcast).
    broadcastAlert().catch(() => {});

    // 3) Ask for notification permission and schedule local reminder
    //    notifications every ~90s until the user marks themselves safe.
    (async () => {
      const ok = await ensureNotificationSetup();
      if (ok) {
        await cancelCheckInReminders();
        await scheduleCheckInReminders();
      }
    })();

    router.push("/alert");
    setTriggering(false);
  };

  return (
    <View style={styles.root}>
      <StatusBar style="light" />

      <ScrollView
        contentContainerStyle={{
          paddingBottom: 120 + insets.bottom,
        }}
        showsVerticalScrollIndicator={false}
      >
        {/* Hero */}
        <View style={styles.hero}>
          <Image
            source={{ uri: HERO_IMG }}
            style={StyleSheet.absoluteFill}
            contentFit="cover"
            transition={300}
          />
          <LinearGradient
            colors={["rgba(15,17,21,0.15)", "rgba(15,17,21,0.85)", colors.surface]}
            style={StyleSheet.absoluteFill}
          />
          <SafeAreaView edges={["top"]} style={styles.heroContent}>
            <View style={styles.statusRow} testID="system-status-banner">
              <View style={styles.statusDot} />
              <Text style={styles.statusText}>SYSTEM ACTIVE · MONITORING</Text>
            </View>
            <Text style={styles.brand}>QUAKEGUARD</Text>
            <Text style={styles.tagline}>
              Earthquake preparedness{"\n"}at your fingertips.
            </Text>
          </SafeAreaView>
        </View>

        {/* Tips */}
        <View style={styles.section}>
          <Text style={styles.sectionTitle}>SAFETY PROTOCOL</Text>
          <Text style={styles.sectionSub}>
            Memorize these four steps. Every second counts.
          </Text>

          <View style={styles.tipsList}>
            {TIPS.map((tip, i) => (
              <View
                key={tip.title}
                style={styles.tipCard}
                testID={`tip-card-${tip.title.toLowerCase()}`}
              >
                <View style={styles.tipIndex}>
                  <Text style={styles.tipIndexText}>
                    {String(i + 1).padStart(2, "0")}
                  </Text>
                </View>
                <View style={styles.tipIcon}>
                  <Ionicons name={tip.icon} size={22} color={colors.brandPrimary} />
                </View>
                <View style={{ flex: 1 }}>
                  <Text style={styles.tipTitle}>{tip.title}</Text>
                  <Text style={styles.tipBody}>{tip.body}</Text>
                </View>
              </View>
            ))}
          </View>
        </View>

        {/* Info card */}
        <View style={styles.section}>
          <View style={styles.infoCard}>
            <Ionicons name="information-circle" size={20} color={colors.brandSecondary} />
            <Text style={styles.infoText}>
              This is a test tool. Tapping below simulates an earthquake alert so
              you can practice reporting yourself safe.
            </Text>
          </View>
        </View>
      </ScrollView>

      {/* Sticky trigger */}
      <View
        style={[
          styles.stickyBar,
          { paddingBottom: Math.max(insets.bottom, spacing.lg) },
        ]}
      >
        <LinearGradient
          colors={["rgba(15,17,21,0)", colors.surface]}
          style={[styles.stickyScrim, { pointerEvents: "none" }]}
        />
        <Pressable
          onPress={handleTrigger}
          disabled={triggering}
          style={({ pressed }) => [
            styles.triggerBtn,
            pressed && { opacity: 0.85, transform: [{ scale: 0.98 }] },
          ]}
          testID="trigger-alert-btn"
        >
          {triggering ? (
            <ActivityIndicator color={colors.onBrandPrimary} />
          ) : (
            <Ionicons name="warning" size={22} color={colors.onBrandPrimary} />
          )}
          <Text style={styles.triggerText}>
            {triggering ? "TRIGGERING…" : "TRIGGER TEST ALERT"}
          </Text>
        </Pressable>
      </View>
    </View>
  );
}

const styles = StyleSheet.create({
  root: {
    flex: 1,
    backgroundColor: colors.surface,
  },
  hero: {
    height: 340,
    overflow: "hidden",
  },
  heroContent: {
    flex: 1,
    paddingHorizontal: spacing.xl,
    justifyContent: "flex-end",
    paddingBottom: spacing.xl,
  },
  statusRow: {
    flexDirection: "row",
    alignItems: "center",
    gap: spacing.sm,
    marginBottom: spacing.lg,
  },
  statusDot: {
    width: 8,
    height: 8,
    borderRadius: 4,
    backgroundColor: colors.success,
  },
  statusText: {
    color: colors.onSurfaceSecondary,
    fontSize: 11,
    letterSpacing: 2,
    fontWeight: "600",
  },
  brand: {
    color: colors.onSurface,
    fontSize: 44,
    fontWeight: "900",
    letterSpacing: 2,
    marginBottom: spacing.sm,
  },
  tagline: {
    color: colors.onSurfaceTertiary,
    fontSize: 15,
    lineHeight: 22,
  },
  section: {
    paddingHorizontal: spacing.xl,
    marginTop: spacing.xl,
  },
  sectionTitle: {
    color: colors.onSurface,
    fontSize: 20,
    fontWeight: "800",
    letterSpacing: 1.5,
  },
  sectionSub: {
    color: colors.onSurfaceTertiary,
    fontSize: 13,
    marginTop: spacing.xs,
    marginBottom: spacing.lg,
  },
  tipsList: {
    gap: spacing.md,
  },
  tipCard: {
    flexDirection: "row",
    alignItems: "center",
    backgroundColor: colors.surfaceSecondary,
    borderRadius: radius.lg,
    padding: spacing.lg,
    gap: spacing.md,
    borderWidth: 1,
    borderColor: colors.border,
  },
  tipIndex: {
    width: 32,
  },
  tipIndexText: {
    color: colors.brandPrimary,
    fontSize: 20,
    fontWeight: "800",
    letterSpacing: 1,
  },
  tipIcon: {
    width: 40,
    height: 40,
    borderRadius: radius.md,
    backgroundColor: colors.brandTertiary,
    alignItems: "center",
    justifyContent: "center",
  },
  tipTitle: {
    color: colors.onSurface,
    fontSize: 15,
    fontWeight: "800",
    letterSpacing: 1.5,
    marginBottom: 2,
  },
  tipBody: {
    color: colors.onSurfaceTertiary,
    fontSize: 13,
    lineHeight: 18,
  },
  infoCard: {
    flexDirection: "row",
    alignItems: "flex-start",
    backgroundColor: colors.surfaceSecondary,
    borderRadius: radius.lg,
    padding: spacing.lg,
    gap: spacing.md,
    borderWidth: 1,
    borderColor: colors.border,
  },
  infoText: {
    flex: 1,
    color: colors.onSurfaceSecondary,
    fontSize: 13,
    lineHeight: 19,
  },
  stickyBar: {
    position: "absolute",
    left: 0,
    right: 0,
    bottom: 0,
    paddingHorizontal: spacing.xl,
    paddingTop: spacing.lg,
  },
  stickyScrim: {
    position: "absolute",
    left: 0,
    right: 0,
    top: -40,
    height: 60,
  },
  triggerBtn: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "center",
    gap: spacing.md,
    height: 60,
    borderRadius: radius.lg,
    backgroundColor: colors.brandPrimary,
    shadowColor: colors.brandPrimary,
    shadowOpacity: 0.5,
    shadowRadius: 20,
    shadowOffset: { width: 0, height: 6 },
    elevation: 8,
  },
  triggerText: {
    color: colors.onBrandPrimary,
    fontSize: 16,
    fontWeight: "800",
    letterSpacing: 2,
  },
});
