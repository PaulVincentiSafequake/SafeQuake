import { useCallback, useState } from "react";
import {
  ActivityIndicator,
  Platform,
  Pressable,
  ScrollView,
  StyleSheet,
  Text,
  View,
} from "react-native";
import { SafeAreaView, useSafeAreaInsets } from "react-native-safe-area-context";
import { Stack, useLocalSearchParams, useRouter } from "expo-router";
import { StatusBar } from "expo-status-bar";
import { Ionicons } from "@expo/vector-icons";
import * as Haptics from "expo-haptics";
import AsyncStorage from "@react-native-async-storage/async-storage";

import { AppleWatchNote } from "@/src/components/AppleWatchNote";
import { colors, radius, spacing } from "@/src/theme";
import { registerForPushNotifications } from "@/src/utils/push";

const ONBOARDING_DONE_KEY = "quakeguard_onboarding_done";

export default function OnboardingScreen() {
  const router = useRouter();
  const insets = useSafeAreaInsets();
  const [busy, setBusy] = useState(false);
  // §6 #144 (Neo round 2, 2026-06): setup now has a second step — a
  // skippable, repeatable rehearsal of the real siren + vibration, so a
  // user's first encounter with the alarm isn't a real 4am earthquake.
  // "permission" = the existing Critical Alerts ask (unchanged).
  // "rehearsal" = the new demo step, reached after Enable OR Skip.
  const [step, setStep] = useState<"permission" | "rehearsal">("permission");
  // ?preview=1 forces the iOS layout to render on web/Android — used by
  // devs to visually inspect the screen without a real iPhone. Has no
  // effect on real iOS devices.
  const { preview } = useLocalSearchParams<{ preview?: string }>();
  const forcePreview = preview === "1";

  const goHome = useCallback(() => {
    router.replace("/");
  }, [router]);

  const markDone = useCallback(async () => {
    try {
      await AsyncStorage.setItem(ONBOARDING_DONE_KEY, "1");
    } catch {}
  }, []);

  const onEnable = useCallback(async () => {
    if (busy) return;
    setBusy(true);
    await Haptics.impactAsync(Haptics.ImpactFeedbackStyle.Medium).catch(() => {});
    // registerForPushNotifications() itself asks the system for the
    // Critical Alerts permission via Notifications.requestPermissionsAsync
    // and then registers the resulting token with the backend.
    try {
      await registerForPushNotifications();
    } catch (e) {
      console.log("[QuakeGuard] onboarding permission flow err:", e);
    }
    await markDone();
    setBusy(false);
    // §6 #144: go to the rehearsal step instead of home — granting the
    // permission is not the end of setup any more.
    setStep("rehearsal");
  }, [busy, markDone]);

  const onSkip = useCallback(async () => {
    if (busy) return;
    await markDone();
    // §6 #144: "Not now" on the permission ask still leads to the
    // rehearsal offer — the two are independent decisions. Someone who
    // declines Critical Alerts still benefits from knowing what the
    // siren sounds and feels like inside the app.
    setStep("rehearsal");
  }, [busy, markDone]);

  // §6 #144 — the rehearsal itself.
  //
  // Honesty rule: this MUST exercise the real siren + real vibration, not
  // a description of them. It reuses the EXACT same path as Home's
  // "TRIGGER TEST ALERT" button (siren=1&test=1) — the same code that
  // plays siren.mp3 on loop and fires the warning haptic on mount, and
  // that never posts a real check-in or arms reminders (isTestRun guard
  // in app/alert.tsx). Single source of truth: if that path is ever
  // fixed or broken, this rehearsal changes with it automatically.
  //
  // "Repeatable" (Paul's explicit requirement): a user is never limited
  // to seeing this once. The exact same button lives permanently on the
  // Home screen ("TRIGGER TEST ALERT"), so anyone can replay the siren +
  // vibration + check-in flow at any time, not just during setup.
  const onPlayRehearsal = useCallback(() => {
    Haptics.impactAsync(Haptics.ImpactFeedbackStyle.Heavy).catch(() => {});
    router.push("/alert?siren=1&test=1");
  }, [router]);

  const onSkipRehearsal = useCallback(() => {
    goHome();
  }, [goHome]);

  // Safety net: on web / non-iOS this route is a no-op — render an
  // informational placeholder. We deliberately do NOT call router.replace
  // during the first commit because RootLayout's Stack may not be mounted
  // yet and expo-router will throw "Attempted to navigate before mounting
  // the Root Layout".
  if (Platform.OS !== "ios" && !forcePreview) {
    return (
      <View style={[styles.root, styles.centered]}>
        <StatusBar style="light" />
        <Stack.Screen options={{ headerShown: false }} />
        <Ionicons name="logo-apple" size={40} color={colors.onSurfaceTertiary} />
        <Text style={styles.nonIosTitle}>iOS-only setup step</Text>
        <Text style={styles.nonIosBody}>
          Critical Alerts and the Apple Watch note are iOS-specific. Return
          home to continue.
        </Text>
        <Pressable style={styles.nonIosBtn} onPress={() => router.replace("/")}>
          <Text style={styles.nonIosBtnText}>Continue</Text>
        </Pressable>
      </View>
    );
  }

  // §6 #144 (Neo round 2) — rehearsal step, reached after either Enable
  // or Not now on the permission ask. Same Stack.Screen options (no
  // gesture-dismiss, fade transition) so the two steps feel like one
  // continuous flow rather than a new screen.
  if (step === "rehearsal") {
    return (
      <View style={styles.root}>
        <StatusBar style="light" />
        <Stack.Screen
          options={{
            headerShown: false,
            gestureEnabled: false,
            animation: "fade",
          }}
        />
        <SafeAreaView style={styles.safe} edges={["top"]}>
          <ScrollView
            contentContainerStyle={[
              styles.body,
              { paddingBottom: 200 + insets.bottom },
            ]}
            showsVerticalScrollIndicator={false}
          >
            <View style={styles.brandRow}>
              <View style={styles.brandDot} />
              <Text style={styles.brandLabel}>QUAKE ANGEL · SETUP</Text>
            </View>
            <Text style={styles.h1}>Hear it before it happens</Text>
            <Text style={styles.subtitle}>
              This plays the exact siren and vibration you&apos;ll get during
              a real alert — right now, safely. Nothing is sent to anyone,
              and no rescue report is filed.
            </Text>

            <View style={styles.bulletList}>
              <BulletRow
                icon="volume-high"
                title="The real siren"
                body="Same sound file used for a genuine earthquake alert, at full volume."
              />
              <BulletRow
                icon="phone-portrait"
                title="A buzz you'll feel"
                body="Your phone vibrates too, so you'd notice it even face-down."
              />
              <BulletRow
                icon="shield-checkmark"
                title="Practice what to do"
                body="Try tapping I'M SAFE or I NEED HELP — exactly like the real thing."
              />
            </View>

            <Text style={styles.footnote}>
              You can run this again anytime — look for{" "}
              <Text style={styles.footnoteBold}>TRIGGER TEST ALERT</Text> on
              the Home screen.
            </Text>
          </ScrollView>

          <View
            style={[
              styles.ctaBar,
              { paddingBottom: Math.max(insets.bottom, spacing.lg) },
            ]}
          >
            <Pressable
              onPress={onPlayRehearsal}
              style={({ pressed }) => [
                styles.primaryBtn,
                pressed && { opacity: 0.9, transform: [{ scale: 0.98 }] },
              ]}
              testID="onboarding-rehearsal-play-btn"
            >
              <Ionicons name="play" size={20} color={colors.onBrandPrimary} />
              <Text style={styles.primaryBtnText}>PLAY THE DEMO</Text>
            </Pressable>
            <Pressable
              onPress={onSkipRehearsal}
              style={styles.secondaryBtn}
              testID="onboarding-rehearsal-skip-btn"
            >
              <Text style={styles.secondaryBtnText}>Skip — I&apos;ll try it later</Text>
            </Pressable>
          </View>
        </SafeAreaView>
      </View>
    );
  }

  return (
    <View style={styles.root}>
      <StatusBar style="light" />
      <Stack.Screen
        options={{
          headerShown: false,
          gestureEnabled: false,
          animation: "fade",
        }}
      />
      <SafeAreaView style={styles.safe} edges={["top"]}>
        <ScrollView
          contentContainerStyle={[
            styles.body,
            { paddingBottom: 200 + insets.bottom },
          ]}
          showsVerticalScrollIndicator={false}
        >
          {/* Header */}
          <View style={styles.brandRow}>
            <View style={styles.brandDot} />
            <Text style={styles.brandLabel}>QUAKE ANGEL · SETUP</Text>
          </View>
          <Text style={styles.h1}>Turn on Critical Alerts</Text>
          <Text style={styles.subtitle}>
            An earthquake alert must be able to sound on your phone even when
            it&apos;s on silent or in a Focus mode. Grant Critical Alerts so
            Quake Angel can do that.
          </Text>

          {/* Bullet list of what the permission enables */}
          <View style={styles.bulletList}>
            <BulletRow
              icon="volume-high"
              title="Loud siren, even on Silent"
              body="Alerts bypass the ringer switch and Focus/DND."
            />
            <BulletRow
              icon="phone-portrait"
              title="Wakes the screen"
              body="Your iPhone lights up so you don't miss the alert."
            />
            <BulletRow
              icon="lock-open"
              title="Delivered instantly"
              body="Uses Apple's push infrastructure for lowest latency."
            />
          </View>

          {/* Apple Watch caveat — shown right next to the permission ask */}
          <View style={{ marginTop: spacing.xl }}>
            <AppleWatchNote variant="onboarding" />
          </View>

          <Text style={styles.footnote}>
            You can change these settings later in{" "}
            <Text style={styles.footnoteBold}>Settings › Notifications › Quake Angel</Text>
            .
          </Text>
        </ScrollView>

        {/* Sticky CTA */}
        <View
          style={[
            styles.ctaBar,
            { paddingBottom: Math.max(insets.bottom, spacing.lg) },
          ]}
        >
          <Pressable
            onPress={onEnable}
            disabled={busy}
            style={({ pressed }) => [
              styles.primaryBtn,
              pressed && { opacity: 0.9, transform: [{ scale: 0.98 }] },
              busy && { opacity: 0.7 },
            ]}
            testID="onboarding-enable-btn"
          >
            {busy ? (
              <ActivityIndicator color={colors.onBrandPrimary} />
            ) : (
              <Ionicons
                name="notifications"
                size={20}
                color={colors.onBrandPrimary}
              />
            )}
            <Text style={styles.primaryBtnText}>
              {busy ? "REQUESTING…" : "ENABLE CRITICAL ALERTS"}
            </Text>
          </Pressable>
          <Pressable
            onPress={onSkip}
            disabled={busy}
            style={styles.secondaryBtn}
            testID="onboarding-skip-btn"
          >
            <Text style={styles.secondaryBtnText}>Not now</Text>
          </Pressable>
        </View>
      </SafeAreaView>
    </View>
  );
}

