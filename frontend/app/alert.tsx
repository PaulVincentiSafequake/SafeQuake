import { useRouter, useLocalSearchParams } from "expo-router";
import { StatusBar } from "expo-status-bar";
import { Ionicons } from "@expo/vector-icons";
import { LinearGradient } from "expo-linear-gradient";
import * as Haptics from "expo-haptics";
import * as Location from "expo-location";
import * as Battery from "expo-battery";
import { useAudioPlayer, useAudioPlayerStatus, setAudioModeAsync } from "expo-audio";
import { useEffect, useRef, useState } from "react";
import {
  Modal,
  Pressable,
  StyleSheet,
  Text,
  View,
  useWindowDimensions,
} from "react-native";
import { SafeAreaView, useSafeAreaInsets } from "react-native-safe-area-context";
import Animated, {
  Easing,
  useAnimatedStyle,
  useSharedValue,
  withRepeat,
  withTiming,
  cancelAnimation,
} from "react-native-reanimated";

import { colors, radius, spacing } from "@/src/theme";
import {
  getShortCode,
  getDisplayName,
  postStatus,
  type Egress,
  type Mobility,
  type TriageSeverity,
} from "@/src/utils/checkin";
import {
  publishAlert,
  setAlertScreenMounted,
  subscribeToAlerts,
  type CriticalAlertEvent,
} from "@/src/utils/alertBus";
import {
  cancelCheckInReminders,
  cancelRescueInfoNotification,
  ensureNotificationSetup,
  postRescueInfoNotification,
  scheduleCheckInReminders,
} from "@/src/utils/reminders";
import { clearActiveAlert } from "@/src/utils/activeAlert";
import { resolveEventReadings } from "@/src/utils/eventReadings";

const SIREN_SOURCE = require("../assets/audio/siren.mp3");

type Status = "idle" | "sending" | "sent" | "error";
type OutcomeKind = "safe" | "trapped";

