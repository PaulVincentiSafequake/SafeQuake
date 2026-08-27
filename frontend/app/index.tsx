import { useRouter, useLocalSearchParams } from "expo-router";
import { StatusBar } from "expo-status-bar";
import { Image } from "expo-image";
import { LinearGradient } from "expo-linear-gradient";
import { Ionicons } from "@expo/vector-icons";
import * as Haptics from "expo-haptics";
import * as Location from "expo-location";
import Constants from "expo-constants";
import AsyncStorage from "@react-native-async-storage/async-storage";
import { useCallback, useEffect, useState } from "react";
import {
  ActivityIndicator,
  Alert,
  AppState,
  type AppStateStatus,
  Keyboard,
  KeyboardAvoidingView,
  Modal,
  Platform,
  Pressable,
  ScrollView,
  StyleSheet,
  Text,
  TextInput,
  View,
} from "react-native";
import { SafeAreaView, useSafeAreaInsets } from "react-native-safe-area-context";

import { AppleWatchNote } from "@/src/components/AppleWatchNote";
import {
  WATCH_CONFIRMED_AT_KEY,
  WATCH_SNOOZE_DAYS,
  confirmWatchChecked as confirmWatch,
  snoozeWatchReminder,
  watchReminderDue,
  watchReminderWhy,
  type WatchReminderReason,
} from "@/src/utils/watchReminder";
import EntitlementBanner from "@/src/components/EntitlementBanner";
import ReadinessBanner from "@/src/components/ReadinessBanner";
import {
  getTremorNoticeStats,
  markQuietenAsked,
  shouldAskToQuieten,
} from "@/src/utils/tremorNotices";
// #208 R4 — `shouldRedirectToAlert`/`toAlertQuery` moved to _layout.tsx
// so the unanswered-alert redirect fires from every screen, not just
// this one. Keeping the imports here would be dead weight.
import { colors, radius, spacing } from "@/src/theme";
import {
  getDeviceId,
  getDisplayName,
  getShortCode,
  markNamePrompted,
  submitStatus,
  sanitizeDisplayName,
  setDisplayName,
  wasNamePrompted,
} from "@/src/utils/checkin";
import {
  ensureNotificationSetup,
} from "@/src/utils/reminders";