function BulletRow({
  icon,
  title,
  body,
}: {
  icon: keyof typeof Ionicons.glyphMap;
  title: string;
  body: string;
}) {
  return (
    <View style={styles.bulletRow}>
      <View style={styles.bulletIcon}>
        <Ionicons name={icon} size={18} color={colors.brandPrimary} />
      </View>
      <View style={{ flex: 1 }}>
        <Text style={styles.bulletTitle}>{title}</Text>
        <Text style={styles.bulletBody}>{body}</Text>
      </View>
    </View>
  );
}

const styles = StyleSheet.create({
  root: {
    flex: 1,
    backgroundColor: colors.surface,
  },
  centered: {
    alignItems: "center",
    justifyContent: "center",
    padding: spacing.xl,
    gap: spacing.md,
  },
  nonIosTitle: {
    color: colors.onSurface,
    fontSize: 18,
    fontWeight: "800",
    marginTop: spacing.md,
  },
  nonIosBody: {
    color: colors.onSurfaceTertiary,
    fontSize: 14,
    lineHeight: 20,
    textAlign: "center",
    maxWidth: 300,
  },
  nonIosBtn: {
    marginTop: spacing.lg,
    paddingHorizontal: spacing.xl,
    height: 44,
    borderRadius: radius.lg,
    alignItems: "center",
    justifyContent: "center",
    backgroundColor: colors.brandPrimary,
  },
  nonIosBtnText: {
    color: colors.onBrandPrimary,
    fontSize: 14,
    fontWeight: "700",
    letterSpacing: 1,
  },
  safe: {
    flex: 1,
  },
  body: {
    paddingHorizontal: spacing.xl,
    paddingTop: spacing.lg,
  },
  brandRow: {
    flexDirection: "row",
    alignItems: "center",
    gap: spacing.sm,
    marginBottom: spacing.lg,
  },
  brandDot: {
    width: 8,
    height: 8,
    borderRadius: 4,
    backgroundColor: colors.brandPrimary,
  },
  brandLabel: {
    color: colors.onSurfaceTertiary,
    fontSize: 11,
    letterSpacing: 2,
    fontWeight: "700",
  },
  h1: {
    color: colors.onSurface,
    fontSize: 30,
    fontWeight: "900",
    letterSpacing: 0.3,
    marginBottom: spacing.md,
  },
  subtitle: {
    color: colors.onSurfaceSecondary,
    fontSize: 15,
    lineHeight: 22,
    marginBottom: spacing.xl,
  },
  bulletList: {
    gap: spacing.md,
  },
  bulletRow: {
    flexDirection: "row",
    alignItems: "flex-start",
    gap: spacing.md,
    backgroundColor: colors.surfaceSecondary,
    borderRadius: radius.md,
    padding: spacing.md,
    borderWidth: 1,
    borderColor: colors.border,
  },
  bulletIcon: {
    width: 32,
    height: 32,
    borderRadius: 16,
    backgroundColor: colors.brandTertiary,
    alignItems: "center",
    justifyContent: "center",
  },
  bulletTitle: {
    color: colors.onSurface,
    fontSize: 14,
    fontWeight: "700",
    marginBottom: 2,
  },
  bulletBody: {
    color: colors.onSurfaceTertiary,
    fontSize: 13,
    lineHeight: 18,
  },
  footnote: {
    marginTop: spacing.xl,
    color: colors.onSurfaceTertiary,
    fontSize: 12,
    lineHeight: 18,
    textAlign: "center",
  },
  footnoteBold: {
    color: colors.onSurfaceSecondary,
    fontWeight: "700",
  },
  ctaBar: {
    position: "absolute",
    left: 0,
    right: 0,
    bottom: 0,
    paddingHorizontal: spacing.xl,
    paddingTop: spacing.lg,
    backgroundColor: colors.surface,
    borderTopWidth: 1,
    borderTopColor: colors.divider,
    gap: spacing.sm,
  },
  primaryBtn: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "center",
    gap: spacing.md,
    height: 58,
    borderRadius: radius.lg,
    backgroundColor: colors.brandPrimary,
  },
  primaryBtnText: {
    color: colors.onBrandPrimary,
    fontSize: 15,
    fontWeight: "800",
    letterSpacing: 1.5,
  },
  secondaryBtn: {
    height: 44,
    alignItems: "center",
    justifyContent: "center",
  },
  secondaryBtnText: {
    color: colors.onSurfaceTertiary,
    fontSize: 14,
    fontWeight: "600",
  },
});