export default function AlertScreen() {
  const router = useRouter();
  // Event details from the notification payload (via router params).
  // BUG-2026-08-06-alert-hardcoded: previously the alert screen showed
  // literal "6.4", "12km", "VII" regardless of the triggering event.
  // These are now sourced from the notification's `data` fields at tap
  // time. Missing fields render as "—", NEVER as stale values.
  const params = useLocalSearchParams<{
    magnitude?: string;
    distance_km?: string;
    intensity?: string;
    depth_km?: string;
    region?: string;
    unid?: string;
    siren?: string;
    reminder?: string;
    test?: string;
    rehearse?: string;
  }>();
  // #205 (Batch 7 R4): the three constants that used to live here
  // (eventMagnitude / eventDistanceKm / eventIntensity, each reading
  // ONLY from URL params) have been removed. Every render now reads
  // from `eventReadings` below — the single function every surface on
  // this screen shares. See the block-comment there for the rule.
  // Siren defaults OFF when the param is missing — deliberate fail-safe.
  // Set by the notification-tap handler for kind=critical_alert AND by the
  // Trigger Test Alert button on Home, so a practice run exercises exactly
  // the same playback path as the real thing (#169). Direct /alert
  // navigation (e.g. dev browsing, preview taps) still stays silent.
  const shouldPlaySiren = params.siren === "1";
  // A practice run from Home. Plays the siren exactly like a real alert
  // (#169) but arms no check-in reminders (B1) — the two are separate
  // decisions, and conflating them is what silenced the siren for 11 days.
  const isTestRun = params.test === "1";
  // `reminder=1` is set by the tap handler when the user tapped a
  // check-in reminder notification. Currently unused in the render path
  // (reminders re-open the same check-in UI as fresh alerts) but the
  // param is preserved so future UI can hint "you're following up on
  // an earlier alert" without changing the tap-routing contract.
  // eslint-disable-next-line @typescript-eslint/no-unused-vars
  const isReminderContext = params.reminder === "1";
  const insets = useSafeAreaInsets();
  // Short-screen mode (batch 5, B2). iPhone SE/mini class devices can't fit
  // the 220pt pulse graphic + 40pt headline + data strip + two large action
  // buttons. Below this height the graphic and headline shrink; the data
  // strip and the buttons never do.
  const { height: windowHeight } = useWindowDimensions();
  const compact = windowHeight < 760;
  const [status, setStatus] = useState<Status>("idle");
  const [outcome, setOutcome] = useState<OutcomeKind>("safe");
  const [chosenSeverity, setChosenSeverity] =
    useState<TriageSeverity | null>(null);
  const [chosenMobility, setChosenMobility] = useState<Mobility | null>(null);
  const [triageOpen, setTriageOpen] = useState(false);
  // Between severity pick and submission we open a mobility follow-up. The
  // severity is held here so the mobility handler can forward it.
  const [pendingSeverity, setPendingSeverity] =
    useState<TriageSeverity | null>(null);
  const [mobilityOpen, setMobilityOpen] = useState(false);
  // GREEN-only egress follow-up (2026-06-18). #51 limits the mobility question
  // to yellow, so someone trapped but uninjured picked green and was never
  // asked whether they were stuck — they surfaced as "minor, walking wounded"
  // while physically unable to leave, and never appeared as an extraction case.
  // Mobility is not egress: the body versus the building.
  const [egressOpen, setEgressOpen] = useState(false);
  const [chosenEgress, setChosenEgress] = useState<Egress | null>(null);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const [elapsed, setElapsed] = useState(0);

  const pulse = useSharedValue(1);
  const ring = useSharedValue(0);
  // Warm-up: keep the freshest high-accuracy GPS fix we've received so far
  const latestFixRef = useRef<Location.LocationObject | null>(null);

  // Looping siren — starts once the audio source is loaded, stops when the
  // user marks themselves safe / dismisses / navigates away.
  // NOTE: useAudioPlayer + useAudioPlayerStatus is the correct API pair —
  // .play() before the source finishes loading is a no-op on iOS.
  const sirenPlayer = useAudioPlayer(SIREN_SOURCE);
  const sirenStatus = useAudioPlayerStatus(sirenPlayer);
  // Latching guard: once the user silences the siren (I'm Safe / Dismiss /
  // unmount), this flips permanently to false so no subsequent re-render or
  // player-status blip can restart playback.
  // Siren-should-play guard.
  //
  // Initial value: derived from `params.siren === "1"`. Only the
  // notification-tap handler for `kind: "critical_alert"` sets siren=1.
  // Every other path (reminders, preview taps, direct navigation, dev
  // browsing) reaches /alert with siren!=1 and the siren stays silent.
  //
  // BUG-2026-08-06-preview-tap-siren regression guard: if a future edit
  // ever removes this initial gate and defaults to `true`, a preview
  // notification tap could re-detonate the siren. Keep the default off.
  const shouldPlayRef = useRef(shouldPlaySiren);

  useEffect(() => {
    if (!sirenStatus.isLoaded) return;
    if (!shouldPlayRef.current) return;
    (async () => {
      try {
        // Belt-and-braces: (re)apply the audio session config just before
        // playing in case _layout.tsx's cold-start call was preempted.
        await setAudioModeAsync({
          playsInSilentMode: true,
          shouldPlayInBackground: false,
          interruptionMode: "doNotMix",
          allowsRecording: false,
        });
      } catch {
        // ignore — session may still be in a playable state
      }
      // Re-check the guard: setAudioModeAsync is awaited, and the user may
      // have tapped "I'm Safe" while we were setting the session.
      if (!shouldPlayRef.current) return;
      try {
        sirenPlayer.loop = true;
        sirenPlayer.volume = 1.0;
        sirenPlayer.play();
        // #169: leaves a trace in the device log that playback was actually
        // requested, so "the screen appeared but was silent" can be told
        // apart from "the sound path never ran" without guessing.
        console.log("[QuakeAngel] SIREN play() requested");
      } catch (e) {
        console.log("[QuakeAngel] SIREN play() threw:", (e as Error)?.message);
      }
    })();
  }, [sirenStatus.isLoaded, sirenPlayer]);

  // Unmount cleanup
  useEffect(() => {
    return () => {
      shouldPlayRef.current = false;
      try {
        sirenPlayer.pause();
      } catch {
        // ignore
      }
    };
  }, [sirenPlayer]);

  // ─── SIREN KILL-SWITCH ───────────────────────────────────────────────
  // Watches the player's live "playing" flag. If we EVER observe playback
  // active while shouldPlayRef.current is false (i.e. the user has already
  // silenced the siren via I'm Safe / triage / mobility / dismiss), force
  // it back to paused synchronously. This is defence-in-depth against any
  // edge case where expo-audio's internal state resurrects the player
  // across a re-render — e.g. the extra state transitions introduced by
  // the mobility follow-up modal.
  useEffect(() => {
    if (!sirenStatus.playing) return;
    if (shouldPlayRef.current) return;
    try {
      sirenPlayer.loop = false;
    } catch {
      // ignore
    }
    try {
      sirenPlayer.volume = 0;
    } catch {
      // ignore
    }
    try {
      sirenPlayer.pause();
    } catch {
      // ignore
    }
    try {
      sirenPlayer.seekTo(0);
    } catch {
      // ignore
    }
  }, [sirenStatus.playing, sirenPlayer]);

  // ─── FINAL SAFETY NET: status transitions to "sent" ───────────────────
  // Whenever a submission completes (safe or trapped), imperatively pause
  // the player one more time. stopSiren() ran synchronously the moment
  // the user tapped a triage option — this catches the pathological case
  // where something during the ~1-15s async submission (GPS acquisition,
  // battery read, network round-trip, React re-renders driven by
  // setState("sending") → setState("sent")) somehow revives playback.
  useEffect(() => {
    if (status !== "sent") return;
    shouldPlayRef.current = false;
    try {
      sirenPlayer.loop = false;
    } catch {
      // ignore
    }
    try {
      sirenPlayer.volume = 0;
    } catch {
      // ignore
    }
    try {
      sirenPlayer.pause();
    } catch {
      // ignore
    }
    try {
      sirenPlayer.seekTo(0);
    } catch {
      // ignore
    }
  }, [status, sirenPlayer]);

  // ─── CHECK-IN REMINDERS: real alerts only (batch 5, B1) ───────────────
  // Reminders used to be scheduled by the in-app TEST trigger on Home,
  // which meant a practice run nagged the user 8 times over 11½ minutes.
  // They now arm here, and only when this screen was opened by a genuine
  // critical alert (siren=1, set solely by the kind="critical_alert" tap
  // handler) or by a reminder from that alert. Test triggers and preview
  // taps reach /alert with siren!=1 and arm nothing.
  useEffect(() => {
    if (!shouldPlaySiren) return;
    if (isTestRun) return;   // practice run: siren yes, 11½ min of nagging no
    let cancelled = false;
    (async () => {
      const ok = await ensureNotificationSetup();
      if (!ok || cancelled) return;
      // cancel-then-schedule keeps overlapping sets from stacking when a
      // second alert arrives while the first is unanswered.
      await cancelCheckInReminders();
      if (cancelled) return;
      await scheduleCheckInReminders();
    })();
    return () => {
      cancelled = true;
    };
  }, [shouldPlaySiren, isTestRun]);

  // ─── AFTERSHOCKS: a new alert must never cost someone their answer ───
  // A second alert arriving while this screen is open is published to us by
  // _layout.tsx instead of navigating (which would remount this screen and
  // discard an in-progress answer — open triage sheet, chosen severity,
  // mobility answer — while leaving the old siren looping underneath).
  //
  // What we do with it: update the readings, say plainly that a new alert
  // arrived, and leave every piece of the user's state exactly where it was.
  // If they had already answered, we do NOT silently reset them — we offer an
  // explicit "Update my status" button, because deciding for them is how you
  // end up with a rescue list that doesn't match reality.
  const [aftershock, setAftershock] = useState<CriticalAlertEvent | null>(null);
  // #199/#202 (R4 companion): a stand-down push arriving on the alert
  // bus flips this to true, which replaces the check-in buttons with a
  // clear "stood down" panel and a home button. The user is NEVER
  // stranded on a check-in screen for an incident that no longer
  // exists. See handleStoodDown below.
  const [stoodDown, setStoodDown] = useState<{ reason: string } | null>(null);

  useEffect(() => {
    setAlertScreenMounted(true);
    return () => setAlertScreenMounted(false);
  }, []);

  useEffect(() => {
    return subscribeToAlerts((event: any) => {
      // Stand-down / incident-closed signal (silent push received while
      // this screen is mounted). Kill the siren immediately, mark the
      // screen as stood down. Do NOT auto-navigate — let the user tap
      // OK and go home themselves, so they see WHY the check-in is
      // gone. Silent auto-nav in a stress situation reads as a bug.
      if (event && event.stood_down) {
        shouldPlayRef.current = false;
        try { sirenPlayer.stop(); } catch { /* non-fatal */ }
        setStoodDown({
          reason:
            typeof event.stood_down_reason === "string"
              ? event.stood_down_reason
              : "false_alarm",
        });
        return;
      }
      setAftershock(event);
      // DELIBERATELY NO SIREN RE-ARM HERE.
      //
      // First cut of this re-started the siren for the new event. Testing
      // showed it restarting while the user was mid-triage — they had
      // silenced it by tapping I'M TRAPPED seconds earlier, and it came
      // back. That is precisely the failure shape of #31/#50 ("I'm safe
      // doesn't stop the siren"), and a siren that resurrects itself after
      // someone deliberately silenced it is worse than one that stays quiet:
      // it teaches them the button doesn't work.
      //
      // Audibility of the new event is already covered, and covered better,
      // by the push itself: iOS plays siren.caf (critical, volume 1.0) on
      // arrival regardless of what this screen is doing. So the new alert is
      // heard, and the person's control over the in-app siren is absolute.
    });
  }, []);

  // #205 (Batch 7 R4, 2026-08-19 night — verbatim to Paul's rule):
  //   "Name the single function that supplies magnitude, distance and
  //    intensity, and confirm all four surfaces read from it: the
  //    notification payload, the alert-screen panel, the aftershock
  //    banner, and the seismic detail page."
  //
  // This IS that function. It resolves the current reading for every
  // surface on the alert screen:
  //   - The notification payload lands its values in `params` (URL
  //     search params, populated by _layout.tsx handleTap).
  //   - An aftershock arriving mid-answer publishes its values via
  //     the alert bus into `aftershock` state.
  //   - The rehearsal path publishes the SAME synthetic reading
  //     through the SAME bus — so it flows through here as well.
  //
  // Rule: an aftershock (if present) is fresher than the URL params.
  // Both the aftershock banner and the metrics panel read from here.
  // Missing fields render as "—" — never as stale hard-coded 6.4/12/VII.
  //
  // What this fixes (Paul's screenshot, build 130): the amber banner
  // read "M5.1" while the panel below it read MAGNITUDE — because
  // banner and panel had different sources. Both now share this one.
  // #205 (Batch 7 R3) + §1 #174 (Neo 2026-08-20): the single resolver
  // used by /alert AND /quake/[unid]. Was previously an inline object
  // here that /quake/[unid] didn't share, so when the #205 fix landed
  // on the banner it left the informational detail screen showing
  // dashes. Now both surfaces call resolveEventReadings — pattern #1
  // enforced by shape, not by discipline.
  //
  // What this fixes (Paul's screenshot, build 130): the amber banner
  // read "M5.1" while the panel below it read MAGNITUDE — because
  // banner and panel had different sources. Both now share this one.
  const readings = resolveEventReadings(params, aftershock ?? undefined);
  const eventReadings = {
    magnitude: readings.magnitude,
    distance_km: readings.distance_km != null ? String(readings.distance_km) : null,
    intensity: readings.intensity,
    depth_km: readings.depth_km != null ? String(readings.depth_km) : null,
    region: readings.region,
    unid: readings.unid,
  };
  const aftershockMagnitude = aftershock?.magnitude ?? null;

  // Aftershock REHEARSAL (Paul, 2026-06-18). He asked to see the aftershock
  // case on his own phone: trigger an alert, then a second one before
  // answering the first. Home's Trigger Test Alert cannot show it — it
  // navigates locally, and once you are on this screen the button is behind
  // you, so a practice run could never reproduce the one case that matters.
  //
  // This publishes a synthetic second event through `publishAlert` — the
  // SAME bus, the SAME listener above, that a real push hits from
  // _layout.tsx. What it does not exercise is APNs delivery itself, and the
  // notice says so, because a rehearsal that quietly skips a step is worse
  // than no rehearsal.
  const isRehearsal = params.rehearse === "aftershock";
  useEffect(() => {
    if (!isRehearsal) return;
    const t = setTimeout(() => {
      publishAlert({
        magnitude: "5.1",
        distance_km: "38",
        intensity: "V",
        depth_km: "10",
        region: "REHEARSAL — not a real earthquake",
        unid: "rehearsal",
      });
    }, 12000);
    return () => clearTimeout(t);
  }, [isRehearsal]);

  const stopSiren = () => {
    // Flip the guard FIRST so any in-flight play effect bails out before
    // touching the hardware. Then forcibly silence: loop=false so any
    // buffered loop iteration doesn't wrap, volume=0 so residual samples
    // are inaudible, pause() to actually halt.
    shouldPlayRef.current = false;
    try {
      sirenPlayer.loop = false;
    } catch {
      // ignore
    }
    try {
      sirenPlayer.volume = 0;
    } catch {
      // ignore
    }
    try {
      sirenPlayer.pause();
    } catch {
      // ignore
    }
    try {
      sirenPlayer.seekTo(0);
    } catch {
      // ignore
    }
  };


  // Pre-warm the GPS the moment the alert opens so tapping "I'm Safe" uses a
  // real, fresh fix (avoids Wi-Fi / cell triangulation and cold-start delay).
  useEffect(() => {
    let sub: Location.LocationSubscription | null = null;
    let cancelled = false;
    (async () => {
      try {
        const servicesOn = await Location.hasServicesEnabledAsync();
        if (!servicesOn) return;
        const { status: permStatus } =
          await Location.requestForegroundPermissionsAsync();
        if (permStatus !== "granted" || cancelled) return;
        sub = await Location.watchPositionAsync(
          {
            accuracy: Location.Accuracy.BestForNavigation,
            timeInterval: 1000,
            distanceInterval: 0,
          },
          (loc) => {
            latestFixRef.current = loc;
          },
        );
      } catch {
        // swallow — handleImSafe will fall back to getCurrentPositionAsync
      }
    })();
    return () => {
      cancelled = true;
      try {
        sub?.remove();
      } catch {
        // expo-location's web shim lacks removeSubscription; safe to ignore
      }
    };
  }, []);

  useEffect(() => {
    pulse.value = withRepeat(
      withTiming(1.15, { duration: 700, easing: Easing.inOut(Easing.ease) }),
      -1,
      true,
    );
    ring.value = withRepeat(
      withTiming(1, { duration: 1400, easing: Easing.out(Easing.ease) }),
      -1,
      false,
    );

    // warning haptic once on mount
    Haptics.notificationAsync(Haptics.NotificationFeedbackType.Warning).catch(
      () => {},
    );

    const t = setInterval(() => setElapsed((s) => s + 1), 1000);
    return () => {
      cancelAnimation(pulse);
      cancelAnimation(ring);
      clearInterval(t);
    };
  }, [pulse, ring]);

  const iconStyle = useAnimatedStyle(() => ({
    transform: [{ scale: pulse.value }],
  }));
  const ringStyle = useAnimatedStyle(() => ({
    opacity: 1 - ring.value,
    transform: [{ scale: 1 + ring.value * 1.6 }],
  }));

  useEffect(() => {
    if (status !== "sent") return;
    // For "safe" check-ins, return to home after a moment.
    // For "trapped", stay on this screen — the user needs a persistent
    // "help is coming" confirmation, not an automatic redirect.
    if (outcome !== "safe") return;
    const nav = setTimeout(() => router.replace("/"), 1200);
    return () => clearTimeout(nav);
  }, [status, outcome, router]);

  const submitCheckIn = async (
    kind: OutcomeKind,
    severity: TriageSeverity | null = null,
    mobility: Mobility | null = null,
    egress: Egress | null = null,
  ) => {
    if (status === "sending" || status === "sent") return;
    // 1) IMMEDIATELY silence the siren and cancel pending reminders. The user
    //    tapping I'm Safe / a triage option is an explicit, unambiguous
    //    intent to stop the alarm — it must not wait for GPS acquisition,
    //    battery read, or the network round-trip. shouldPlayRef is latched
    //    inside stopSiren() so a network failure won't restart the siren.
    stopSiren();
    cancelCheckInReminders().catch(() => {});

    setOutcome(kind);
    setChosenSeverity(severity);
    setChosenMobility(mobility);
    setChosenEgress(egress);
    setStatus("sending");
    setErrorMsg(null);
    Haptics.notificationAsync(
      kind === "safe"
        ? Haptics.NotificationFeedbackType.Success
        : Haptics.NotificationFeedbackType.Warning,
    ).catch(() => {});

    // Gather location (with timeout guard) — force a fresh high-accuracy GPS fix
    let latitude: number | null = null;
    let longitude: number | null = null;
    let accuracy: number | null = null;
    let locationError: string | null = null;

    // 1) If our warm-up watcher already has a very recent, high-accuracy fix,
    //    use it — instant and truly the phone's live location.
    const cached = latestFixRef.current;
    if (
      cached &&
      Date.now() - cached.timestamp < 15000 &&
      cached.coords.accuracy !== null &&
      cached.coords.accuracy !== undefined &&
      cached.coords.accuracy <= 50
    ) {
      latitude = cached.coords.latitude;
      longitude = cached.coords.longitude;
      accuracy = cached.coords.accuracy ?? null;
      console.log(
        "[QuakeGuard] using warm GPS fix →",
        latitude,
        longitude,
        "±",
        accuracy,
        "m",
      );
    } else {
      try {
        // Make sure device location services are on (Android especially)
        const servicesOn = await Location.hasServicesEnabledAsync();
        if (!servicesOn) {
          locationError = "location_services_off";
        } else {
          const { status: permStatus } =
            await Location.requestForegroundPermissionsAsync();
          if (permStatus === "granted") {
            const posPromise = Location.getCurrentPositionAsync({
              // BestForNavigation → real GPS fix, not Wi-Fi / cell triangulation
              accuracy: Location.Accuracy.BestForNavigation,
              mayShowUserSettingsDialog: true,
            });
            const timeoutPromise = new Promise<never>((_, reject) =>
              setTimeout(() => reject(new Error("location_timeout")), 15000),
            );
            const pos = (await Promise.race([
              posPromise,
              timeoutPromise,
            ])) as Location.LocationObject;
            latitude = pos.coords.latitude;
            longitude = pos.coords.longitude;
            accuracy = pos.coords.accuracy ?? null;
            console.log(
              "[QuakeGuard] GPS fix →",
              latitude,
              longitude,
              "±",
              accuracy,
              "m",
            );
          } else {
            locationError = "permission_denied";
          }
        }
      } catch (e: any) {
        locationError = e?.message ?? "location_error";
        console.log("[QuakeGuard] location error:", locationError);
      }
    }

    // Gather battery
    let batteryLevel: number | null = null;
    let batteryState: string | null = null;
    try {
      batteryLevel = await Battery.getBatteryLevelAsync();
      const s = await Battery.getBatteryStateAsync();
      batteryState =
        s === Battery.BatteryState.CHARGING
          ? "charging"
          : s === Battery.BatteryState.FULL
            ? "full"
            : s === Battery.BatteryState.UNPLUGGED
              ? "unplugged"
              : "unknown";
    } catch {
      // ignore
    }

    try {
      // #206 (Batch 7): a REHEARSAL or test run must never post a real
      // status to the backend. Doing so puts a live "trapped" row on
      // dispatch, which the re-check sweeper then wakes up on every
      // schedule tick — the user gets real "Are you still OK?" pushes
      // every few minutes because they were curious what the button did.
      //
      // Under isTestRun we simulate a successful post: same UI
      // transitions, no network call, no post-submission side effects
      // (rescue-info lock-screen card, cancel-rescue clear). The
      // rehearsal exists to prove the buttons work; the moment we get
      // to "here's what the button does after you tap it" the practice
      // run is complete.
      let res: { ok: boolean; status: number };
      if (isTestRun) {
        console.log(
          `[QuakeGuard] TEST RUN — skipping real ${kind} post to backend`,
        );
        res = { ok: true, status: 200 };
      } else {
        res = await postStatus({
          status: kind === "safe" ? "safe" : "trapped",
          severity: kind === "trapped" ? severity : null,
          mobility: kind === "trapped" ? mobility : null,
          egress: kind === "trapped" ? egress : null,
          location: { latitude, longitude, accuracy, error: locationError },
          battery: { level: batteryLevel, state: batteryState },
        });
      }
      console.log(
        `[QuakeGuard] ${kind}${severity ? "/" + severity : ""}${mobility ? "/" + mobility : ""} → response status:`,
        res.status,
      );
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      setStatus("sent");

      // #208 R4 (Batch 7): the alert has now been answered. Clear the
      // "unanswered alert" marker so the home screen stops redirecting
      // to /alert. Answering is the ONLY thing that clears it —
      // swiping the notification, restarting the phone, the siren
      // timing out — none of those clear it. Rule 9.2, silence is
      // information, not an answer.
      if (!isTestRun) {
        clearActiveAlert().catch(() => {});
      }

      // Post-submission side effects: manage the persistent rescue-info
      // lock-screen card. This is what lets a responder pick up an
      // unconscious victim's locked phone, read the short code + name off
      // the lock screen, and match it to a pin on the dashboard — the
      // whole point of this feature.
      //
      // #206 (Batch 7): the rescue-info card is a REAL lock-screen
      // notification. During a test run we skip posting it entirely, so
      // a rehearsal never leaves a fake "help is coming" card on the
      // user's own lock screen.
      if (kind === "trapped" && !isTestRun) {
        // Fire-and-forget — do NOT block the "sent" UI transition on the
        // notification API, which occasionally takes a beat on cold-start.
        (async () => {
          try {
            const [code, name] = await Promise.all([
              getShortCode(),
              getDisplayName(),
            ]);
            await postRescueInfoNotification(code, name);
          } catch (err) {
            console.log(
              "[QuakeAngel] rescue info notification failed:",
              (err as Error)?.message,
            );
          }
        })();
      } else if (kind === "safe") {
        // Safe = no longer trapped → clear any stale rescue card from a
        // previous trapped submission in the same session.
        cancelRescueInfoNotification().catch(() => {});
      }
    } catch (e: any) {
      console.log("[QuakeGuard] check-in error:", e?.message);
      setErrorMsg(e?.message ?? "Network error");
      setStatus("error");
    }
  };

  const handleImSafe = () => submitCheckIn("safe");

  const openTriage = () => {
    if (status === "sending" || status === "sent") return;
    // Silence the siren the moment the user commits to opening triage — they
    // are actively responding, so the alarm has served its purpose.
    stopSiren();
    cancelCheckInReminders().catch(() => {});
    Haptics.impactAsync(Haptics.ImpactFeedbackStyle.Medium).catch(() => {});
    setTriageOpen(true);
  };

  const chooseTriage = (severity: TriageSeverity) => {
    // Severity chosen. The mobility follow-up is only genuinely useful for
    // YELLOW: green's label already says "I can walk / not badly hurt" and
    // red's says "can't move" — asking again would just be extra taps in
    // an emergency. So for green/red we short-circuit straight to
    // submission with the mobility inferred from the severity choice.
    //
    // stopSiren() is called defensively at every step of the trapped flow
    // — the kill-switch effect will also catch any resurrection, but
    // calling it here means we don't rely on that safety net firing in
    // time.
    stopSiren();
    setTriageOpen(false);

    if (severity === "yellow") {
      // Only yellow needs the follow-up — mobility is genuinely ambiguous.
      setPendingSeverity(severity);
      setMobilityOpen(true);
      Haptics.impactAsync(Haptics.ImpactFeedbackStyle.Light).catch(() => {});
      return;
    }

    if (severity === "green") {
      // Green has just told us they can walk, so mobility is settled — but
      // egress is not, and it is the only thing that decides whether a team
      // with cutting gear is needed. One extra tap, asked of the group most
      // able to give it. NOT extended to red: red already implies immobility
      // and gets maximum response anyway. Yellow keeps its mobility question.
      setPendingSeverity(severity);
      setEgressOpen(true);
      Haptics.impactAsync(Haptics.ImpactFeedbackStyle.Light).catch(() => {});
      return;
    }

    // Red → seriously injured / can't move → mobility is "trapped".
    submitCheckIn("trapped", severity, "trapped");
  };

  const chooseMobility = (mobility: Mobility) => {
    // Defensive: silence again immediately before we start the async
    // submission chain (GPS → battery → network) so even a multi-second
    // network stall cannot leave the siren audible.
    stopSiren();
    setMobilityOpen(false);
    const sev = pendingSeverity;
    setPendingSeverity(null);
    submitCheckIn("trapped", sev, mobility);
  };

  const chooseEgress = (egress: Egress) => {
    stopSiren();
    setEgressOpen(false);
    const sev = pendingSeverity;
    setPendingSeverity(null);
    submitCheckIn("trapped", sev, "mobile", egress);
  };

  // Back arrow inside the mobility sheet: reopen severity picker so the
  // user can re-answer without losing their place in the flow.
  const backToSeverity = () => {
    stopSiren();
    setMobilityOpen(false);
    setEgressOpen(false);
    setPendingSeverity(null);
    setTriageOpen(true);
  };

  const mm = String(Math.floor(elapsed / 60)).padStart(2, "0");
  const ss = String(elapsed % 60).padStart(2, "0");

  return (
    <View style={styles.root} testID="alert-screen">
      <StatusBar style="light" />

      {/* Red gradient background */}
      <LinearGradient
        colors={["#3B0A08", "#7A0E10", "#3B0A08", "#0F1115"]}
        locations={[0, 0.35, 0.7, 1]}
        style={StyleSheet.absoluteFill}
      />

      <SafeAreaView edges={["top", "bottom"]} style={styles.content}>
        {/* Top banner */}
        <View style={styles.topBanner}>
          <View style={styles.liveDot} />
          <Text style={styles.liveText}>LIVE ALERT · {mm}:{ss}</Text>
        </View>

        {/* Aftershock notice. Deliberately a NOTICE, not a navigation: the
            user's answer — submitted or half-given — is untouched. */}
        {aftershock && (
          <View style={styles.aftershockBar} testID="aftershock-bar">
            <Ionicons name="pulse" size={16} color="#FFD79A" />
            <Text style={styles.aftershockText}>
              {aftershockMagnitude
                ? `Another alert just arrived — M${aftershockMagnitude}. `
                : "Another alert just arrived. "}
              {status === "sent"
                ? "Your report already reached the rescue team."
                : "Your answer below still applies."}
            </Text>
            {status === "sent" && (
              <Pressable
                onPress={() => {
                  // Explicit, user-initiated. We never silently reset someone
                  // who has already answered.
                  setStatus("idle");
                  setChosenSeverity(null);
                  setChosenMobility(null);
                  setAftershock(null);
                }}
                hitSlop={10}
                style={styles.aftershockAction}
                testID="aftershock-update-btn"
              >
                <Text style={styles.aftershockActionText}>Update</Text>
              </Pressable>
            )}
          </View>
        )}

        {/* Center: pulsing warning.
            LAYOUT CONTRACT (batch 5, B2): this block is the ONLY shrinkable
            region on the screen. The metrics strip and the action buttons are
            siblings with flexShrink:0, so on a short iPhone the graphic and
            headline give up space instead of the data strip sliding underneath
            the I'M SAFE button (the bug: only the top few pixels of each
            number were visible). `overflow: hidden` is the belt-and-braces
            guarantee that nothing from here can ever paint over the strip. */}
        <View style={styles.center}>
          <View style={[styles.pulseWrap, compact && styles.pulseWrapCompact]}>
            <Animated.View style={[styles.pulseRing, ringStyle]} />
            <Animated.View style={[styles.pulseRing, styles.pulseRingInner, ringStyle]} />
            <Animated.View
              style={[styles.iconBubble, compact && styles.iconBubbleCompact, iconStyle]}
            >
              <Ionicons
                name="warning"
                size={compact ? 48 : 72}
                color={colors.onBrandPrimary}
              />
            </Animated.View>
          </View>

          <Text style={[styles.heading, compact && styles.headingCompact]}>
            EARTHQUAKE{"\n"}DETECTED
          </Text>
          {/* #253 (Batch 7 R4): the two-sentence safety instruction was
              rendered with a hard `\n` inside a single <Text>, and when
              the aftershock banner pushed the layout down "stops" got
              clipped — turning "move to open space WHEN SHAKING STOPS"
              into "move to open space WHEN SHAKING". That is the
              opposite of correct earthquake guidance on the one screen
              where clipping a word can hurt somebody.

              Root-cause fix (Paul's rule: fix the layout, not the
              wording). TWO invariants now hold on this block:

              1. Each sentence renders as its OWN <Text> and both are
                 wrapped in a `flexShrink: 0` container — the container
                 must give up other space (icon pulse, spacing) before
                 the safety text is compressed.
              2. `adjustsFontSizeToFit={true}` with `numberOfLines={2}`
                 on each sentence — if the phone is truly tiny or the
                 system text size is enormous, the FONT scales down;
                 the sentence NEVER truncates.

              Layout rule for the whole app: safety-critical text uses
              `adjustsFontSizeToFit`, never a bare `numberOfLines` that
              can cut a word off. See #253 sweep list below in this
              file's comment. */}
          <View style={styles.safetyInstruction}>
            <Text
              style={[styles.subheading, compact && styles.subheadingCompact]}
              numberOfLines={1}
              adjustsFontSizeToFit
              minimumFontScale={0.7}
              accessibilityRole="text"
            >
              Drop. Cover. Hold on.
            </Text>
            <Text
              style={[styles.subheading, compact && styles.subheadingCompact, styles.safetyInstructionSecond]}
              numberOfLines={2}
              adjustsFontSizeToFit
              minimumFontScale={0.7}
              accessibilityRole="text"
            >
              Move to open space when shaking stops.
            </Text>
          </View>
        </View>

        {/* Data strip — own row, never overlapped by the buttons below. */}
        <View style={styles.metricsRow}>
          <View style={styles.metric}>
            <Text style={[styles.metricLabel, compact && styles.metricLabelCompact]}>
              MAGNITUDE
            </Text>
            <Text style={styles.metricValue} numberOfLines={1} adjustsFontSizeToFit>
              {eventReadings.magnitude ?? "—"}
            </Text>
          </View>
          <View style={styles.metricDivider} />
          <View style={styles.metric}>
            <Text style={[styles.metricLabel, compact && styles.metricLabelCompact]}>
              DISTANCE
            </Text>
            <Text style={styles.metricValue} numberOfLines={1} adjustsFontSizeToFit>
              {eventReadings.distance_km != null ? (
                <>{eventReadings.distance_km}<Text style={styles.metricUnit}>km</Text></>
              ) : "—"}
            </Text>
          </View>
          <View style={styles.metricDivider} />
          <View style={styles.metric}>
            <Text style={[styles.metricLabel, compact && styles.metricLabelCompact]}>
              INTENSITY
            </Text>
            <Text style={styles.metricValue} numberOfLines={1} adjustsFontSizeToFit>
              {eventReadings.intensity ?? "—"}
            </Text>
          </View>
        </View>

        {/* Bottom action */}
        <View style={[styles.bottomWrap, { paddingBottom: Math.max(insets.bottom, spacing.md) }]}>
          {status === "error" && errorMsg && (
            <View style={styles.errorToast} testID="alert-error-toast">
              <Ionicons name="alert-circle" size={22} color={colors.warning} />
              <Text style={styles.errorText}>{errorMsg}. Tap again to retry.</Text>
            </View>
          )}
          {status === "sent" && outcome === "safe" && (
            <View style={styles.successToast} testID="alert-success-toast">
              <Ionicons name="checkmark-circle" size={22} color={colors.onSuccess} />
              <Text style={styles.successText}>Report received. Stay safe.</Text>
            </View>
          )}
          {status === "sent" && outcome === "trapped" && (
            <View
              style={[styles.trappedToast, severityToastStyle(chosenSeverity)]}
              testID="alert-trapped-toast"
            >
              <Ionicons name="megaphone" size={28} color="#fff" />
              <View style={{ flex: 1 }}>
                <Text style={styles.trappedToastText}>
                  Rescuers alerted. Stay calm. Conserve battery.
                </Text>
                {chosenEgress ? (
                  <Text style={styles.trappedToastMeta} testID="trapped-egress-summary">
                    Reported:{" "}
                    {chosenEgress === "can_exit"
                      ? "you can get out on your own"
                      : "you cannot get out — extraction needed"}
                  </Text>
                ) : chosenMobility ? (
                  <Text style={styles.trappedToastMeta} testID="trapped-mobility-summary">
                    Reported:{" "}
                    {chosenMobility === "mobile"
                      ? "you can move"
                      : "you are trapped/pinned"}
                  </Text>
                ) : null}
              </View>
            </View>
          )}

          {/* #199/#202 (R4 companion): stand-down / incident-closed
              overrides the check-in buttons. The user sees a plain
              message saying the alert has been called off, plus a
              single home button. Layered ABOVE the buttons rather
              than removing them, so the button layout doesn't jump
              around; the buttons still exist but they render below
              this panel. */}
          {stoodDown && (
            <View
              accessibilityRole="alert"
              accessibilityLabel="This alert has been stood down. It was a false alarm."
              style={styles.stoodDownPanel}
            >
              <Ionicons name="checkmark-circle" size={40} color="#4EE0A5" />
              <Text style={styles.stoodDownTitle}>Alert called off.</Text>
              <Text style={styles.stoodDownBody}>
                {stoodDown.reason === "incident_closed"
                  ? "The incident has been closed. You don't need to check in."
                  : "This turned out to be a false alarm. You don't need to check in."}
              </Text>
              <Pressable
                onPress={() => router.replace("/" as any)}
                style={({ pressed }) => [
                  styles.stoodDownHomeBtn,
                  pressed && { opacity: 0.9 },
                ]}
                testID="stood-down-home-btn"
              >
                <Text style={styles.stoodDownHomeBtnText}>Back to home</Text>
              </Pressable>
            </View>
          )}

          {/* Primary: I'M SAFE (green) — hidden once a trapped report was sent */}
          {!(status === "sent" && outcome === "trapped") && !stoodDown && (
            <Pressable
              onPress={handleImSafe}
              disabled={status === "sending" || status === "sent"}
              style={({ pressed }) => [
                styles.safeBtn,
                (status === "sent") && styles.safeBtnDone,
                pressed && { opacity: 0.9, transform: [{ scale: 0.98 }] },
              ]}
              testID="im-safe-btn"
            >
              <Ionicons
                name={
                  status === "sent" && outcome === "safe"
                    ? "checkmark"
                    : "shield-checkmark"
                }
                size={26}
                color={colors.onSuccess}
              />
              <Text style={styles.safeBtnText}>
                {status === "sending" && outcome === "safe"
                  ? "SENDING…"
                  : status === "sent" && outcome === "safe"
                    ? "MARKED SAFE"
                    : "I'M SAFE"}
              </Text>
            </Pressable>
          )}

          {/* Secondary: I NEED HELP (amber) — opens triage sheet.
              Reworded from "I'M TRAPPED / NEED HELP" on 2026-06-18. The button
              was doing double duty: worded as "I'm trapped" but functioning as
              "I'm not safe", so it contradicted the very next screen, whose
              first option is "I can walk and I'm not badly hurt". If you can
              walk you are not trapped. Rewording fixes the contradiction with
              no extra screens and no extra taps — splitting injury and
              entrapment into two questions for everyone was considered and
              rejected, because it adds a tap for the person least able to make
              one, and the dashboard records both facts separately anyway. */}
          {status !== "sent" && !stoodDown && (
            <Pressable
              onPress={openTriage}
              disabled={status === "sending"}
              style={({ pressed }) => [
                styles.trappedBtn,
                pressed && { opacity: 0.9, transform: [{ scale: 0.98 }] },
              ]}
              testID="im-trapped-btn"
            >
              <Ionicons name="warning" size={22} color="#fff" />
              <Text style={styles.trappedBtnText}>
                {status === "sending" && outcome === "trapped"
                  ? "SENDING…"
                  : "I NEED HELP"}
              </Text>
            </Pressable>
          )}

          {/* #250 (Batch 7 D1): what happens if you don't answer? Users
              wondered whether silence would mark them as trapped and
              scramble a rescue. It does NOT — the correct default is
              "not responding", not "trapped". Written out here so the
              behaviour matches the wording on the dashboard exactly. */}
          {status !== "sent" && !stoodDown && (
            <Text style={styles.unansweredNote} testID="alert-unanswered-note">
              If you don&apos;t answer, we mark you as{" "}
              <Text style={styles.unansweredNoteBold}>not responding</Text>
              {" "}— never as trapped. The siren stops when you tap I&apos;m
              safe or I need help, when someone else calls off the alert, or
              about a minute after it started if neither has happened.
            </Text>
          )}

          {/* Task #14: the "Dismiss alert" escape hatch is gone — an
              unanswered alert must not be dismissable, because a dismissal
              looks identical to silence on the dashboard. The only way off
              this screen is to answer (I'M SAFE / I NEED HELP). After a
              trapped report is confirmed, a plain "Back to home" remains. */}
          {status === "sent" && outcome === "trapped" && (
            <Pressable
              onPress={() => {
                stopSiren();
                if (router.canGoBack()) {
                  router.back();
                } else {
                  router.replace("/");
                }
              }}
              style={styles.dismissBtn}
              hitSlop={12}
              testID="alert-back-home-btn"
            >
              <Text style={styles.dismissText}>Back to home</Text>
            </Pressable>
          )}
        </View>

        {/* Triage sheet — plain-language mapping to START triage colours.
            Never offers a "black/deceased" option; that determination is
            reserved for human first responders on scene. */}
        <Modal
          visible={triageOpen}
          animationType="slide"
          transparent
          onRequestClose={() => setTriageOpen(false)}
        >
          <View style={styles.triageBackdrop}>
            <View
              style={[
                styles.triageSheet,
                { paddingBottom: Math.max(insets.bottom + spacing.md, spacing.xl) },
              ]}
            >
              <View style={styles.triageHandle} />
              <Text style={styles.triageTitle}>How badly are you hurt?</Text>
              <Text style={styles.triageSubtitle}>
                Rescuers will prioritise responses based on your answer.
              </Text>

              <TriageOption
                color="#2E7D32"
                label="I can walk and I&apos;m not badly hurt"
                sublabel="Minor injuries · walking wounded"
                icon="walk"
                onPress={() => chooseTriage("green")}
                testID="triage-green"
              />
              <TriageOption
                color="#EA9500"
                label="I&apos;m hurt but stable, waiting for help"
                sublabel="Serious — Stable · not immediately life-threatening"
                icon="medkit"
                onPress={() => chooseTriage("yellow")}
                testID="triage-yellow"
              />
              <TriageOption
                color="#C21818"
                label="I&apos;m seriously injured / can&apos;t move"
                sublabel="Immediate · life-threatening"
                icon="pulse"
                onPress={() => chooseTriage("red")}
                testID="triage-red"
              />

              <Pressable
                onPress={() => setTriageOpen(false)}
                style={styles.triageCancel}
                hitSlop={8}
                testID="triage-cancel"
              >
                <Text style={styles.triageCancelText}>Back</Text>
              </Pressable>
            </View>
          </View>
        </Modal>

        {/* Mobility follow-up — shown after severity. Captures whether the
            user can move themselves out of danger or is pinned/trapped
            (e.g. under debris). Value flows into postStatus as
            `mobility: 'mobile' | 'trapped'` for the rescuer dashboard. */}
        <Modal
          visible={mobilityOpen}
          animationType="slide"
          transparent
          onRequestClose={backToSeverity}
        >
          <View style={styles.triageBackdrop}>
            <View
              style={[
                styles.triageSheet,
                { paddingBottom: Math.max(insets.bottom + spacing.md, spacing.xl) },
              ]}
            >
              <View style={styles.triageHandle} />
              <Text style={styles.triageTitle}>Can you move?</Text>
              <Text style={styles.triageSubtitle}>
                Tell rescuers whether you&apos;re free to move or physically
                pinned. This does not delay your report.
              </Text>

              <TriageOption
                color="#2E7D32"
                label="Yes, I can move"
                sublabel="I can walk or crawl to a safer spot"
                icon="walk"
                onPress={() => chooseMobility("mobile")}
                testID="mobility-mobile"
              />
              <TriageOption
                color="#C21818"
                label="No, I&apos;m trapped/pinned"
                sublabel="Stuck under debris or unable to move"
                icon="alert-circle"
                onPress={() => chooseMobility("trapped")}
                testID="mobility-trapped"
              />

              <Pressable
                onPress={backToSeverity}
                style={styles.triageCancel}
                hitSlop={8}
                testID="mobility-back"
              >
                <Text style={styles.triageCancelText}>Back</Text>
              </Pressable>
            </View>
          </View>
        </Modal>

        {/* GREEN-only egress follow-up (2026-06-18). Mobility describes the
            body; egress describes the building. Someone can be fully mobile
            and still unable to leave — jammed door, beam pinning a limb
            without injuring it, collapsed stairwell, blocked basement — and
            only egress decides whether a team with cutting gear is needed.
            A "no" surfaces them as needing extraction despite minor injury. */}
        <Modal
          visible={egressOpen}
          animationType="slide"
          transparent
          onRequestClose={backToSeverity}
        >
          <View style={styles.triageBackdrop}>
            <View
              style={[
                styles.triageSheet,
                { paddingBottom: Math.max(insets.bottom + spacing.md, spacing.xl) },
              ]}
            >
              <View style={styles.triageHandle} />
              <Text style={styles.triageTitle}>Can you get out on your own?</Text>
              <Text style={styles.triageSubtitle}>
                Not about your injuries — about the building. A jammed door or
                blocked stairwell counts as no. This does not delay your report.
              </Text>

              <TriageOption
                color="#2E7D32"
                label="Yes, I can get out"
                sublabel="Nothing is blocking my way out"
                icon="exit"
                onPress={() => chooseEgress("can_exit")}
                testID="egress-can-exit"
              />
              <TriageOption
                color="#C21818"
                label="No, I can&apos;t get out"
                sublabel="Blocked, jammed or pinned — a team will be needed"
                icon="lock-closed"
                onPress={() => chooseEgress("cannot_exit")}
                testID="egress-cannot-exit"
              />

              <Pressable
                onPress={backToSeverity}
                style={styles.triageCancel}
                hitSlop={8}
                testID="egress-back"
              >
                <Text style={styles.triageCancelText}>Back</Text>
              </Pressable>
            </View>
          </View>
        </Modal>
      </SafeAreaView>
    </View>
  );
}

