import { useCallback, useState } from "react";
import {
  ActivityIndicator,
  Linking,
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
import * as Location from "expo-location";
import AsyncStorage from "@react-native-async-storage/async-storage";
import Constants from "expo-constants";

import { AppleWatchNote } from "@/src/components/AppleWatchNote";
import { colors, radius, spacing } from "@/src/theme";
import { registerForPushNotifications } from "@/src/utils/push";
import { confirmWatchChecked, snoozeWatchReminder } from "@/src/utils/watchReminder";
import { getDeviceId } from "@/src/utils/checkin";

const BACKEND_URL =
  process.env.EXPO_PUBLIC_BACKEND_URL ??
  (Constants.expoConfig?.extra as any)?.EXPO_PUBLIC_BACKEND_URL;

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
  // #285 (2026-08-22 — Paul): "The setup screen is doing too much... Split
  // the critical alerts screen into two steps. One screen, one decision."
  //   permission = allow the siren (and the two iOS prompts that follow)
  //   watch      = the Apple Watch check, on its own screen
  //   rehearsal  = hear the real siren, safely
  // #289 (2026-08-23 — Paul): location gets its own step, explained
  // before it is asked for. It used to be requested by the practice button
  // and again by the alert screen, which is how iOS put its location box on
  // top of a playing siren. Setup asks; nothing else ever does.
  // #291: the name is NOT a step. It is optional, the app works without it,
  // and every extra screen before someone is protected is a chance they
  // abandon setup halfway. It is asked on the home screen afterwards.
  const [step, setStep] = useState<
    "permission" | "watch" | "location" | "rehearsal"
  >("permission");
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
    // #305 (2026-08-23 — Paul): new installs start on "everything nearby",
    // so the app stays visible in the years between felt earthquakes.
    // Written HERE and nowhere else, because this code path only runs for
    // someone going through setup — an existing user never returns to it,
    // so nobody's setting is changed behind their back.
    try {
      const did = await getDeviceId();
      await fetch(`${BACKEND_URL}/api/devices/notification-preset`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ device_id: did, preset: "everything" }),
      });
    } catch {
      // Offline at setup: the server default stands and the settings
      // screen still shows what is actually stored. Nothing is claimed.
    }
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
    // #285: the Watch question gets its own screen, before the rehearsal.
    setStep("watch");
  }, [busy, markDone]);

  const onSkip = useCallback(async () => {
    if (busy) return;
    await markDone();
    // §6 #144: "Not now" still leads on through setup — these are
    // independent decisions. Someone who declines still benefits from
    // knowing what the siren sounds and feels like inside the app, and the
    // home screen will tell them, permanently, that it cannot sound (#281).
    setStep("watch");
  }, [busy, markDone]);

  const onWatchChecked = useCallback(async () => {
    await confirmWatchChecked();
    setStep("location");
  }, []);

  const onWatchSnooze = useCallback(async () => {
    await snoozeWatchReminder();
    setStep("location");
  }, []);

  // #289 — step 3: location, asked here and nowhere else.
  const onAllowLocation = useCallback(async () => {
    if (busy) return;
    setBusy(true);
    try {
      const res = await Location.requestForegroundPermissionsAsync();
      if (!res.granted && !res.canAskAgain) {
        // Refused for good. No dead end: the home screen carries a
        // permanent line saying what still works and what does not, with a
        // way into iOS settings.
        Linking.openSettings().catch(() => {});
      }
    } catch (e) {
      console.log("[QuakeAngel] location step err:", (e as Error)?.message);
    }
    setBusy(false);
    setStep("rehearsal");
  }, [busy]);

  const onSkipLocation = useCallback(() => {
    setStep("rehearsal");
  }, []);

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

  // #285 — step 2: the Apple Watch check, on its own screen, one decision.
  //
  // #286 (2026-08-22 — Paul) on the answer we accept here:
  //   "'I don't own an Apple Watch — don't show this again' is a decision
  //    someone makes today about a phone they may pair to a Watch next
  //    Christmas... Never let a safety reminder be permanently dismissed on
  //    the basis of something that can change."
  // So there is no permanent no on this screen either — see
  // src/utils/watchReminder.ts for the technical answer on detection and
  // why we ask rather than detect.
  if (step === "watch") {
    return (
      <View style={styles.root}>
        <StatusBar style="light" />
        <Stack.Screen
          options={{ headerShown: false, gestureEnabled: false, animation: "fade" }}
        />
        <SafeAreaView style={styles.safe} edges={["top"]}>
          <ScrollView
            style={{ flex: 1 }}
            contentContainerStyle={styles.body}
            showsVerticalScrollIndicator={false}
          >
            <View style={styles.brandRow}>
              <View style={styles.brandDot} />
              <Text style={styles.brandLabel}>Quake Angel · setup · step 2 of 4</Text>
            </View>
            <Text style={styles.h1}>Do you wear an Apple Watch?</Text>
            <Text style={styles.subtitle}>
              If you do, your iPhone can send the alert to your wrist instead of
              sounding out loud. A tap on the wrist is easy to sleep through.
            </Text>
            <View style={{ marginTop: spacing.lg }}>
              <AppleWatchNote variant="onboarding" />
            </View>
          </ScrollView>

          <View
            style={[
              styles.ctaBar,
              { paddingBottom: Math.max(insets.bottom, spacing.lg) },
            ]}
          >
            <Pressable
              onPress={onWatchChecked}
              style={({ pressed }) => [
                styles.primaryBtn,
                pressed && { opacity: 0.9, transform: [{ scale: 0.98 }] },
              ]}
              testID="onboarding-watch-checked-btn"
            >
              <Ionicons name="checkmark-circle" size={20} color={colors.onBrandPrimary} />
              <Text style={styles.primaryBtnText}>I have checked this</Text>
            </Pressable>
            <Pressable
              onPress={onWatchSnooze}
              style={styles.secondaryBtn}
              testID="onboarding-watch-snooze-btn"
            >
              <Text style={styles.secondaryBtnText}>
                I don&apos;t have one — ask me again in a few months
              </Text>
            </Pressable>
          </View>
        </SafeAreaView>
      </View>
    );
  }

  // #289 — step 3: location. Explained before it is asked for, and never
  // asked for anywhere else in the app.
  if (step === "location") {
    return (
      <View style={styles.root}>
        <StatusBar style="light" />
        <Stack.Screen
          options={{ headerShown: false, gestureEnabled: false, animation: "fade" }}
        />
        <SafeAreaView style={styles.safe} edges={["top"]}>
          <ScrollView
            style={{ flex: 1 }}
            contentContainerStyle={styles.body}
            showsVerticalScrollIndicator={false}
          >
            <View style={styles.brandRow}>
              <View style={styles.brandDot} />
              <Text style={styles.brandLabel}>Quake Angel · setup · step 3 of 4</Text>
            </View>
            <Text style={styles.h1}>Where should we send help?</Text>
            <Text style={styles.subtitle}>
              If you ask for help, we send your place with it so a team knows
              where to go. Your phone only shares it when you tap I need help
              or I&apos;m safe.
            </Text>

            <View style={styles.reassurePanel}>
              <Text style={styles.reassureTitle}>If you say no</Text>
              <Text style={styles.reassureBody}>
                Your rescue code still reaches the people running the
                response, and your name and code are still read out.
                {"\n\n"}
                What they will not have is a pin on the map for you. Someone
                would have to be told where you are another way.
                {"\n\n"}
                You can change your mind later. The home screen will keep
                reminding you while it is off.
              </Text>
            </View>

            <Text style={styles.footnote}>
              We never watch where you go. Nothing is sent until you tap one
              of the two buttons on an alert.
            </Text>
          </ScrollView>

          <View
            style={[
              styles.ctaBar,
              { paddingBottom: Math.max(insets.bottom, spacing.lg) },
            ]}
          >
            <Pressable
              onPress={onAllowLocation}
              disabled={busy}
              style={({ pressed }) => [
                styles.primaryBtn,
                pressed && { opacity: 0.9, transform: [{ scale: 0.98 }] },
                busy && { opacity: 0.7 },
              ]}
              testID="onboarding-location-allow-btn"
            >
              {busy ? (
                <ActivityIndicator color={colors.onBrandPrimary} />
              ) : (
                <Ionicons name="location" size={20} color={colors.onBrandPrimary} />
              )}
              <Text style={styles.primaryBtnText}>
                {busy ? "Asking your phone…" : "Share my place when I ask for help"}
              </Text>
            </Pressable>
            <Pressable
              onPress={onSkipLocation}
              disabled={busy}
              style={styles.secondaryBtn}
              testID="onboarding-location-skip-btn"
            >
              <Text style={styles.secondaryBtnText}>Not now</Text>
            </Pressable>
          </View>
        </SafeAreaView>
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
            style={{ flex: 1 }}
            contentContainerStyle={styles.body}
            showsVerticalScrollIndicator={false}
          >
            <View style={styles.brandRow}>
              <View style={styles.brandDot} />
              <Text style={styles.brandLabel}>Quake Angel · setup · step 4 of 4</Text>
            </View>
            <Text style={styles.h1}>Hear what a real alert sounds like</Text>
            <Text style={styles.subtitle}>
              This plays the exact siren and vibration you&apos;ll get during
              a real alert — right now, safely. Nothing is sent to anyone,
              and no rescue report is filed.
            </Text>

            <View style={styles.bulletList}>
              <BulletRow
                icon="volume-high"
                title="The real siren"
                body="The same siren you would hear in a real earthquake, at full volume."
              />
              <BulletRow
                icon="phone-portrait"
                title="A buzz you'll feel"
                body="Your phone buzzes the whole time the siren plays. If System Haptics is off in your iPhone settings, no app can make it buzz."
              />
              <BulletRow
                icon="shield-checkmark"
                title="Practice what to do"
                body="Try tapping I'm safe or I need help — exactly like the real thing."
              />
            </View>

            <Text style={styles.footnote}>
              You can run this again anytime — look for{" "}
              <Text style={styles.footnoteBold}>Practise the alert</Text> on
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
              <Text style={styles.primaryBtnText}>Play the practice siren</Text>
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
          style={{ flex: 1 }}
          contentContainerStyle={styles.body}
          showsVerticalScrollIndicator={false}
        >
          {/* Header */}
          <View style={styles.brandRow}>
            <View style={styles.brandDot} />
            <Text style={styles.brandLabel}>Quake Angel · setup · step 1 of 4</Text>
          </View>
          <Text style={styles.h1}>Let the siren sound</Text>
          <Text style={styles.subtitle}>
            An earthquake alert has to be able to sound even when your phone is
            on silent. That needs your permission.
          </Text>

          {/* #285 (2026-08-22 — Paul): "a reader could reasonably conclude
              they will be sirened by every tremor, and nobody grants that
              permission." Specific, not vague. No promise to wake anyone,
              and nothing that could be read as warning before a quake. */}
          <View style={styles.reassurePanel}>
            <Text style={styles.reassureTitle}>How rare this is</Text>
            <Text style={styles.reassureBody}>
              The siren is only for a real earthquake close enough to be felt
              where you are. Small or distant tremors never siren.
              {"\n\n"}
              This is rare. Malta goes years between earthquakes strong enough
              to trigger it.
            </Text>
          </View>

          {/* #285: the reader has no way to know these are two separate
              things unless we say so here. */}
          <View style={styles.reassurePanel}>
            <Text style={styles.reassureTitle}>Two separate things</Text>
            <Text style={styles.reassureBody}>
              The siren is for a dangerous earthquake.
              {"\n\n"}
              Tremor notices are quiet messages about small shakes nearby. You
              can turn those off completely, or see only the bigger ones, and
              the siren is not affected either way.
            </Text>
          </View>

          {/* Bullet list of what the permission enables */}
          <View style={styles.bulletList}>
            <BulletRow
              icon="volume-high"
              title="Loud siren, even on Silent"
              body="The siren sounds even if your phone is on silent or set to Do Not Disturb."
            />
            <BulletRow
              icon="phone-portrait"
              title="Wakes the screen"
              body="Your iPhone lights up when the alert arrives."
            />
            <BulletRow
              icon="lock-open"
              title="Sent by the fastest route"
              body="We send it the fastest way Apple allows. How quickly it lands depends on your phone's signal."
            />
            {/* #279 (2026-08-21 — Paul): said at setup, not discovered in an
                earthquake. His own check-in question arrived inside a Focus
                mode with no sound, hidden in a collapsed group. An alert
                breaks through Focus; a check-in question does not, and we
                are not making it a critical alert to fix that — the
                entitlement matters more. So people are told plainly. */}
            <BulletRow
              icon="moon-outline"
              title="Check-in questions can be silenced"
              body="After a quake we may ask how you are. A Focus mode can hide that question. An earthquake alert is set to come through Focus and silent — it still needs signal and the permission above."
            />
          </View>

          {/* #285: "Before Apple's permission boxes appear, tell them what
              is coming." */}
          <View style={styles.headsUpPanel}>
            <Ionicons name="information-circle" size={20} color="#5DB1FF" />
            {/* #299 (2026-08-23 — Paul): the old version explained what
                each iOS box was for and confused him twice. "Messages"
                invites the fear of advertising, and describing the second
                box as being about the siren reads as asking twice for the
                same thing. Action first, reassurance second, explanation
                last — and no description of the individual boxes. */}
            <Text style={styles.headsUpText}>
              Your iPhone will ask twice. Say yes to both.
              {"\n\n"}
              This app never sends advertising. It only sends earthquake
              alerts, and notices about small tremors if you choose to have
              them.
              {"\n\n"}
              Your iPhone simply asks in two steps.
            </Text>
          </View>

          <Text style={styles.footnote}>
            You can change this later in{" "}
            <Text style={styles.footnoteBold}>Settings › Notifications › Quake Angel</Text>
            . To let check-in questions through a Focus mode, turn on{" "}
            <Text style={styles.footnoteBold}>Time Sensitive Notifications</Text>
            {" "}there too.
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
              {busy ? "Asking your phone…" : "Allow the siren"}
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
    // #302 (2026-08-23 — Paul): "It carries meaning, so it must be body
    // size and clearly readable." 11pt faint grey was neither.
    color: colors.onSurfaceSecondary,
    fontSize: 16,
    fontWeight: "700",
  },
  h1: {
    color: colors.onSurface,
    fontSize: 30,
    fontWeight: "900",
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
    fontSize: 14,
    lineHeight: 18,
  },
  footnote: {
    marginTop: spacing.xl,
    color: colors.onSurfaceTertiary,
    fontSize: 14,
    lineHeight: 18,
    textAlign: "center",
  },
  footnoteBold: {
    color: colors.onSurfaceSecondary,
    fontWeight: "700",
  },
  reassurePanel: {
    marginTop: spacing.xl,
    backgroundColor: "#131A26",
    borderColor: "#25324A",
    borderWidth: 1,
    borderRadius: radius.lg,
    padding: spacing.lg,
  },
  reassureTitle: {
    color: colors.onSurface,
    fontSize: 16,
    fontWeight: "700",
    marginBottom: 6,
  },
  reassureBody: {
    color: colors.onSurfaceSecondary,
    fontSize: 15,
    lineHeight: 22,
  },
  headsUpPanel: {
    flexDirection: "row",
    gap: 10,
    alignItems: "flex-start",
    marginTop: spacing.xl,
    backgroundColor: "#0F2540",
    borderColor: "#2A4A6B",
    borderWidth: 1,
    borderRadius: radius.lg,
    padding: spacing.lg,
  },
  headsUpText: {
    flex: 1,
    color: "#CFE4FA",
    fontSize: 15,
    lineHeight: 22,
  },
  ctaBar: {
    // #282: a flex sibling of the ScrollView, never over it. The old
    // absolute bar plus a magic `200 + insets.bottom` reserve buried the
    // last line of the body at large system text sizes.
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
