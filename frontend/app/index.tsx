import { useRouter, useLocalSearchParams } from "expo-router";
import { StatusBar } from "expo-status-bar";
import { Image } from "expo-image";
import { LinearGradient } from "expo-linear-gradient";
import { Ionicons } from "@expo/vector-icons";
import * as Haptics from "expo-haptics";
import * as Location from "expo-location";
import Constants from "expo-constants";
import AsyncStorage from "@react-native-async-storage/async-storage";
import { useEffect, useState } from "react";
import {
  ActivityIndicator,
  Modal,
  Platform,
  Pressable,
  ScrollView,
  StyleSheet,
  Text,
  View,
} from "react-native";
import { SafeAreaView, useSafeAreaInsets } from "react-native-safe-area-context";

import { AppleWatchNote } from "@/src/components/AppleWatchNote";
import { colors, radius, spacing } from "@/src/theme";
import { postStatus } from "@/src/utils/checkin";
import {
  cancelCheckInReminders,
  ensureNotificationSetup,
  scheduleCheckInReminders,
} from "@/src/utils/reminders";

const LAST_SEEN_VERSION_KEY = "quakeguard_last_seen_version";

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
  // ?preview=1 forces the iOS-only update-reminder banner to render on web
  // during development — has no effect on real devices.
  const { preview } = useLocalSearchParams<{ preview?: string }>();
  const forcePreview = preview === "1";
  // ── Post-update Apple Watch reminder ───────────────────────────────────
  // iOS is known to silently reset the Watch app's per-app notification
  // mirroring toggle after app updates (including TestFlight installs and
  // name/icon changes) — Apple Support docs and community reports confirm
  // this. Since we can't detect or control that toggle from JS, we track
  // the installed version and, on the FIRST launch after any version bump,
  // surface a dismissible amber card reminding the user to re-check.
  const [updateReminderVisible, setUpdateReminderVisible] = useState(false);
  const [watchModalOpen, setWatchModalOpen] = useState(false);
  const currentVersion = (Constants.expoConfig?.version as string) ?? null;

  useEffect(() => {
    // Only meaningful on iOS. Android users don't have this Watch-mirror
    // toggle to worry about. The forcePreview flag lets devs visually
    // verify the banner in the web preview.
    if (Platform.OS !== "ios" && !forcePreview) return;
    if (!currentVersion) return;
    (async () => {
      try {
        const seen = await AsyncStorage.getItem(LAST_SEEN_VERSION_KEY);
        // Fresh install (nothing stored) → don't show anything, just record.
        // Users who install for the first time haven't "updated".
        if (seen === null && !forcePreview) {
          await AsyncStorage.setItem(LAST_SEEN_VERSION_KEY, currentVersion);
          return;
        }
        if (seen !== currentVersion || forcePreview) {
          setUpdateReminderVisible(true);
        }
      } catch (e) {
        console.log("[QuakeGuard] version-seen check failed:", (e as Error)?.message);
      }
    })();
  }, [currentVersion, forcePreview]);

  const dismissUpdateReminder = async () => {
    setUpdateReminderVisible(false);
    if (!currentVersion) return;
    try {
      await AsyncStorage.setItem(LAST_SEEN_VERSION_KEY, currentVersion);
    } catch (e) {
      console.log("[QuakeGuard] version-seen persist failed:", (e as Error)?.message);
    }
  };

  const openWatchModal = () => {
    setWatchModalOpen(true);
    Haptics.impactAsync(Haptics.ImpactFeedbackStyle.Light).catch(() => {});
  };

  const handleTrigger = async () => {
    if (triggering) return;
    setTriggering(true);
    await Haptics.impactAsync(Haptics.ImpactFeedbackStyle.Heavy).catch(() => {});

    // 1) Try to grab a location instantly for the initial 'not_responding'
    //    ping so a red MARKER also appears on the dashboard map — not just a
    //    count. We use last-known first (returns immediately) and fall back
    //    to a coarse-but-fast fix if no cache exists.
    let servicesOn = true;
    let permGranted = false;
    try {
      servicesOn = await Location.hasServicesEnabledAsync();
      if (servicesOn) {
        const cur = await Location.getForegroundPermissionsAsync();
        if (cur.granted) {
          permGranted = true;
        } else if (cur.canAskAgain) {
          const req = await Location.requestForegroundPermissionsAsync();
          permGranted = req.status === "granted";
        }
      }
    } catch {
      // ignore — treat as no permission
    }

    let lastKnown: Location.LocationObject | null = null;
    if (permGranted) {
      try {
        lastKnown = await Location.getLastKnownPositionAsync({
          maxAge: 120_000,
          requiredAccuracy: 500,
        });
      } catch {
        lastKnown = null;
      }
    }

    const initialLocation = lastKnown
      ? {
          latitude: lastKnown.coords.latitude,
          longitude: lastKnown.coords.longitude,
          accuracy: lastKnown.coords.accuracy ?? null,
          error: null,
        }
      : {
          latitude: null,
          longitude: null,
          accuracy: null,
          error: !servicesOn
            ? "location_services_off"
            : !permGranted
              ? "permission_denied"
              : "no_cached_fix",
        };

    postStatus({
      status: "not_responding",
      location: initialLocation,
    }).catch(() => {});

    // 1b) If we didn't have a fresh cached fix, kick off a real GPS lookup in
    //     the background and re-POST 'not_responding' with the true coords the
    //     moment it lands — so the map marker snaps to the actual position.
    if (permGranted && (!lastKnown || (lastKnown.coords.accuracy ?? 9999) > 100)) {
      (async () => {
        try {
          const fresh = await Location.getCurrentPositionAsync({
            accuracy: Location.Accuracy.BestForNavigation,
          });
          await postStatus({
            status: "not_responding",
            location: {
              latitude: fresh.coords.latitude,
              longitude: fresh.coords.longitude,
              accuracy: fresh.coords.accuracy ?? null,
              error: null,
            },
          });
          console.log(
            "[QuakeGuard] refreshed not_responding location →",
            fresh.coords.latitude,
            fresh.coords.longitude,
            "±",
            fresh.coords.accuracy,
            "m",
          );
        } catch (e) {
          console.log(
            "[QuakeGuard] background GPS refresh failed:",
            (e as Error)?.message,
          );
        }
      })();
    }

    // 2) NOTE: Mobile app trigger is a LOCAL TEST — it no longer broadcasts to
    //    every registered device. Real cross-device broadcasts go through the
    //    password-protected "Trigger Earthquake Alert" button on the emergency
    //    personnel dashboard (POST /api/trigger-alert with X-Admin-Token).

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
            <Text style={styles.brand}>QUAKE ANGEL</Text>
            <Text style={styles.tagline}>
              Earthquake preparedness{"\n"}at your fingertips.
            </Text>
          </SafeAreaView>
        </View>

        {/* Post-update Apple Watch reminder — iOS only, once per version */}
        {updateReminderVisible ? (
          <View style={styles.section}>
            <View style={styles.updateReminderCard} testID="update-reminder-card">
              <View style={styles.updateReminderHeader}>
                <View style={styles.updateReminderIcon}>
                  <Ionicons name="watch-outline" size={20} color={colors.warning} />
                </View>
                <Text style={styles.updateReminderTitle}>
                  Just updated? Re-check your Apple Watch
                </Text>
              </View>
              <Text style={styles.updateReminderBody}>
                iOS often resets the Watch app&apos;s notification-mirroring
                toggle back to <Text style={styles.updateReminderBold}>on</Text>{" "}
                after an app update — even for critical alerts. If you turned
                it off before, please re-check it now.
              </Text>
              <View style={styles.updateReminderActions}>
                <Pressable
                  onPress={openWatchModal}
                  style={({ pressed }) => [
                    styles.updateReminderPrimary,
                    pressed && { opacity: 0.85 },
                  ]}
                  testID="update-reminder-show-steps"
                >
                  <Text style={styles.updateReminderPrimaryText}>Show me how</Text>
                </Pressable>
                <Pressable
                  onPress={dismissUpdateReminder}
                  style={styles.updateReminderSecondary}
                  testID="update-reminder-dismiss"
                  hitSlop={6}
                >
                  <Text style={styles.updateReminderSecondaryText}>Dismiss</Text>
                </Pressable>
              </View>
            </View>
          </View>
        ) : null}

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

        {/* Diagnostics link (discrete) */}
        <Pressable
          onPress={() => router.push("/diag")}
          style={styles.diagLinkRow}
          testID="open-diag"
        >
          <Ionicons name="pulse" size={14} color={colors.onSurfaceTertiary} />
          <Text style={styles.diagLinkText}>Diagnostics</Text>
        </Pressable>
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

      {/* Apple Watch help modal — opened from the post-update reminder card */}
      <Modal
        visible={watchModalOpen}
        animationType="slide"
        transparent
        onRequestClose={() => setWatchModalOpen(false)}
      >
        <View style={styles.watchModalBackdrop}>
          <View
            style={[
              styles.watchModalSheet,
              { paddingBottom: Math.max(insets.bottom + spacing.md, spacing.xl) },
            ]}
          >
            <View style={styles.watchModalHandle} />
            <ScrollView
              style={{ maxHeight: 620 }}
              contentContainerStyle={{ paddingBottom: spacing.md }}
              showsVerticalScrollIndicator={false}
            >
              <AppleWatchNote variant="onboarding" />
            </ScrollView>
            <Pressable
              onPress={() => {
                setWatchModalOpen(false);
                dismissUpdateReminder();
              }}
              style={({ pressed }) => [
                styles.watchModalGotIt,
                pressed && { opacity: 0.9 },
              ]}
              testID="watch-modal-got-it"
            >
              <Text style={styles.watchModalGotItText}>GOT IT</Text>
            </Pressable>
          </View>
        </View>
      </Modal>
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
  diagLinkRow: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "center",
    gap: 6,
    paddingVertical: 14,
    marginTop: 4,
  },
  diagLinkText: {
    color: colors.onSurfaceTertiary,
    fontSize: 12,
    fontWeight: "600",
    letterSpacing: 1,
    textTransform: "uppercase",
  },

  /* Post-update Apple Watch reminder */
  updateReminderCard: {
    backgroundColor: "#2A1F0A",
    borderRadius: radius.lg,
    borderWidth: 1,
    borderColor: "#4A3814",
    padding: spacing.lg,
    gap: spacing.md,
  },
  updateReminderHeader: {
    flexDirection: "row",
    alignItems: "center",
    gap: spacing.sm,
  },
  updateReminderIcon: {
    width: 34,
    height: 34,
    borderRadius: 17,
    backgroundColor: "#3B2C0F",
    alignItems: "center",
    justifyContent: "center",
  },
  updateReminderTitle: {
    flex: 1,
    color: colors.onSurface,
    fontSize: 16,
    fontWeight: "800",
    lineHeight: 22,
  },
  updateReminderBody: {
    color: colors.onSurfaceSecondary,
    fontSize: 14,
    lineHeight: 20,
  },
  updateReminderBold: {
    color: colors.onSurface,
    fontWeight: "800",
  },
  updateReminderActions: {
    flexDirection: "row",
    alignItems: "center",
    gap: spacing.md,
    marginTop: 2,
  },
  updateReminderPrimary: {
    flex: 1,
    height: 44,
    borderRadius: radius.md,
    backgroundColor: colors.warning,
    alignItems: "center",
    justifyContent: "center",
  },
  updateReminderPrimaryText: {
    color: "#1B1005",
    fontSize: 14,
    fontWeight: "800",
    letterSpacing: 0.5,
  },
  updateReminderSecondary: {
    height: 44,
    paddingHorizontal: spacing.md,
    alignItems: "center",
    justifyContent: "center",
  },
  updateReminderSecondaryText: {
    color: colors.onSurfaceTertiary,
    fontSize: 14,
    fontWeight: "700",
  },

  /* Apple Watch help modal */
  watchModalBackdrop: {
    flex: 1,
    backgroundColor: "rgba(0,0,0,0.6)",
    justifyContent: "flex-end",
  },
  watchModalSheet: {
    backgroundColor: colors.surface,
    borderTopLeftRadius: 20,
    borderTopRightRadius: 20,
    paddingTop: spacing.sm,
    paddingHorizontal: spacing.lg,
    gap: spacing.md,
  },
  watchModalHandle: {
    alignSelf: "center",
    width: 44,
    height: 4,
    borderRadius: 2,
    backgroundColor: "rgba(255,255,255,0.35)",
    marginBottom: spacing.sm,
  },
  watchModalGotIt: {
    height: 52,
    borderRadius: radius.md,
    backgroundColor: colors.warning,
    alignItems: "center",
    justifyContent: "center",
  },
  watchModalGotItText: {
    color: "#1B1005",
    fontSize: 15,
    fontWeight: "800",
    letterSpacing: 2,
  },
});