/* ---------- Triage sheet row ---------- */

function TriageOption({
  color,
  label,
  sublabel,
  icon,
  onPress,
  testID,
}: {
  color: string;
  label: string;
  sublabel: string;
  icon: React.ComponentProps<typeof Ionicons>["name"];
  onPress: () => void;
  testID?: string;
}) {
  return (
    <Pressable
      onPress={onPress}
      style={({ pressed }) => [
        styles.triageOption,
        { backgroundColor: color },
        pressed && { opacity: 0.9, transform: [{ scale: 0.99 }] },
      ]}
      testID={testID}
      hitSlop={4}
    >
      <View style={styles.triageIconWrap}>
        <Ionicons name={icon} size={30} color="#fff" />
      </View>
      <View style={{ flex: 1 }}>
        <Text style={styles.triageOptionLabel}>{label}</Text>
        <Text style={styles.triageOptionSublabel}>{sublabel}</Text>
      </View>
      <Ionicons name="chevron-forward" size={22} color="rgba(255,255,255,0.85)" />
    </Pressable>
  );
}

function severityToastStyle(sev: TriageSeverity | null) {
  switch (sev) {
    case "green":
      return { backgroundColor: "#2E7D32", borderColor: "#1F5A24" };
    case "yellow":
      return { backgroundColor: "#EA9500", borderColor: "#B77400" };
    case "red":
    default:
      return { backgroundColor: "#C21818", borderColor: "#8E1010" };
  }
}