const BACKEND_URL =
  process.env.EXPO_PUBLIC_BACKEND_URL ??
  (Constants.expoConfig?.extra as any)?.EXPO_PUBLIC_BACKEND_URL;

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
    // #283: DROP / COVER / HOLD ON stay in capitals — the recognised
    // international phrase, the one agreed exception. "Afterwards" is not
    // part of that phrase, so it is sentence case like everything else.
    title: "Afterwards",
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
  // ── Post-update Apple Watch reminder (sticky) ─────────────────────────
  // iOS silently resets the Watch app's per-app notification-mirroring
  // toggle after app updates, and the Watch's own software updates can flip
  // it independently of the phone app. Since we can't detect or control the
  // toggle from JS, we surface this to the user on EVERY app open until
  // they explicitly tap "I've checked this". After confirmation we keep a
  // small green pill visible so the state is always glanceable, and we
  // re-nag with the full banner if:
  //   (a) the app version differs from the version at confirmation, OR
  //   (b) more than WATCH_RECHECK_DAYS have passed since confirmation.
  const [watchState, setWatchState] = useState<
    | { kind: "loading" }
    | { kind: "hidden" } // non-iOS, or web without preview flag
    | { kind: "nag"; reason: WatchReminderReason }
    | { kind: "confirmed"; confirmedAt: number; daysAgo: number; daysUntilNext: number }
  >({ kind: "loading" });
  const [watchModalOpen, setWatchModalOpen] = useState(false);
  const currentVersion = (Constants.expoConfig?.version as string) ?? null;

  // ── Rescue-info identity: short code + optional first name ────────────
  // The short code is the last 5 chars of the device ID (uppercase), shown
  // prominently on the main screen and on the persistent lock-screen card
  // fired after a trapped submission. Together with the optional first name,
  // it lets an on-site responder confirm which pin corresponds to the
  // physical phone in front of them — used only as a local tie-breaker
  // among 2-3 pins already narrowed down by GPS.
  const [shortCode, setShortCode] = useState<string | null>(null);
  const [displayName, setDisplayNameState] = useState<string | null>(null);
  const [nameModalOpen, setNameModalOpen] = useState(false);
  const [nameDraft, setNameDraft] = useState("");
  // Track whether the modal was opened by the auto first-launch prompt vs
  // a manual tap on the pill. Auto-open uses "Skip" copy on the secondary
  // action; manual edit uses "Cancel". Same modal, different affordance.
  const [nameModalReason, setNameModalReason] = useState<"auto" | "manual">(
    "manual",
  );
  const [nameSaving, setNameSaving] = useState(false);
  // #291: the home-screen prompt. Visible until answered or declined once.
  const [namePromptOpen, setNamePromptOpen] = useState(false);
  // #305: the one-time "want fewer tremor notices?" question. Shown at most
  // once, ever, and never while an alert is live.
  const [quietenAsk, setQuietenAsk] = useState<{ received: number } | null>(null);

  const evaluateWatchState = useCallback(async () => {
    // Non-iOS / web without ?preview=1 → the entire feature is a no-op.
    if (Platform.OS !== "ios" && !forcePreview) {
      setWatchState({ kind: "hidden" });
      return;
    }
    if (!currentVersion) {
      setWatchState({ kind: "hidden" });
      return;
    }
    try {
      // #286: one rule, in src/utils/watchReminder.ts. This screen no longer
      // decides for itself — a second copy of the rule is how "don't show
      // this again" became permanent in the first place.
      const due = await watchReminderDue();
      if (due.due) {
        setWatchState({ kind: "nag", reason: due.reason });
        return;
      }
      const confirmedAtRaw = await AsyncStorage.getItem(WATCH_CONFIRMED_AT_KEY);
      const confirmedAt = confirmedAtRaw ? new Date(confirmedAtRaw).getTime() : NaN;
      if (!Number.isFinite(confirmedAt)) {
        setWatchState({ kind: "hidden" });
        return;
      }
      const elapsed = Date.now() - confirmedAt;
      const daysAgo = Math.max(0, Math.floor(elapsed / 86_400_000));
      setWatchState({
        kind: "confirmed",
        confirmedAt,
        daysAgo,
        daysUntilNext: Math.max(0, WATCH_SNOOZE_DAYS - daysAgo),
      });
    } catch (e) {
      console.log("[QuakeGuard] watch-state eval failed:", (e as Error)?.message);
      // Fail-safe: ask rather than hide. A tap costs nothing; an unheard
      // siren costs everything.
      setWatchState({ kind: "nag", reason: "never_answered" });
    }
  }, [currentVersion, forcePreview]);

  // #208 R4 (Batch 7) — moved (Neo, 2026-08-20 — Paul):
  //   The unanswered-alert redirect now lives in `app/_layout.tsx` so
  //   it fires from ANY screen, not just Home. The bug that motivated
  //   the move: locking the phone on Diagnostics, receiving a real
  //   critical alert, tapping it, and being returned to Diagnostics
  //   instead of the check-in screen — because the notification tap
  //   handler's `router.push("/alert")` can race the router-ready
  //   state on resume from lock, and Home was the only place with a
  //   compensating watcher.
  //
  // This screen keeps no watcher of its own — a duplicate would race
  // the layout watcher and could stack two /alert screens during a
  // single unlock. The layout is the single source of truth.

  // Run on mount and every time app returns to foreground. The AppState
  // listener is what makes the banner truly sticky across sessions —
  // without it a user who backgrounds the app for a week would need to
  // fully kill it to trigger a re-check.
  useEffect(() => {
    evaluateWatchState();
  }, [evaluateWatchState]);

  // #305: decide once per app open. All the "do not ask" rules live in
  // src/utils/tremorNotices.ts so no screen can get them wrong.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        if (!(await shouldAskToQuieten())) return;
        const stats = await getTremorNoticeStats();
        if (!cancelled) setQuietenAsk({ received: stats.received });
      } catch { /* no question is the safe outcome */ }
    })();
    return () => { cancelled = true; };
  }, []);

  const answerQuieten = useCallback(async (fewer: boolean) => {
    setQuietenAsk(null);
    await markQuietenAsked().catch(() => {});
    if (!fewer) return;
    try {
      const did = await getDeviceId();
      await fetch(`${BACKEND_URL}/api/devices/notification-preset`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ device_id: did, preset: "noticeable" }),
      });
    } catch { /* they can also change it in settings */ }
  }, []);

  useEffect(() => {
    const sub = AppState.addEventListener("change", (next: AppStateStatus) => {
      if (next === "active") {
        evaluateWatchState();
      }
    });
    return () => sub.remove();
  }, [evaluateWatchState]);

  // Load short code + display name on mount, and open the first-launch
  // prompt exactly once if the user has never been asked. Using
  // AsyncStorage + a "prompted" flag keeps this cross-platform (iOS +
  // Android + web) without bolting it onto the iOS-only onboarding flow.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const [code, name, prompted] = await Promise.all([
          getShortCode(),
          getDisplayName(),
          wasNamePrompted(),
        ]);
        if (cancelled) return;
        setShortCode(code);
        setDisplayNameState(name);
        // #291 (2026-08-23 — Paul): this used to open the name editor by
        // itself the moment setup finished, which made it feel like a
        // fourth setup step — "Do not say three and show four." The name
        // is optional and the app works without it, so it is never a gate.
        // It is now a prompt on the home screen that stays until the
        // person answers it or says not now.
        setNamePromptOpen(!prompted && !name);
      } catch (e) {
        console.log("[QuakeAngel] load identity failed:", (e as Error)?.message);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const confirmWatchChecked = useCallback(async () => {
    await confirmWatch();   // src/utils/watchReminder.ts — one place
    setWatchState({
      kind: "confirmed",
      confirmedAt: Date.now(),
      daysAgo: 0,
      daysUntilNext: WATCH_SNOOZE_DAYS,
    });
    Haptics.notificationAsync(Haptics.NotificationFeedbackType.Success).catch(() => {});
  }, []);

  const openWatchModal = () => {
    setWatchModalOpen(true);
    Haptics.impactAsync(Haptics.ImpactFeedbackStyle.Light).catch(() => {});
  };

  // #286 (2026-08-22 — Paul): there is no permanent no here any more.
  //
  //   "That is a decision someone makes today about a phone they may pair to
  //    a Watch next Christmas. The moment they do, they fall into exactly the
  //    trap this notice exists to prevent, and nothing will ever tell them."
  //
  // "I don't have one" now means "ask me again in a few months", and we ask
  // sooner if iOS has had a major update or if the practice siren was heard
  // on a wrist. The rule and the reasons live in one place:
  // src/utils/watchReminder.ts — which also records the technical answer on
  // why we ask instead of detecting a paired Watch.
  const snoozeWatch = useCallback(() => {
    Alert.alert(
      "Do you have an Apple Watch?",
      "This only matters if you do. Your iPhone can send the alert to your " +
        "wrist instead of sounding out loud, and Apple switches that back on " +
        "after updates.\n\n" +
        "If you do not have one, we will stop asking for a few months, then " +
        "check again in case that has changed.",
      [
        { text: "I have one — keep reminding me", style: "cancel" },
        {
          text: "I don't have one",
          onPress: async () => {
            await snoozeWatchReminder();
            setWatchState({ kind: "hidden" });
            Haptics.impactAsync(Haptics.ImpactFeedbackStyle.Light).catch(() => {});
          },
        },
      ],
    );
  }, []);

  // ── Name modal handlers ──────────────────────────────────────────────
  const openNameEditor = useCallback(() => {
    setNameDraft(displayName ?? "");
    setNameModalReason("manual");
    setNameModalOpen(true);
    Haptics.impactAsync(Haptics.ImpactFeedbackStyle.Light).catch(() => {});
  }, [displayName]);

  const closeNameModal = useCallback(async (options?: { markPrompted?: boolean }) => {
    setNameModalOpen(false);
    Keyboard.dismiss();
    if (options?.markPrompted) {
      // "Skip" on the first-launch modal → remember we asked so we don't
      // re-open on next launch. On manual edits (markPrompted=false) we
      // leave the flag alone (it was already set the first time).
      try {
        await markNamePrompted();
      } catch {
        // ignore — worst case they see the prompt one more time
      }
    }
  }, []);

  const dismissNamePrompt = useCallback(async () => {
    setNamePromptOpen(false);
    try { await markNamePrompted(); } catch { /* they will see it once more */ }
  }, []);

  const saveDisplayName = useCallback(async () => {
    if (nameSaving) return;
    setNameSaving(true);
    try {
      // setDisplayName sanitizes (trim + control-char strip + 40-char cap)
      // and also flips the "prompted" flag, so both auto-prompt and
      // manual-edit paths converge on the same persisted state.
      const saved = await setDisplayName(nameDraft.trim() || null);
      setDisplayNameState(saved);
      setNamePromptOpen(false);
      Haptics.notificationAsync(Haptics.NotificationFeedbackType.Success).catch(
        () => {},
      );
    } catch (e) {
      console.log("[QuakeAngel] save name failed:", (e as Error)?.message);
    } finally {
      setNameSaving(false);
      setNameModalOpen(false);
      Keyboard.dismiss();
    }
  }, [nameDraft, nameSaving]);

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
        // #289: the practice button used to ask for location before it
        // opened the alert screen, which is how a system box ended up on
        // top of a playing siren. Check only, here and everywhere else
        // that runs during an alert or a practice.
        const cur = await Location.getForegroundPermissionsAsync();
        if (cur.granted) permGranted = true;
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

    submitStatus({
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
          await submitStatus({
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
    //    dashboard's "Trigger Earthquake Alert" button, which authenticates
    //    per-operator with a Google Sign-In JWT (POST /api/trigger-alert with
    //    Authorization: Bearer <jwt>) — every trigger is now attributed to a
    //    named operator in the audit trail. See Task #9 (2026-08).

    // 3) NO reminders for the in-app test trigger (batch 5, B1). A practice
    //    run must not nag the user 8 times over 11½ minutes. Reminders are
    //    scheduled ONLY when a genuine critical alert opens /alert (see
    //    app/alert.tsx) or arrives while the app is running (_layout.tsx).
    //    We still ensure notification permission/channel setup exists so the
    //    real path is ready when it matters.
    ensureNotificationSetup().catch(() => {});

    // 4) #169 — the TEST TRIGGER MUST EXERCISE THE REAL SIREN PATH.
    //
    //    This navigated to a bare "/alert" and was therefore SILENT from
    //    2026-08-06 (commit d3e8d81) onwards: that commit made the siren
    //    opt-in via `?siren=1` to stop informational preview taps from
    //    detonating it (BUG-2026-08-06-preview-tap-siren), and the test
    //    trigger — which never passed the param — was silenced as
    //    collateral damage. The screen appeared, the audio session was
    //    configured, the file was loaded, and nothing played.
    //
    //    `siren=1` is now passed so the test uses the IDENTICAL playback
    //    path as a real server-sent critical alert (see the
    //    kind="critical_alert" branch in app/_layout.tsx). A test that
    //    doesn't exercise the real path is worse than no test: it reports
    //    success for code that has never run.
    //
    //    `test=1` keeps the B1 promise separate — the siren plays, but a
    //    practice run still schedules no check-in reminders. Siren and
    //    reminders are now two independent decisions rather than one flag
    //    doing double duty, which is what let this hide.
    router.push("/alert?siren=1&test=1");
    setTriggering(false);
  };

  return (
    <View style={styles.root}>
      <StatusBar style="light" />

      {/* #282 (2026-08-22 — Paul, third time) THE LAYOUT RULE, FIXED AT
          THE CAUSE.

          "The red TRIGGER TEST ALERT button is sitting on top of the safety
           steps... the rescue code and name are overlapping the phone's
           status bar. This is #209 and #253 again, worse than before. Do
           not patch this screen. Find the layout rule that allows a fixed
           element to sit over content, fix it once at the cause."

          The cause was the pattern itself, not the numbers. The footer was
          `position: absolute` OVER the scroll area, and the scroll area
          reserved space for it — first with a magic number (#209), then
          with a measured height (#253). Both are guesses about a box whose
          height depends on the system text size, and a guess that is one
          line short puts a button on top of "Hold on".

          There is no reserve any more. The footer is an ordinary sibling in
          a flex column: ScrollView takes the space that is left, the footer
          takes what it needs, and overlap is not expressible in this
          layout. No measuring, no padding arithmetic, nothing to get wrong
          at a larger text size.

          THE RULE, for every screen: a footer or header that sits over
          scrollable text must be a flex sibling, never absolutely
          positioned. Absolute positioning is only for decoration (glows,
          rings, gradients) and for overlays on a map canvas, where there is
          no text underneath to bury. Applied here and in onboarding.tsx;
          alert.tsx and map.tsx were checked and are decoration/canvas
          only. */}
      <ScrollView
        style={{ flex: 1 }}
        contentContainerStyle={{ paddingBottom: spacing.md }}
        showsVerticalScrollIndicator={false}
      >
        {/* #281: the very first thing, above the hero and the rescue code,
            and only when something is actually wrong. */}
        <ReadinessBanner />
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
              {/* #86 (Batch 7 R4, 2026-08-19 night — Paul):
                    "Standing rule: never imply professional rescuers are
                     monitoring until that is contractually true."
                  The previous line "SYSTEM ACTIVE · MONITORING" implied
                  the exact reassurance the system cannot provide. Rule
                  9.9. Replaced with two short lines that describe what
                  the app genuinely does — an alert coming in AND a
                  report going out — approved verbatim. */}
              <View style={{ flex: 1 }}>
                <Text style={styles.statusText}>Connected and ready.</Text>
                <Text style={styles.statusSubText}>
                  If an earthquake happens near you, this phone sounds the
                  siren. You then tell us if you are safe or need help.
                </Text>
              </View>
            </View>
            <Text style={styles.brand}>QUAKE ANGEL</Text>
            <Text style={styles.tagline}>
              Earthquake preparedness{"\n"}at your fingertips.
            </Text>

            {/* Rescue-code pill — persistent, tap to edit name.
                Purpose: an on-site responder standing over a possibly
                unconscious victim can glance at this on the phone screen
                and match it to a specific pin on the dashboard, without
                unlocking the device or scanning through a list. This is a
                LOCAL tie-breaker among 2-3 nearby pins already narrowed
                down by GPS proximity — not a globally unique identifier.
                Tapping opens the name editor. */}
            {shortCode ? (
              <Pressable
                onPress={openNameEditor}
                hitSlop={8}
                testID="rescue-code-pill"
                style={({ pressed }) => [
                  styles.rescuePill,
                  pressed && { opacity: 0.85 },
                ]}
              >
                <View style={styles.rescuePillLeft}>
                  <Text style={styles.rescuePillLabel}>Rescue code</Text>
                  <Text style={styles.rescuePillCode}>{shortCode}</Text>
                </View>
                <View style={styles.rescuePillDivider} />
                <View style={styles.rescuePillRight}>
                  <Text style={styles.rescuePillLabel}>Name</Text>
                  <View style={styles.rescuePillNameRow}>
                    <Text
                      style={[
                        styles.rescuePillName,
                        !displayName && styles.rescuePillNameEmpty,
                      ]}
                      numberOfLines={1}
                    >
                      {displayName ?? "Add optional first name"}
                    </Text>
                    <Ionicons
                      name="pencil"
                      size={13}
                      color={colors.onSurfaceTertiary}
                    />
                  </View>
                </View>
              </Pressable>
            ) : null}

            {/* #291: obvious and easy, never a gate. It stays until they
                add a name or say not now — the same rule as every other
                prompt in this app: no silent disappearing. */}
            {/* #305 — asked once, ever, and never while an alert is live
                (this whole screen is replaced by /alert then). It says in
                one line that the siren is not affected, because someone
                turning notices down must not think they are turning down
                their protection. */}
            {quietenAsk ? (
              <View style={styles.namePrompt} testID="home-quieten-ask">
                <Text style={styles.namePromptTitle}>
                  You&apos;ve had {quietenAsk.received} tremor notices this week
                  and opened none of them. Want fewer?
                </Text>
                <Text style={styles.namePromptBody}>
                  This only changes the quiet notices about small shakes. The
                  earthquake siren is not affected either way.
                </Text>
                <View style={styles.namePromptRow}>
                  <Pressable
                    onPress={() => answerQuieten(true)}
                    style={styles.namePromptPrimary}
                    testID="home-quieten-fewer"
                  >
                    <Ionicons name="volume-low" size={16} color={colors.onBrandPrimary} />
                    <Text style={styles.namePromptPrimaryText}>
                      Yes — only shakes I&apos;d feel
                    </Text>
                  </Pressable>
                  <Pressable
                    onPress={() => answerQuieten(false)}
                    style={styles.namePromptSecondary}
                    hitSlop={8}
                    testID="home-quieten-keep"
                  >
                    <Text style={styles.namePromptSecondaryText}>
                      No — keep them all
                    </Text>
                  </Pressable>
                </View>
              </View>
            ) : null}

            {namePromptOpen ? (
              <View style={styles.namePrompt} testID="home-name-prompt">
                <Text style={styles.namePromptTitle}>
                  Add your first name?
                </Text>
                <Text style={styles.namePromptBody}>
                  If you ask for help, a rescuer sees your name beside your
                  code, so they know who they are looking for. You can leave
                  it out and the app works the same.
                </Text>
                <View style={styles.namePromptRow}>
                  <Pressable
                    onPress={openNameEditor}
                    style={styles.namePromptPrimary}
                    testID="home-name-prompt-add"
                  >
                    <Ionicons name="person-add" size={16} color={colors.onBrandPrimary} />
                    <Text style={styles.namePromptPrimaryText}>Add my name</Text>
                  </Pressable>
                  <Pressable
                    onPress={dismissNamePrompt}
                    style={styles.namePromptSecondary}
                    hitSlop={8}
                    testID="home-name-prompt-dismiss"
                  >
                    <Text style={styles.namePromptSecondaryText}>Not now</Text>
                  </Pressable>
                </View>
              </View>
            ) : null}
          </SafeAreaView>
        </View>

        {/* Sticky Apple Watch reminder — iOS only. Two mutually-exclusive
            surfaces:
              • Full amber banner (state = "nag") until user taps
                "I've checked this".
              • Small green pill (state = "confirmed") after confirmation,
                showing days-ago + days-until-next-check. Always tappable
                so the user can re-verify at any time. */}
        {watchState.kind === "nag" ? (
          <View style={styles.section}>
            <View style={styles.updateReminderCard} testID="update-reminder-card">
              <View style={styles.updateReminderHeader}>
                <View style={styles.updateReminderIcon}>
                  <Ionicons name="watch-outline" size={20} color={colors.warning} />
                </View>
                <Text style={styles.updateReminderTitle}>
                  {watchState.reason === "heard_on_wrist"
                    ? "Your siren played on your watch"
                    : watchState.reason === "new_ios"
                      ? "Your iPhone software has changed"
                      : watchState.reason === "snooze_expired"
                        ? "Checking again about an Apple Watch"
                        : "Do you wear an Apple Watch?"}
                </Text>
              </View>
              {/* #286: the reason is written in one place, so the card and
                  the settings screen can never explain it differently. */}
              <Text style={styles.updateReminderBody}>
                {watchReminderWhy(watchState.reason)}
                {"\n\n"}
                If your watch takes the alert, your phone may not sound out
                loud, and a tap on the wrist is easy to sleep through. Turn
                watch notifications for Quake Angel off, so the phone always
                sounds.
              </Text>
              <View style={styles.updateReminderActions}>
                <Pressable
                  onPress={confirmWatchChecked}
                  style={({ pressed }) => [
                    styles.updateReminderPrimary,
                    pressed && { opacity: 0.85 },
                  ]}
                  testID="update-reminder-confirm"
                >
                  <Ionicons name="checkmark-circle" size={18} color="#1B1005" />
                  <Text style={styles.updateReminderPrimaryText}>
                    I have checked this
                  </Text>
                </Pressable>
                <Pressable
                  onPress={openWatchModal}
                  style={styles.updateReminderSecondary}
                  testID="update-reminder-show-steps"
                  hitSlop={6}
                >
                  <Text style={styles.updateReminderSecondaryText}>
                    How do I check?
                  </Text>
                </Pressable>
              </View>
              <Text style={styles.updateReminderFootnote}>
                We ask again after an iPhone software update, because that can
                switch watch notifications back on. The app cannot see or
                change that setting itself.
              </Text>
              <Pressable
                onPress={snoozeWatch}
                style={styles.updateReminderOptOut}
                testID="update-reminder-no-watch"
                hitSlop={6}
              >
                <Text style={styles.updateReminderOptOutText}>
                  I don&apos;t have one — ask me again in a few months
                </Text>
              </Pressable>
            </View>
          </View>
        ) : watchState.kind === "confirmed" ? (
          <View style={styles.section}>
            <Pressable
              onPress={openWatchModal}
              style={({ pressed }) => [
                styles.watchOkPill,
                pressed && { opacity: 0.85 },
              ]}
              testID="watch-ok-pill"
            >
              <Ionicons name="checkmark-circle" size={16} color="#1F8A3A" />
              <Text style={styles.watchOkPillText}>
                Watch checked
                {watchState.daysAgo === 0
                  ? " · today"
                  : ` · ${watchState.daysAgo} day${watchState.daysAgo === 1 ? "" : "s"} ago`}
              </Text>
              <Text style={styles.watchOkPillMeta}>
                {watchState.daysUntilNext > 0
                  ? `we ask again in ${watchState.daysUntilNext} days`
                  : "due for a check"}
              </Text>
              <Ionicons name="chevron-forward" size={14} color={colors.onSurfaceTertiary} />
            </Pressable>
          </View>
        ) : null}

        {/* Subscription entitlement banner — shows only when the user
            is in grace/lapsed state, per /api/entitlement. Sits above
            the safety-protocol content but never blocks it. Copy
            invariant enforced backend-side: every variant tells the
            user critical alerts still work. See
            /app/frontend/src/components/EntitlementBanner.tsx. */}
        <EntitlementBanner />

        {/* Tips */}
        <View style={styles.section}>
          {/* #C12 (Batch 7 R4): heading is sentence case, plain English,
              says what it is FOR rather than what it IS. The four cards
              below still say DROP / COVER / HOLD ON in capitals —
              deliberate exception to sentence case (Paul), the
              internationally recognised standard phrase used by
              earthquake authorities worldwide (rule 9.11). */}
          <Text style={styles.sectionTitle}>What to do when it shakes</Text>
          <Text style={styles.sectionSub}>
            Learn these four steps. Every second counts.
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
              The button below is practice. It plays the real siren on this
              phone so you know what it sounds like, and lets you try saying
              you are safe.
            </Text>
          </View>
        </View>

        {/* Notification settings link — safety-critical placement.
            The user must be able to reach the informational-notification
            off-switch easily, otherwise a frustrated user reaches for
            iOS Settings' notification blanket switch and kills critical
            alerts too. Discrete but reachable in one tap from home. */}
        <Pressable
          onPress={() => router.push("/map" as any)}
          style={styles.diagLinkRow}
          testID="open-seismic-map"
          accessibilityRole="button"
          accessibilityLabel="Recent seismic activity map"
        >
          <Ionicons name="map-outline" size={14} color={colors.onSurfaceTertiary} />
          <Text style={styles.diagLinkText}>Recent seismic activity</Text>
        </Pressable>

        <Pressable
          onPress={() => router.push("/settings/notifications" as any)}
          style={styles.diagLinkRow}
          testID="open-notification-settings"
          accessibilityRole="button"
          accessibilityLabel="Notification settings"
        >
          <Ionicons name="notifications-outline" size={14} color={colors.onSurfaceTertiary} />
          <Text style={styles.diagLinkText}>Notification settings</Text>
        </Pressable>

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

      {/* Footer — a flex sibling of the ScrollView, never over it (#282). */}
      <View
        style={[
          styles.footerBar,
          { paddingBottom: Math.max(insets.bottom, spacing.lg) },
        ]}
      >
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
            {triggering ? "Starting the practice…" : "Practise the alert"}
          </Text>
        </Pressable>
        {/* #244 (Batch 7 D): honest wording about what this test does
            and does not prove. Full APNs round-trip (dashboard →
            Apple/Google → this phone) is a bigger cut and lives in
            the diagnostics screen. This one line prevents the "I
            tested the alert and it works" reading from concealing
            the fact that the network path was never exercised. */}
        <Text style={styles.triggerHonestyNote}>
          Plays the siren and the check-in screen on this phone only. Nothing
          is sent to anyone, and no report is filed.
        </Text>
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
              onPress={async () => {
                setWatchModalOpen(false);
                await confirmWatchChecked();
              }}
              style={({ pressed }) => [
                styles.watchModalGotIt,
                pressed && { opacity: 0.9 },
              ]}
              testID="watch-modal-got-it"
            >
              <Ionicons name="checkmark-circle" size={20} color="#1B1005" />
              <Text style={styles.watchModalGotItText}>I have checked this</Text>
            </Pressable>
          </View>
        </View>
      </Modal>

      {/* Name editor modal — served both for the one-shot first-launch
          prompt (nameModalReason === "auto") and every manual tap on the
          rescue-code pill (reason === "manual"). Same component, only
          the secondary-action copy changes. */}
      <Modal
        visible={nameModalOpen}
        animationType="slide"
        transparent
        onRequestClose={() =>
          closeNameModal({ markPrompted: nameModalReason === "auto" })
        }
      >
        <KeyboardAvoidingView
          behavior={Platform.OS === "ios" ? "padding" : undefined}
          style={styles.nameModalBackdrop}
        >
          <Pressable
            style={StyleSheet.absoluteFill}
            onPress={() =>
              closeNameModal({ markPrompted: nameModalReason === "auto" })
            }
          />
          <View
            style={[
              styles.nameModalSheet,
              { paddingBottom: Math.max(insets.bottom + spacing.md, spacing.xl) },
            ]}
          >
            <View style={styles.watchModalHandle} />
            <Text style={styles.nameModalTitle}>
              {nameModalReason === "auto"
                ? "Add your first name?"
                : displayName
                  ? "Edit your first name"
                  : "Add your first name"}
            </Text>
            <Text style={styles.nameModalBody}>
              Optional — helps a first responder confirm they&apos;ve found the
              right person, especially over radio. You can change or remove it
              anytime by tapping the rescue-code pill.
            </Text>

            <View style={styles.nameInputRow}>
              <Ionicons
                name="person"
                size={18}
                color={colors.onSurfaceTertiary}
                style={styles.nameInputIcon}
              />
              <TextInput
                value={nameDraft}
                onChangeText={(v) => {
                  // Live-cap at 40 chars so the input never displays more
                  // than the backend accepts. sanitizeDisplayName re-runs
                  // on save as a defence-in-depth belt.
                  const capped = v.length > 40 ? v.slice(0, 40) : v;
                  setNameDraft(capped);
                }}
                placeholder="e.g. Paul"
                placeholderTextColor={colors.onSurfaceTertiary}
                autoFocus={nameModalOpen}
                autoCapitalize="words"
                autoCorrect={false}
                maxLength={40}
                returnKeyType="done"
                onSubmitEditing={saveDisplayName}
                style={styles.nameInput}
                testID="name-input"
              />
              {nameDraft.length > 0 ? (
                <Pressable
                  onPress={() => setNameDraft("")}
                  hitSlop={10}
                  style={styles.nameInputClear}
                  testID="name-input-clear"
                >
                  <Ionicons
                    name="close-circle"
                    size={18}
                    color={colors.onSurfaceTertiary}
                  />
                </Pressable>
              ) : null}
            </View>

            {/* Live preview so the user can see how their name will appear
                next to the rescue code on the dashboard. */}
            <View style={styles.namePreviewRow}>
              <Text style={styles.namePreviewLabel}>If you ask for help, this is what appears:</Text>
              <View style={styles.namePreviewChip}>
                <Text style={styles.namePreviewChipName} numberOfLines={1}>
                  {sanitizeDisplayName(nameDraft) ?? "—"}
                </Text>
                <Text style={styles.namePreviewChipSep}>·</Text>
                <Text style={styles.namePreviewChipCode}>
                  {shortCode ?? "-----"}
                </Text>
              </View>
            </View>

            <Pressable
              onPress={saveDisplayName}
              disabled={nameSaving || (nameDraft.trim().length === 0 && !displayName)}
              style={({ pressed }) => [
                styles.nameModalPrimary,
                pressed && { opacity: 0.9 },
                nameSaving && { opacity: 0.7 },
              ]}
              testID="name-modal-save"
            >
              {nameSaving ? (
                <ActivityIndicator color={colors.onBrandPrimary} />
              ) : (
                <Ionicons
                  name="checkmark-circle"
                  size={20}
                  color={colors.onBrandPrimary}
                />
              )}
              {/* #291: "the red button should be the action a person
                  would normally take". It saves. It never skips. */}
              <Text style={styles.nameModalPrimaryText}>
                {nameSaving
                  ? "Saving…"
                  : nameDraft.trim().length === 0 && displayName
                    ? "Remove my name"
                    : "Save my name"}
              </Text>
            </Pressable>

            <Pressable
              onPress={() =>
                closeNameModal({ markPrompted: nameModalReason === "auto" })
              }
              style={styles.nameModalSecondary}
              testID="name-modal-secondary"
              hitSlop={8}
            >
              {/* One single way to decline. "Skip" and "Not now" side by
                  side meant the same thing twice. */}
              <Text style={styles.nameModalSecondaryText}>Not now</Text>
            </Pressable>
          </View>
        </KeyboardAvoidingView>
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
    // #282: minHeight, not height. At the largest system text size the
    // status line, the name and the rescue code are taller than 340pt, and
    // a fixed height pushed them up under the phone's status bar and
    // clipped them. The box grows with the words now.
    minHeight: 340,
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
    color: colors.onSurface,
    // §7 #257 (Neo round 2): body-size (was 13).
    fontSize: 14,
    fontWeight: "700",
  },
  statusSubText: {
    // §7 #257 (Neo 2026-08-20): body-size (was 12).
    color: colors.onSurfaceSecondary,
    fontSize: 14,
    fontWeight: "500",
    marginTop: 4,
    lineHeight: 20,
  },
  brand: {
    color: colors.onSurface,
    fontSize: 44,
    fontWeight: "900",
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
  },
  sectionSub: {
    color: colors.onSurfaceTertiary,
    // §7 #257 (Neo round 2): body-size (was 13).
    fontSize: 14,
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
    marginBottom: 2,
  },
  tipBody: {
    color: colors.onSurfaceTertiary,
    // §7 #257 (Neo round 2): body-size (was 13) — these are the DROP /
    // COVER / HOLD ON safety instructions; wording unchanged, only size.
    fontSize: 14,
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
    // §7 #257 (Neo round 2): body-size (was 13).
    fontSize: 14,
    lineHeight: 19,
  },
  footerBar: {
    // #282: an ordinary row at the bottom of the column. No absolute
    // positioning, so it cannot sit on top of the safety steps however
    // tall the person's text is.
    paddingHorizontal: spacing.xl,
    paddingTop: spacing.lg,
    backgroundColor: colors.surface,
    borderTopWidth: 1,
    borderTopColor: colors.divider,
  },
  triggerBtn: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "center",
    gap: spacing.md,
    // #282: minHeight so the label can wrap at a large text size instead
    // of spilling out of a fixed 60pt box and over the note below it.
    minHeight: 60,
    paddingVertical: spacing.md,
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
  },
  // #244 (Batch 7 D): honest disclaimer under the test button.
  // §7 #257 (Neo round 2): 11 → 13 → 14 for readability under stress.
  triggerHonestyNote: {
    color: colors.onSurfaceTertiary,
    fontSize: 14,
    lineHeight: 18,
    textAlign: "center",
    marginTop: 8,
    paddingHorizontal: spacing.md,
    fontStyle: "italic",
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
    // §7 #257 (Neo round 2): body-size (was 12).
    fontSize: 14,
    fontWeight: "600",
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
    flexDirection: "row",
    gap: 8,
  },
  updateReminderPrimaryText: {
    color: "#1B1005",
    fontSize: 14,
    fontWeight: "800",
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
  updateReminderFootnote: {
    marginTop: 4,
    color: colors.onSurfaceTertiary,
    // §7 #257 (Neo round 2): body-size (was 12).
    fontSize: 14,
    lineHeight: 17,
    fontStyle: "italic",
  },
  updateReminderOptOut: {
    marginTop: spacing.md,
    paddingVertical: spacing.sm,
    minHeight: 44,
    justifyContent: "center",
  },
  updateReminderOptOutText: {
    color: colors.onSurfaceSecondary,
    // §7 #257 (Neo round 2): body-size (was 13).
    fontSize: 14,
    textDecorationLine: "underline",
  },


  /* Confirmed-state pill — always visible until the next re-check triggers */
  watchOkPill: {
    flexDirection: "row",
    alignItems: "center",
    gap: 8,
    alignSelf: "flex-start",
    backgroundColor: "rgba(31, 138, 58, 0.12)",
    borderColor: "rgba(31, 138, 58, 0.4)",
    borderWidth: 1,
    borderRadius: 999,
    paddingLeft: 10,
    paddingRight: 8,
    paddingVertical: 6,
  },
  watchOkPillText: {
    color: "#7ED89A",
    // §7 #257 (Neo round 2): body-size (was 12).
    fontSize: 14,
    fontWeight: "700",
  },
  watchOkPillMeta: {
    color: colors.onSurfaceTertiary,
    // §7 #257 (Neo round 2): body-size (was 11).
    fontSize: 14,
    fontWeight: "600",
    marginLeft: 4,
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
    flexDirection: "row",
    gap: 10,
  },
  watchModalGotItText: {
    color: "#1B1005",
    fontSize: 15,
    fontWeight: "800",
  },

  /* ─── Rescue-code pill (hero) ────────────────────────────────────── */
  rescuePill: {
    marginTop: spacing.lg,
    flexDirection: "row",
    alignItems: "stretch",
    alignSelf: "stretch",
    backgroundColor: "rgba(15,17,21,0.55)",
    borderRadius: radius.md,
    borderWidth: 1,
    borderColor: "rgba(255,255,255,0.14)",
    paddingVertical: 10,
    paddingHorizontal: spacing.md,
    gap: spacing.md,
  },
  rescuePillLeft: {
    minWidth: 96,
  },
  // #291 — the home-screen name prompt.
  namePrompt: {
    marginTop: spacing.md,
    backgroundColor: "rgba(20,23,28,0.92)",
    borderRadius: radius.lg,
    borderWidth: 1,
    borderColor: "rgba(255,255,255,0.14)",
    padding: spacing.lg,
    gap: 6,
  },
  namePromptTitle: {
    color: colors.onSurface,
    fontSize: 18,
    fontWeight: "800",
  },
  namePromptBody: {
    color: colors.onSurfaceSecondary,
    fontSize: 15,
    lineHeight: 21,
  },
  namePromptRow: {
    flexDirection: "row",
    alignItems: "center",
    gap: spacing.md,
    marginTop: spacing.sm,
  },
  namePromptPrimary: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "center",
    gap: 8,
    backgroundColor: colors.brandPrimary,
    borderRadius: radius.md,
    paddingHorizontal: spacing.lg,
    minHeight: 48,
    flexShrink: 1,
  },
  namePromptPrimaryText: {
    color: colors.onBrandPrimary,
    fontSize: 16,
    fontWeight: "800",
  },
  namePromptSecondary: {
    minHeight: 48,
    justifyContent: "center",
    paddingHorizontal: spacing.sm,
  },
  namePromptSecondaryText: {
    color: colors.onSurfaceSecondary,
    fontSize: 16,
    fontWeight: "700",
  },
  rescuePillDivider: {
    width: 1,
    backgroundColor: "rgba(255,255,255,0.14)",
  },
  rescuePillRight: {
    flex: 1,
    justifyContent: "center",
  },
  rescuePillLabel: {
    color: colors.onSurfaceTertiary,
    fontSize: 10,
    fontWeight: "700",
    marginBottom: 2,
  },
  rescuePillCode: {
    color: colors.onSurface,
    fontSize: 22,
    fontWeight: "900",
    // Monospaced feel — matters when a responder reads it off the screen
    // out loud over radio.
    fontVariant: ["tabular-nums"],
  },
  rescuePillNameRow: {
    flexDirection: "row",
    alignItems: "center",
    gap: 6,
  },
  rescuePillName: {
    flex: 1,
    color: colors.onSurface,
    fontSize: 15,
    fontWeight: "700",
  },
  rescuePillNameEmpty: {
    color: colors.onSurfaceTertiary,
    fontWeight: "600",
    fontStyle: "italic",
  },

  /* ─── Name editor modal ──────────────────────────────────────────── */
  nameModalBackdrop: {
    flex: 1,
    backgroundColor: "rgba(0,0,0,0.6)",
    justifyContent: "flex-end",
  },
  nameModalSheet: {
    backgroundColor: colors.surface,
    borderTopLeftRadius: 20,
    borderTopRightRadius: 20,
    paddingTop: spacing.sm,
    paddingHorizontal: spacing.xl,
    gap: spacing.md,
  },
  nameModalTitle: {
    color: colors.onSurface,
    fontSize: 22,
    fontWeight: "800",
    marginTop: spacing.sm,
  },
  nameModalBody: {
    color: colors.onSurfaceSecondary,
    fontSize: 14,
    lineHeight: 20,
  },
  nameInputRow: {
    flexDirection: "row",
    alignItems: "center",
    backgroundColor: colors.surfaceSecondary,
    borderRadius: radius.md,
    borderWidth: 1,
    borderColor: colors.border,
    paddingHorizontal: spacing.md,
    minHeight: 52,
  },
  nameInputIcon: {
    marginRight: spacing.sm,
  },
  nameInput: {
    flex: 1,
    color: colors.onSurface,
    fontSize: 17,
    fontWeight: "600",
    paddingVertical: Platform.OS === "ios" ? 14 : 8,
  },
  nameInputClear: {
    padding: 6,
    marginLeft: 4,
  },
  namePreviewRow: {
    flexDirection: "row",
    alignItems: "center",
    gap: spacing.sm,
    flexWrap: "wrap",
  },
  namePreviewLabel: {
    color: colors.onSurfaceTertiary,
    // #283: was 12pt uppercase. Sentence case, body size — this line
    // explains what a stranger would see, so it has to be readable.
    fontSize: 14,
    fontWeight: "700",
  },
  namePreviewChip: {
    flexDirection: "row",
    alignItems: "center",
    gap: 8,
    backgroundColor: colors.surfaceSecondary,
    borderRadius: 999,
    borderWidth: 1,
    borderColor: colors.border,
    paddingHorizontal: 12,
    paddingVertical: 6,
  },
  namePreviewChipName: {
    color: colors.onSurface,
    fontSize: 14,
    fontWeight: "700",
    maxWidth: 180,
  },
  namePreviewChipSep: {
    color: colors.onSurfaceTertiary,
    fontSize: 14,
    fontWeight: "700",
  },
  namePreviewChipCode: {
    color: colors.brandPrimary,
    fontSize: 14,
    fontWeight: "900",
  },
  nameModalPrimary: {
    marginTop: spacing.sm,
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "center",
    gap: spacing.sm,
    height: 54,
    borderRadius: radius.md,
    backgroundColor: colors.brandPrimary,
  },
  nameModalPrimaryText: {
    color: colors.onBrandPrimary,
    fontSize: 15,
    fontWeight: "800",
  },
  nameModalSecondary: {
    height: 44,
    alignItems: "center",
    justifyContent: "center",
  },
  nameModalSecondaryText: {
    color: colors.onSurfaceTertiary,
    fontSize: 14,
    fontWeight: "700",
  },
});