const styles = StyleSheet.create({
  root: {
    flex: 1,
    backgroundColor: "#3B0A08",
  },
  content: {
    flex: 1,
    paddingHorizontal: spacing.xl,
  },
  aftershockBar: {
    flexDirection: "row",
    alignItems: "center",
    gap: spacing.sm,
    marginTop: spacing.sm,
    paddingVertical: spacing.sm,
    paddingHorizontal: spacing.md,
    borderRadius: radius.md,
    backgroundColor: "rgba(255,176,32,0.16)",
    borderWidth: 1,
    borderColor: "rgba(255,176,32,0.45)",
    flexShrink: 0,
  },
  aftershockText: {
    flex: 1,
    color: "#FFD79A",
    fontSize: 13,
    lineHeight: 18,
    fontWeight: "600",
  },
  aftershockAction: {
    minHeight: 32,
    paddingHorizontal: spacing.md,
    justifyContent: "center",
    borderRadius: radius.sm,
    backgroundColor: "rgba(255,176,32,0.25)",
  },
  aftershockActionText: {
    color: "#FFE7C2",
    fontSize: 13,
    fontWeight: "800",
  },
  topBanner: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "center",
    gap: spacing.sm,
    paddingTop: spacing.md,
  },
  liveDot: {
    width: 10,
    height: 10,
    borderRadius: 5,
    backgroundColor: colors.brandPrimary,
    shadowColor: colors.brandPrimary,
    shadowOpacity: 1,
    shadowRadius: 10,
  },
  liveText: {
    color: colors.onSurface,
    fontSize: 12,
    letterSpacing: 3,
    fontWeight: "800",
  },
  center: {
    flex: 1,
    flexShrink: 1,
    minHeight: 0,
    overflow: "hidden",
    alignItems: "center",
    justifyContent: "center",
  },
  pulseWrap: {
    width: 220,
    height: 220,
    alignItems: "center",
    justifyContent: "center",
    marginBottom: spacing.xl,
  },
  pulseWrapCompact: {
    width: 150,
    height: 150,
    marginBottom: spacing.md,
  },
  pulseRing: {
    position: "absolute",
    width: 160,
    height: 160,
    borderRadius: 80,
    borderWidth: 2,
    borderColor: colors.brandPrimary,
  },
  pulseRingInner: {
    width: 200,
    height: 200,
    borderRadius: 100,
    borderColor: "rgba(255,59,48,0.5)",
  },
  iconBubble: {
    width: 140,
    height: 140,
    borderRadius: 70,
    backgroundColor: colors.brandPrimary,
    alignItems: "center",
    justifyContent: "center",
    shadowColor: colors.brandPrimary,
    shadowOpacity: 0.7,
    shadowRadius: 30,
    shadowOffset: { width: 0, height: 0 },
    elevation: 20,
  },
  iconBubbleCompact: {
    width: 96,
    height: 96,
    borderRadius: 48,
  },
  heading: {
    color: colors.onSurface,
    fontSize: 40,
    fontWeight: "900",
    letterSpacing: 3,
    textAlign: "center",
    lineHeight: 44,
  },
  headingCompact: {
    fontSize: 30,
    lineHeight: 34,
    letterSpacing: 2,
  },
  subheading: {
    marginTop: spacing.md,
    color: "rgba(255,255,255,0.9)",
    fontSize: 19,
    fontWeight: "500",
    textAlign: "center",
    lineHeight: 26,
  },
  subheadingCompact: {
    marginTop: spacing.sm,
    fontSize: 16,
    lineHeight: 22,
  },
  // #253 (Batch 7): safety-instruction block. `flexShrink: 0` means the
  // aftershock banner or any other element pushing on this layout MUST
  // take space from the pulse animation / spacing above, never from
  // these two sentences.
  safetyInstruction: {
    flexShrink: 0,
    alignSelf: "stretch",
    paddingHorizontal: spacing.md,
  },
  safetyInstructionSecond: {
    marginTop: 2,
  },
  metricsRow: {
    flexShrink: 0,
    marginTop: spacing.md,
    marginBottom: spacing.md,
    flexDirection: "row",
    alignItems: "center",
    backgroundColor: "rgba(0,0,0,0.35)",
    borderRadius: radius.lg,
    borderWidth: 1,
    borderColor: "rgba(255,255,255,0.1)",
    paddingVertical: spacing.md,
    paddingHorizontal: spacing.lg,
  },
  metric: {
    flex: 1,
    alignItems: "center",
    paddingHorizontal: spacing.xs,
  },
  metricLabel: {
    color: "rgba(255,255,255,0.65)",
    fontSize: 12,
    letterSpacing: 1.5,
    fontWeight: "700",
    marginBottom: 4,
  },
  metricLabelCompact: {
    fontSize: 10,
    letterSpacing: 0.3,
  },
  metricValue: {
    color: colors.onSurface,
    fontSize: 28,
    fontWeight: "900",
    letterSpacing: 1,
  },
  metricUnit: {
    fontSize: 14,
    fontWeight: "600",
    color: "rgba(255,255,255,0.7)",
  },
  metricDivider: {
    width: 1,
    height: 32,
    backgroundColor: "rgba(255,255,255,0.15)",
  },
  bottomWrap: {
    flexShrink: 0,
    gap: spacing.md,
  },
  errorToast: {
    flexDirection: "row",
    alignItems: "center",
    gap: spacing.sm,
    backgroundColor: "rgba(0,0,0,0.5)",
    borderColor: colors.warning,
    borderWidth: 1,
    padding: spacing.md,
    borderRadius: radius.md,
  },
  errorText: {
    flex: 1,
    color: colors.warning,
    fontSize: 17,
    fontWeight: "700",
    lineHeight: 22,
  },
  successToast: {
    flexDirection: "row",
    alignItems: "center",
    gap: spacing.sm,
    backgroundColor: colors.success,
    paddingHorizontal: spacing.md,
    paddingVertical: spacing.md + 2,
    borderRadius: radius.md,
  },
  successText: {
    flex: 1,
    color: colors.onSuccess,
    fontSize: 18,
    fontWeight: "800",
    lineHeight: 24,
  },
  // #199/#202 (Batch 7 R4 companion): stood-down panel styling. High
  // contrast with clear success colour so the "you can stop" message
  // reads as calm rather than as another alert.
  stoodDownPanel: {
    marginBottom: spacing.lg,
    padding: spacing.lg,
    borderRadius: radius.lg,
    backgroundColor: "#0F2A1E",
    borderWidth: 1,
    borderColor: "#2A6F52",
    alignItems: "center",
  },
  stoodDownTitle: {
    marginTop: spacing.sm,
    color: "#EAF7F0",
    fontSize: 20,
    fontWeight: "800",
    letterSpacing: 0.3,
  },
  stoodDownBody: {
    marginTop: 6,
    color: "#B9D9C9",
    fontSize: 15,
    lineHeight: 21,
    textAlign: "center",
  },
  stoodDownHomeBtn: {
    marginTop: spacing.md,
    paddingVertical: 14,
    paddingHorizontal: 28,
    borderRadius: 12,
    backgroundColor: "#4EE0A5",
    minHeight: 48,
    minWidth: 200,
    alignItems: "center",
    justifyContent: "center",
  },
  stoodDownHomeBtnText: {
    color: "#0B1F16",
    fontSize: 16,
    fontWeight: "800",
    letterSpacing: 0.3,
  },

  safeBtn: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "center",
    gap: spacing.md,
    height: 72,
    borderRadius: radius.lg,
    backgroundColor: colors.success,
    shadowColor: colors.success,
    shadowOpacity: 0.5,
    shadowRadius: 20,
    shadowOffset: { width: 0, height: 4 },
    elevation: 10,
  },
  safeBtnDone: {
    backgroundColor: "#1F8A3A",
  },
  safeBtnText: {
    color: colors.onSuccess,
    fontSize: 22,
    fontWeight: "900",
    letterSpacing: 3,
  },
  dismissBtn: {
    alignItems: "center",
    paddingVertical: spacing.md,
  },
  dismissText: {
    color: "rgba(255,255,255,0.7)",
    fontSize: 15,
    fontWeight: "700",
    letterSpacing: 1,
  },
  // #250 (Batch 7 D1): "if you don't answer" note. Small enough not
  // to compete with the buttons, prominent enough to read at a
  // glance while the siren is going.
  unansweredNote: {
    color: "rgba(255,255,255,0.82)",
    fontSize: 13,
    lineHeight: 18,
    textAlign: "center",
    marginTop: spacing.md,
    marginHorizontal: spacing.md,
  },
  unansweredNoteBold: {
    fontWeight: "800",
    color: "#FFFFFF",
  },

  /* Trapped / triage — secondary CTA on the alert screen */
  trappedBtn: {
    marginTop: spacing.sm,
    borderRadius: radius.lg,
    paddingVertical: spacing.md + 4,
    paddingHorizontal: spacing.lg,
    minHeight: 72,
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "center",
    gap: 12,
    backgroundColor: "#EA9500",
    borderWidth: 1.5,
    borderColor: "#B77400",
  },
  trappedBtnText: {
    color: "#fff",
    fontSize: 20,
    fontWeight: "900",
    letterSpacing: 1.5,
    flexShrink: 1,
    textAlign: "center",
  },
  trappedToast: {
    flexDirection: "row",
    alignItems: "center",
    gap: spacing.sm + 2,
    paddingHorizontal: spacing.lg,
    paddingVertical: spacing.lg,
    borderRadius: radius.lg,
    borderWidth: 1.5,
    marginBottom: spacing.md,
  },
  trappedToastText: {
    color: "#fff",
    fontSize: 26,
    fontWeight: "800",
    lineHeight: 33,
    flex: 1,
  },
  trappedToastMeta: {
    color: "rgba(255,255,255,0.92)",
    fontSize: 15,
    fontWeight: "600",
    lineHeight: 20,
    marginTop: 6,
  },

  /* Triage modal */
  triageBackdrop: {
    flex: 1,
    backgroundColor: "rgba(0,0,0,0.6)",
    justifyContent: "flex-end",
  },
  triageSheet: {
    backgroundColor: "#1a0d0d",
    borderTopLeftRadius: 20,
    borderTopRightRadius: 20,
    paddingTop: spacing.sm,
    paddingHorizontal: spacing.lg,
  },
  triageHandle: {
    alignSelf: "center",
    width: 44,
    height: 4,
    borderRadius: 2,
    backgroundColor: "rgba(255,255,255,0.35)",
    marginBottom: spacing.md,
  },
  triageTitle: {
    color: "#fff",
    fontSize: 26,
    fontWeight: "800",
    marginBottom: 6,
  },
  triageSubtitle: {
    color: "rgba(255,255,255,0.75)",
    fontSize: 16,
    lineHeight: 22,
    marginBottom: spacing.lg,
  },
  triageOption: {
    flexDirection: "row",
    alignItems: "center",
    gap: 14,
    borderRadius: radius.lg,
    paddingHorizontal: spacing.md,
    paddingVertical: spacing.md + 4,
    marginBottom: spacing.sm,
    minHeight: 84,
  },
  triageIconWrap: {
    width: 44,
    height: 44,
    borderRadius: 22,
    backgroundColor: "rgba(255,255,255,0.18)",
    alignItems: "center",
    justifyContent: "center",
  },
  triageOptionLabel: {
    color: "#fff",
    fontSize: 20,
    fontWeight: "800",
    lineHeight: 26,
  },
  triageOptionSublabel: {
    color: "rgba(255,255,255,0.92)",
    fontSize: 18,
    marginTop: 4,
    lineHeight: 24,
  },
  triageCancel: {
    alignItems: "center",
    paddingVertical: spacing.md,
    marginTop: 4,
  },
  triageCancelText: {
    color: "rgba(255,255,255,0.75)",
    fontSize: 16,
    fontWeight: "700",
    letterSpacing: 1,
  },
});
