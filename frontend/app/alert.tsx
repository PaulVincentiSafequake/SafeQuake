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
  submitStatus,
  type Egress,
  type GroupSize,
  type Mobility,
  type TriageSeverity,
} from "@/src/utils/checkin";
import { subscribe as subscribeHelpQueue, kickFlush, type QueueItem } from "@/src/utils/helpQueue";
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
import { recordHeardOnWrist } from "@/src/utils/watchReminder";
import { resolveEventReadings, roundDistanceKm } from "@/src/utils/eventReadings";

const SIREN_SOURCE = require("../assets/audio/siren.mp3");



type Status = "idle" | "sending" | "pending_retry" | "sent" | "error";
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
    // #271: an operator pressed "Ask them to check in". Same screen, same
    // buttons, same submit path as a real alert — but nothing on it may
    // suggest a new earthquake has happened, because none has.
    checkin?: string;
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
  // #271 (2026-08-21 — Paul): "Someone tapping that notification is
  // anxious. The screen they land on must say plainly, before anything
  // else: no new earthquake." So this screen keeps every one of its
  // check-in controls — I'M SAFE, I NEED HELP, the triage sheets, the
  // same submit — and replaces ONLY the earthquake framing: no red, no
  // EARTHQUAKE DETECTED, no Drop-Cover-Hold-on, no magnitude strip.
  // A help report from here is a real report and lands on the working
  // board exactly as one made during an alert (same submitCheckIn call).
  const isCheckInRequest = params.checkin === "1";
  // #286: only used on a practice run — where the siren actually came out.
  const [wristAnswer, setWristAnswer] = useState<null | "phone" | "wrist">(null);
  const insets = useSafeAreaInsets();
  // Short-screen mode (batch 5, B2). iPhone SE/mini class devices can't fit
  // the 220pt pulse graphic + 40pt headline + data strip + two large action
  // buttons. Below this height the graphic and headline shrink; the data
  // strip and the buttons never do.
  const { height: windowHeight } = useWindowDimensions();
  const compact = windowHeight < 760;
  const [status, setStatus] = useState<Status>("idle");
  const [outcome, setOutcome] = useState<OutcomeKind>("safe");
  // Number of failed delivery attempts for the current pending report.
  // Rendered honestly in the "still trying" banner so the user can see
  // the app really is retrying and hasn't quietly given up.
  const [pendingAttempts, setPendingAttempts] = useState(0);
  // ID of the queue item this screen is currently tracking. Used by the
  // helpQueue subscription to know which specific report's updates apply
  // to THIS submission (a user may have already had an unconfirmed report
  // from a previous session sitting in the queue).
  const currentItemIdRef = useRef<string | null>(null);
  // A helpQueue subscribe() effect also runs on mount for backwards state
  // — separate from the per-submit subscription below so we can pick up
  // items from previous sessions.
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
  // ── #185 (2026-09-01 — Paul): GROUP SIZE at this address ────────────
  // Asked AFTER the primary answer so it can never block or delay it.
  // Skippable. Never counted into any total (see the ANTI-DOUBLE-COUNT
  // CONTRACT in server.py). The chosen value travels with the report
  // through the offline queue, so a follow-up made offline waits with
  // the report and lands together when the network returns.
  const [chosenGroupSize, setChosenGroupSize] = useState<GroupSize | null>(null);
  const [groupSizeOpen, setGroupSizeOpen] = useState(false);
  // Which sheet (if any) to open once the group-size sheet closes.
  //   "mobility" → yellow-severity mobility follow-up
  //   "egress"   → green-severity way-out follow-up
  //   null       → end of flow (safe path or red-severity trapped path,
  //                and every time the sheet is reopened for correction)
  const [groupSizeNextSheet, setGroupSizeNextSheet] =
    useState<"mobility" | "egress" | null>(null);
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

  // ─── SIREN RESURRECTION (BUG-2026-09-volume-down-kills-siren) ─────────
  // Paul, testing a Critical Alert siren: pressed volume-down once while
  // the siren was playing. Instead of lowering the volume, the sound was
  // killed completely, and turning volume back up did not bring it back.
  //
  // Root cause: on Android (and some iOS builds) the hardware volume-down
  // key at a low media-stream level, plus any brief audio-focus loss from
  // the volume-key UI, transitions the expo-audio player into a paused
  // state. Nothing in the alert screen was watching for that transition,
  // so a siren silenced by an OS event stayed silenced for the rest of
  // the incident — which for a Critical Alert is unacceptable, since the
  // audible cue is the whole point of the screen.
  //
  // The rule (Paul, verbatim): "Volume-down should keep it playing, just
  // quieter." That is the OS's job for the *media stream volume*, and we
  // must not interfere with it. Our job is to make sure the *player* keeps
  // running so turning volume back up brings the sound back.
  //
  // Implementation:
  //   - `wasPlayingRef` latches true the first time the player actually
  //     reports playing. That way this effect can distinguish an initial
  //     "not yet playing" state (nothing to resurrect) from a "was
  //     playing, then paused externally" transition.
  //   - When playing flips from true → false while shouldPlayRef.current
  //     is still true (i.e. the user has NOT tapped I'm Safe / triage /
  //     Dismiss and no stand-down has arrived), we re-call play() after
  //     a 200 ms debounce.
  //   - The debounce lets the KILL-SWITCH, the status="sent" safety net,
  //     the unmount cleanup, and the stand-down branch all run first —
  //     each of those flips shouldPlayRef.current to false, and the
  //     timeout callback re-checks the ref before touching the player.
  //     So resurrection can never resurrect a siren the user silenced,
  //     which is the #31/#50 failure shape.
  //   - We re-apply loop=true and volume=1.0 on resume, because those
  //     are the player's own settings — separate from the OS media
  //     stream volume that the hardware key legitimately controls.
  const wasPlayingRef = useRef(false);
  useEffect(() => {
    if (sirenStatus.playing) {
      wasPlayingRef.current = true;
      return;
    }
    // playing === false from here down.
    if (!sirenStatus.isLoaded) return;
    if (!shouldPlayRef.current) return;
    if (!wasPlayingRef.current) return;
    const t = setTimeout(() => {
      // Re-check the guard — the user (or a stand-down) may have
      // silenced the siren during the debounce window. If so, do
      // nothing; this is what protects #31/#50.
      if (!shouldPlayRef.current) return;
      try {
        sirenPlayer.loop = true;
        sirenPlayer.volume = 1.0;
        sirenPlayer.play();
        console.log("[QuakeAngel] SIREN resurrected after external pause");
      } catch (e) {
        console.log(
          "[QuakeAngel] SIREN resurrect() threw:",
          (e as Error)?.message,
        );
      }
    }, 200);
    return () => clearTimeout(t);
  }, [sirenStatus.isLoaded, sirenStatus.playing, sirenPlayer]);

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
        try {
          sirenPlayer.loop = false;
          sirenPlayer.volume = 0;
          sirenPlayer.pause();
        } catch { /* non-fatal */ }
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
        // #289 (2026-08-23 — Paul): CHECK, never ask. iOS put its location
        // box on top of a playing siren while he was trying to press
        // "I need help". Nothing on this screen may ever raise a system
        // permission box: location is asked for during setup, and on the
        // home screen if it was refused. Without it we send the report
        // anyway and the board says the place is not known.
        const perm = await Location.getForegroundPermissionsAsync();
        if (!perm.granted || cancelled) return;
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

  // #288 (2026-08-23 — Paul): "The practice does not vibrate. Sound only.
  // This is the one screen whose entire purpose is showing someone what a
  // real alert feels like, and it makes a promise the phone does not keep."
  //
  // What was there: ONE short haptic tap when the screen opened. A fifth of
  // a second, under a siren. Not a lie — just far too small to feel.
  //
  // What it does now: a heavy buzz every 900 ms for as long as the siren is
  // playing, on a real alert and on a practice alike, so the phone is
  // noticeably buzzing rather than tapping once. iOS gives no long
  // vibration to an app (Vibration's duration and pattern are ignored
  // there), so a repeated heavy impact is the honest way to do it.
  //
  // The one thing we cannot control is System Haptics being switched off in
  // iOS settings — no app can vibrate then. The practice screen says so in
  // one line rather than promising something the phone may refuse.
  useEffect(() => {
    if (!shouldPlaySiren) return;
    let stopped = false;
    const buzz = () => {
      if (stopped) return;
      Haptics.impactAsync(Haptics.ImpactFeedbackStyle.Heavy).catch(() => {});
    };
    buzz();
    const id = setInterval(() => {
      // Stops itself the moment the siren stops — answering the alert
      // silences the buzzing as well as the sound.
      if (!shouldPlayRef.current) return;
      buzz();
    }, 900);
    return () => {
      stopped = true;
      clearInterval(id);
    };
  }, [shouldPlaySiren]);

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
    // #286: never on a practice run. The practice run ends with a question
    // only the person can answer — did the siren come out of the phone or
    // the watch? — and an automatic bounce to home raced that panel and
    // won, so the question could never be seen. A practice run now waits
    // for them.
    if (isTestRun) return;
    const nav = setTimeout(() => router.replace("/"), 1200);
    return () => clearTimeout(nav);
  }, [status, outcome, router, isTestRun]);

  // #193 (2026-08-30 — Paul): drive the visible UI state off the persistent
  // offline queue. The queue owns retry-forever; this screen just mirrors
  // whatever the queue says about the report we submitted.
  //
  //   confirmed_at set   → status "sent"    (only place this transition happens)
  //   attempts >= 1      → status "pending_retry"  (still trying — no tick)
  //   otherwise          → status "sending" (initial attempt in flight)
  //
  // The subscription persists as long as the alert screen is mounted; if
  // the user leaves and comes back the currentItemIdRef will already have
  // been reset, so we won't be tracking a stale item.
  useEffect(() => {
    const unsub = subscribeHelpQueue((items) => {
      const id = currentItemIdRef.current;
      if (!id) return;
      const it: QueueItem | undefined = items.find((x) => x.id === id);
      if (!it) {
        // The queue no longer contains our item. This can only happen if
        // some other consumer explicitly removed it — leave state alone.
        return;
      }
      setPendingAttempts(it.attempts);
      if (it.confirmed_at) {
        setStatus("sent");
      } else if (it.attempts >= 1) {
        // First attempt has been made and did not succeed. Show the
        // honest "still trying" state — never a tick, never green.
        setStatus((prev) =>
          prev === "sent" ? prev : "pending_retry",
        );
      }
    });
    return unsub;
  }, []);

  // Manual "Try now" — the user asks us to attempt right away. Also called
  // implicitly whenever the retry pill is tapped so the app feels alive.
  const handleRetryNow = () => {
    Haptics.impactAsync(Haptics.ImpactFeedbackStyle.Light).catch(() => {});
    kickFlush();
  };

  const submitCheckIn = async (
    kind: OutcomeKind,
    severity: TriageSeverity | null = null,
    mobility: Mobility | null = null,
    egress: Egress | null = null,
    // #289 (2026-08-24 — Paul): a follow-up answer UPDATES a report that
    // has already been sent. Without this the guard below would swallow
    // it, because the first send happens the moment a severity is chosen.
    isFollowUp = false,
    // #185: group size at this address. Optional. Travels regardless of
    // status. NEVER counted into any total (see backend contract).
    groupSize: GroupSize | null = null,
  ) => {
    if (!isFollowUp && (status === "sending" || status === "sent" || status === "pending_retry")) return;
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
          // #289: check only — a permission box must never appear during an
          // alert or a practice.
          const perm = await Location.getForegroundPermissionsAsync();
          if (perm.granted) {
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
      if (isTestRun) {
        console.log(
          `[QuakeGuard] TEST RUN — skipping real ${kind} post to backend`,
        );
        setStatus("sent");
      } else {
        // #193 (2026-08-30 — Paul): route the report through the persistent
        // offline queue. The queue owns retry-forever + AsyncStorage
        // persistence. Nothing on this screen may say "sent" until the
        // queue reports `confirmed_at` set — i.e., our backend returned
        // 2xx on a real round trip.
        const itemId = await submitStatus({
          status: kind === "safe" ? "safe" : "trapped",
          severity: kind === "trapped" ? severity : null,
          mobility: kind === "trapped" ? mobility : null,
          egress: kind === "trapped" ? egress : null,
          // #185: group_size travels regardless of status. Never counted.
          group_size: groupSize,
          location: { latitude, longitude, accuracy, error: locationError },
          battery: { level: batteryLevel, state: batteryState },
        });
        currentItemIdRef.current = itemId;
        console.log(
          `[QuakeGuard] ${kind}${severity ? "/" + severity : ""}${mobility ? "/" + mobility : ""} → enqueued as`,
          itemId,
        );
        // Status stays "sending" until the queue subscription (below) tells
        // us the first attempt has landed. That subscription will move us
        // to either "sent" (if confirmed) or "pending_retry" (if not).
      }

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
      //
      // #193: fire regardless of delivery status — the rescue-info card
      // is a LOCAL notification, useful to a first responder finding
      // the phone even if our backend never received the report. The
      // whole feature exists for the case where the network is dead.
      if (kind === "trapped" && !isTestRun) {
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
      // The queue enqueue itself failed (very rare — AsyncStorage down).
      // Even then, we don't lie: show error, invite retry.
      console.log("[QuakeGuard] check-in enqueue error:", e?.message);
      setErrorMsg(e?.message ?? "Could not save your report on this phone.");
      setStatus("error");
    }
  };

  const handleImSafe = () => {
    // #185: fire the report immediately (unchanged rule — one tap sends).
    // The group-size sheet opens right after so it can NEVER block or
    // delay the primary answer. If the user skips or dismisses the
    // sheet, the report still went through with group_size=null.
    submitCheckIn("safe");
    openGroupSizeSheet(null);
  };

  // #185: open the group-size sheet. `next` is the sheet (if any) to
  // open once the user picks or skips — the trapped-yellow path
  // continues to "mobility", the trapped-green path to "egress", and
  // everything else (safe, red, and every correction from the toast)
  // ends here.
  const openGroupSizeSheet = (next: "mobility" | "egress" | null) => {
    setGroupSizeNextSheet(next);
    setGroupSizeOpen(true);
  };

  const openTriage = () => {
    if (status === "sending" || status === "sent" || status === "pending_retry") return;
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

    // #289 (2026-08-24 — Paul, live test): "a real MINOR report didn't
    // appear anywhere on the board on first submission". It never reached
    // the board because NOTHING was sent until the follow-up question was
    // answered — and both sheets can be left, which lost the whole report.
    // Both sheets even promised "This does not delay your report" while
    // being the thing delaying it.
    //
    // So the report goes NOW, on the severity tap, and the follow-up
    // answer updates it. A person who has told us they are hurt is on the
    // board from that moment, whatever they do next.
    if (severity === "yellow") {
      // Only yellow needs the mobility follow-up — mobility is genuinely
      // ambiguous for a serious-but-stable injury.
      setPendingSeverity(severity);
      submitCheckIn("trapped", severity, null);
      // #185: group-size sheet interposes BEFORE mobility. Ordering per
      // Paul, 2026-09-01: "right after severity is chosen — same 'one
      // report already gone' moment as today". The mobility sheet will
      // open when the group-size sheet closes.
      openGroupSizeSheet("mobility");
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
      submitCheckIn("trapped", severity, "mobile", "not_answered");
      // #185: group-size sheet interposes BEFORE egress.
      openGroupSizeSheet("egress");
      Haptics.impactAsync(Haptics.ImpactFeedbackStyle.Light).catch(() => {});
      return;
    }

    // Red → seriously injured / can't move → mobility is "trapped".
    // Passed as a follow-up so that changing MINOR or SERIOUS to
    // IMMEDIATE after the first send still gets through.
    submitCheckIn("trapped", severity, "trapped", null, true);
    // #185: still ask group-size on the red path — a rescuer arriving at
    // a red report needs "how many people" as much as any other severity.
    // No further follow-up sheet after this one; the flow ends here.
    openGroupSizeSheet(null);
  };

  const chooseMobility = (mobility: Mobility) => {
    // Defensive: silence again immediately before we start the async
    // submission chain (GPS → battery → network) so even a multi-second
    // network stall cannot leave the siren audible.
    stopSiren();
    setMobilityOpen(false);
    const sev = pendingSeverity;
    setPendingSeverity(null);
    // #185: carry the current chosen group_size into the follow-up so
    // an offline-queued report still lands with everything the user
    // told us on one round-trip.
    submitCheckIn("trapped", sev, mobility, null, true, chosenGroupSize);
  };

  const chooseEgress = (egress: Egress) => {
    stopSiren();
    setEgressOpen(false);
    const sev = pendingSeverity;
    setPendingSeverity(null);
    // #185: carry group_size in the same follow-up (see chooseMobility).
    submitCheckIn("trapped", sev, "mobile", egress, true, chosenGroupSize);
  };

  // #185: user picked a group-size bucket, or tapped Skip (size === null).
  // Fires a follow-up update only if the value actually changed, then
  // opens the next sheet in a trapped flow (mobility/egress) if any.
  //
  // "Skip" is a real answer: it clears any previously chosen value
  // rather than silently keeping the old one, so a person correcting
  // their report can go from "3" back to "not sure" without being stuck
  // with a wrong number that decides how many rescuers get sent.
  const chooseGroupSize = (size: GroupSize | null) => {
    const changed = size !== chosenGroupSize;
    setChosenGroupSize(size);
    setGroupSizeOpen(false);
    if (changed) {
      // Fire an update through the queue. If the phone is offline the
      // group-size sits with the queued report and lands with it — the
      // same anti-lie contract as the primary answer.
      submitCheckIn(
        outcome,
        chosenSeverity,
        chosenMobility,
        chosenEgress,
        true,
        size,
      );
    }
    // Continue trapped-flow follow-ups if any were queued behind this
    // sheet (yellow → mobility, green → egress).
    const next = groupSizeNextSheet;
    setGroupSizeNextSheet(null);
    if (next === "mobility") setMobilityOpen(true);
    else if (next === "egress") setEgressOpen(true);
  };

  // Back arrow inside a follow-up sheet: reopen the severity picker so the
  // user can re-answer without losing their place in the flow. #289: the
  // report has ALREADY been sent by this point, so nothing is lost either
  // way — changing the severity simply sends an update.
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

      {/* Red gradient background — calm blue instead when this is only a
          check-in request (#271). Colour never carries the meaning on its
          own: the words above say it too. */}
      <LinearGradient
        colors={isCheckInRequest
          ? ["#0B1220", "#132038", "#101A2C", "#0F1115"]
          : ["#3B0A08", "#7A0E10", "#3B0A08", "#0F1115"]}
        locations={[0, 0.35, 0.7, 1]}
        style={StyleSheet.absoluteFill}
      />

      <SafeAreaView edges={["top", "bottom"]} style={styles.content}>
        {/* Top banner */}
        <View style={styles.topBanner}>
          {!isCheckInRequest && <View style={styles.liveDot} />}
          <Text style={styles.liveText}>
            {isCheckInRequest ? "CHECK-IN REQUEST" : `LIVE ALERT · ${mm}:${ss}`}
          </Text>
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
          {isCheckInRequest ? (
            /* #271: reassurance first, question second, nothing else.
               "No new earthquake" is the FIRST thing on the screen,
               because a person opening an earthquake app fears the
               worst for a second before they read on. */
            <View style={styles.checkinPanel} testID="checkin-request-panel">
              <View style={styles.checkinBadge}>
                <Ionicons name="heart-outline" size={44} color="#BFD3F2" />
              </View>
              <Text style={styles.checkinLead}>No new earthquake.</Text>
              <Text style={styles.checkinLead}>
                We are just checking how you are.
              </Text>
              <Text style={styles.checkinHeading}>Are you all right?</Text>
              <Text style={styles.checkinBody}>
                Tap one button below. That is all we need.
              </Text>
            </View>
          ) : (
            <>
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
                  DROP. COVER. HOLD ON.
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
            </>
          )}
        </View>

        {/* Data strip — own row, never overlapped by the buttons below.
            Hidden for a check-in request: there is no event to describe,
            and empty readings would read as missing data (#205 rule 9.4).

            #292 (2026-08-23 — Paul): "Pick one way of saying 'we do not
            know this' and use it in every field, every screen, both PDFs
            and the CSV." It is "Not known". A dash is not a word and reads
            as a broken field.

            And on a practice: "dashes teach people that a real alert looks
            broken." A practice has no event behind it, so the strip is
            replaced by one line saying exactly that, rather than three
            empty fields or invented figures that could be mistaken for
            real ones. */}
        {!isCheckInRequest && isTestRun && (
          <View style={styles.metricsRow} testID="alert-practice-readings-note">
            <Text style={styles.practiceReadingsNote}>
              This is a practice, so there are no real readings. In a real
              alert this strip shows how strong the earthquake was, how far
              away it was, and how hard the shaking was where you are.
            </Text>
          </View>
        )}
        {!isCheckInRequest && !isTestRun && (
        <View style={styles.metricsRow}>
          <View style={styles.metric}>
            <Text style={[styles.metricLabel, compact && styles.metricLabelCompact]}>
              MAGNITUDE
            </Text>
            <Text
              style={[styles.metricValue,
                      eventReadings.magnitude == null && styles.metricValueMissing]}
              numberOfLines={2}
              adjustsFontSizeToFit
            >
              {eventReadings.magnitude ?? "Not known"}
            </Text>
          </View>
          <View style={styles.metricDivider} />
          <View style={styles.metric}>
            <Text style={[styles.metricLabel, compact && styles.metricLabelCompact]}>
              DISTANCE
            </Text>
            <Text
              style={[styles.metricValue,
                      eventReadings.distance_km == null && styles.metricValueMissing]}
              numberOfLines={2}
              adjustsFontSizeToFit
            >
              {eventReadings.distance_km != null ? (
                <>{roundDistanceKm(eventReadings.distance_km)}
                  <Text style={styles.metricUnit}>km</Text></>
              ) : "Not known"}
            </Text>
          </View>
          <View style={styles.metricDivider} />
          <View style={styles.metric}>
            <Text style={[styles.metricLabel, compact && styles.metricLabelCompact]}>
              INTENSITY
            </Text>
            {/* #273 (2026-08-21 — Paul): a bare dash beside a filled-in
                magnitude and distance reads like a missing reading. EMSC
                often publishes no intensity at all for small or distant
                events. #292 (2026-08-23): and it says the same words as
                every other unknown field — "Not known" — because two ways
                of saying it on one screen makes a reader hunt for a
                difference that is not there. */}
            <Text
              style={[styles.metricValue,
                      eventReadings.intensity == null && styles.metricValueMissing]}
              numberOfLines={2}
              adjustsFontSizeToFit
            >
              {eventReadings.intensity ?? "Not known"}
            </Text>
          </View>
        </View>
        )}

        {/* Bottom action */}
        <View style={[styles.bottomWrap, { paddingBottom: Math.max(insets.bottom, spacing.md) }]}>
          {status === "error" && errorMsg && (
            <View style={styles.errorToast} testID="alert-error-toast">
              <Ionicons name="alert-circle" size={22} color={colors.warning} />
              <Text style={styles.errorText}>{errorMsg}. Tap again to retry.</Text>
            </View>
          )}
          {/* #193 (2026-08-30 — Paul): "still trying" banner. This is the
              anti-lie. It replaces any tick / green mark / "sent" wording
              while the phone has NOT yet had a 2xx round-trip to our
              server. The moment we do, the queue subscription flips this
              to `status === "sent"` and the delivered toast below takes
              over.
              Rules for the copy here:
                - Never uses the words "sent" / "delivered" / "received".
                - Says plainly what we're doing (trying), what state we
                  are in (not through yet), and that we won't stop.
                - Shows the honest attempt counter so the user can see
                  the retry loop is real, not a spinner that never ends. */}
          {(status === "sending" || status === "pending_retry") && !stoodDown && (
            <View
              style={styles.pendingToast}
              testID={
                status === "pending_retry"
                  ? "alert-pending-retry-toast"
                  : "alert-sending-toast"
              }
              accessibilityLiveRegion="polite"
              accessibilityRole="alert"
            >
              <Ionicons
                name="sync"
                size={22}
                color="#FFD79A"
                style={pendingAttempts > 0 ? styles.pendingSpinning : undefined}
              />
              <View style={{ flex: 1 }}>
                <Text style={styles.pendingTitle}>
                  {status === "pending_retry"
                    ? "Still trying to reach the rescue team."
                    : outcome === "trapped"
                      ? "Sending your help request…"
                      : "Sending your check-in…"}
                </Text>
                <Text style={styles.pendingBody}>
                  {status === "pending_retry"
                    ? outcome === "trapped"
                      ? `Your phone has not been able to reach our server yet. It will keep trying and tell you the moment it gets through. Attempt ${pendingAttempts + 1}.`
                      : `Your phone has not been able to reach our server yet. It will keep trying. Attempt ${pendingAttempts + 1}.`
                    : "This can take a few seconds when the signal is weak."}
                </Text>
              </View>
              {status === "pending_retry" && (
                <Pressable
                  onPress={handleRetryNow}
                  hitSlop={12}
                  style={styles.pendingRetryBtn}
                  testID="alert-pending-try-now-btn"
                  accessibilityLabel="Try to send now"
                >
                  <Text style={styles.pendingRetryBtnText}>Try now</Text>
                </Pressable>
              )}
            </View>
          )}
          {status === "sent" && outcome === "safe" && (
            <View style={styles.successToast} testID="alert-success-toast">
              <Ionicons name="checkmark-circle" size={22} color={colors.onSuccess} />
              <View style={{ flex: 1 }}>
                <Text style={styles.successText}>
                  {isTestRun
                    ? "Practice finished. Nothing was sent to anyone."
                    : "Your report reached the rescue team."}
                </Text>
                {!isTestRun && (
                  /* #185: tap to edit — people mis-tap under stress, and
                     this number decides how many rescuers get sent. */
                  <Pressable
                    onPress={() => openGroupSizeSheet(null)}
                    hitSlop={8}
                    testID="edit-group-size-safe"
                    accessibilityLabel="Change how many people are here"
                    accessibilityRole="button"
                  >
                    <Text style={styles.groupSizeLine}>
                      {groupSizeSentence(chosenGroupSize)}{" "}
                      <Text style={styles.groupSizeLineAction}>
                        {chosenGroupSize == null ? "" : "Change"}
                      </Text>
                    </Text>
                  </Pressable>
                )}
              </View>
            </View>
          )}
          {/* #286 (2026-08-22 — Paul): "if the practice siren plays on the
              Watch instead of the phone, the app has just discovered the
              problem itself and should say so." We cannot detect where the
              sound came out — iOS does not tell us, and WCSession needs a
              watchOS app we do not have — so we ask the person, once, at
              the only moment they can possibly know. Their answer restarts
              the Watch reminder however they answered it before. */}
          {isTestRun && status === "sent" && !wristAnswer && (
            <View style={styles.wristAsk} testID="rehearsal-wrist-ask">
              <Text style={styles.wristAskTitle}>
                Did your phone sound the siren?
              </Text>
              <View style={styles.wristAskRow}>
                <Pressable
                  onPress={() => setWristAnswer("phone")}
                  style={styles.wristAskBtn}
                  testID="rehearsal-wrist-phone"
                >
                  <Ionicons name="phone-portrait" size={18} color="#0F1115" />
                  <Text style={styles.wristAskBtnText}>Yes</Text>
                </Pressable>
                <Pressable
                  onPress={async () => {
                    setWristAnswer("wrist");
                    await recordHeardOnWrist();
                  }}
                  style={styles.wristAskBtn}
                  testID="rehearsal-wrist-watch"
                >
                  <Ionicons name="watch" size={18} color="#0F1115" />
                  <Text style={styles.wristAskBtnText}>No — only my watch buzzed</Text>
                </Pressable>
              </View>
            </View>
          )}
          {isTestRun && wristAnswer === "wrist" && (
            <View style={styles.wristWarn} testID="rehearsal-wrist-warning">
              <Text style={styles.wristWarnText}>
                Your watch took the sound, so your phone may stay quiet in a
                real earthquake. Turn watch notifications for Quake Angel off.
                We have put the reminder back on your home screen.
              </Text>
            </View>
          )}
          {isTestRun && wristAnswer === "phone" && (
            <View style={styles.wristOk} testID="rehearsal-wrist-ok">
              <Text style={styles.wristOkText}>
                Good — your phone made the sound. That is what you want.
              </Text>
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
                  {/* #283: never imply somebody is watching the board. It
                      says what happened and what to do next, nothing more. */}
                  {/* #193 (2026-08-30 — Paul): "Reached" is only ever
                      shown here, and only after the queue reports a real
                      2xx from our server. It cannot fire on the return
                      of fetch, on a Render 200, or on any local success. */}
                  {isTestRun
                    ? "Practice finished. Nothing was sent to anyone."
                    : "Your help request reached the rescue team. Stay calm. Save your battery."}
                </Text>
                {chosenEgress && chosenEgress !== "not_answered" ? (
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
                {!isTestRun && (
                  /* #185: group-size line, tappable to correct. Same
                     contract as the safe path — never counted, only
                     ever informational for a rescuer at this address. */
                  <Pressable
                    onPress={() => openGroupSizeSheet(null)}
                    hitSlop={8}
                    testID="edit-group-size-trapped"
                    accessibilityLabel="Change how many people are here"
                    accessibilityRole="button"
                  >
                    <Text style={styles.trappedGroupSizeLine}>
                      {groupSizeSentence(chosenGroupSize)}{" "}
                      <Text style={styles.trappedGroupSizeLineAction}>
                        {chosenGroupSize == null ? "" : "Change"}
                      </Text>
                    </Text>
                  </Pressable>
                )}
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
              disabled={status === "sending" || status === "sent" || status === "pending_retry"}
              style={({ pressed }) => [
                styles.safeBtn,
                (status === "sent") && styles.safeBtnDone,
                (status === "pending_retry" && outcome === "safe") && styles.safeBtnPending,
                pressed && { opacity: 0.9, transform: [{ scale: 0.98 }] },
              ]}
              testID="im-safe-btn"
            >
              <Ionicons
                name={
                  status === "sent" && outcome === "safe"
                    ? "checkmark"
                    : status === "pending_retry" && outcome === "safe"
                      ? "sync"
                      : "shield-checkmark"
                }
                size={26}
                color={colors.onSuccess}
              />
              <Text style={styles.safeBtnText}>
                {/* #193: NEVER "Marked safe" while unconfirmed. The button
                    must never imply the check-in has been received.
                    #321 (2026-08-31 — Paul): "Still trying…" reads like
                    an action button, especially sitting where the user
                    just tapped. "Not sent yet" is unambiguously a
                    status and never claims a cause we don't know
                    (their signal vs our server). */}
                {status === "sending" && outcome === "safe"
                  ? "Sending…"
                  : status === "pending_retry" && outcome === "safe"
                    ? "Not sent yet"
                    : status === "sent" && outcome === "safe"
                      ? "Marked safe"
                      : "I'm safe"}
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
              disabled={status === "sending" || status === "pending_retry"}
              style={({ pressed }) => [
                styles.trappedBtn,
                (status === "pending_retry" && outcome === "trapped") && styles.trappedBtnPending,
                pressed && { opacity: 0.9, transform: [{ scale: 0.98 }] },
              ]}
              testID="im-trapped-btn"
            >
              <Ionicons name="warning" size={22} color="#fff" />
              <Text style={styles.trappedBtnText}>
                {/* #193: NEVER shows "Sent" text here. While unconfirmed
                    the button says the honest status. #321 (2026-08-31
                    — Paul): the previous "Still trying…" sat exactly
                    where the user tapped "I need help" a second earlier
                    and read as another button to press. "Not sent yet"
                    is unambiguously a status, is true regardless of
                    whether the fault is the user's signal or our
                    server, and says the one thing that matters. */}
                {status === "sending" && outcome === "trapped"
                  ? "Sending…"
                  : status === "pending_retry" && outcome === "trapped"
                    ? "Not sent yet"
                    : "I need help"}
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
              {isCheckInRequest ? (
                <>
                  If you don&apos;t answer, we mark you as{" "}
                  <Text style={styles.unansweredNoteBold}>not responding</Text>
                  {" "}— never as trapped. No earthquake alert is running.
                  You can close this and answer later.
                </>
              ) : (
                <>
                  If you don&apos;t answer, we mark you as{" "}
                  <Text style={styles.unansweredNoteBold}>not responding</Text>
                  {" "}— never as trapped. The siren stops when you tap I&apos;m
                  safe or I need help, when someone else calls off the alert, or
                  about a minute after it started if neither has happened.
                </>
              )}
            </Text>
          )}

          {/* Task #14: the "Dismiss alert" escape hatch is gone — an
              unanswered alert must not be dismissable, because a dismissal
              looks identical to silence on the dashboard. The only way off
              this screen is to answer (I'M SAFE / I NEED HELP). After a
              trapped report is confirmed, a plain "Back to home" remains. */}
          {status === "sent" && (outcome === "trapped" || isTestRun) && (
            <Pressable
              onPress={() => {
                stopSiren();
                // #290 (2026-08-23 — Paul): "Back to home" went back one
                // screen, and a practice launched from setup made that
                // screen setup. It says home, so it goes home, from every
                // path that shows it.
                router.replace("/");
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
                pinned. Your first answer is safe on this phone —
                this adds a detail to it.
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
              {/* #51 / #290 (Paul, 2026-08-25): "the mobility question
                  still appears after selecting the green option." It was
                  never the mobility question — it was this one, worded so
                  closely ("Can you get out on your own?") that it read as
                  the same question asked twice. It now asks about the way
                  out and nothing else. */}
              <Text style={styles.triageTitle}>Is your way out blocked?</Text>
              <Text style={styles.triageSubtitle}>
                About the building, not your injuries. A jammed door or a
                blocked stairwell counts as blocked.
                Your first answer is safe on this phone — this adds a
                detail to it.
              </Text>

              <TriageOption
                color="#2E7D32"
                label="No — I can get out"
                sublabel="Nothing is blocking my way out"
                icon="exit"
                onPress={() => chooseEgress("can_exit")}
                testID="egress-can-exit"
              />
              <TriageOption
                color="#C21818"
                label="Yes — I&apos;m blocked in"
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

        {/* #185 (2026-09-01 — Paul): GROUP-SIZE sheet. Asked AFTER the
            primary answer so it can never block or delay it. Skippable.
            NEVER counted into any total — see StatusInPayload's
            ANTI-DOUBLE-COUNT CONTRACT on the backend. The chosen value
            travels with the report through the offline queue, so a
            follow-up made offline waits and lands with the report when
            the network returns. Every touch target is ≥ 44pt so the
            frightened one-tap answer works reliably. */}
        <Modal
          visible={groupSizeOpen}
          animationType="slide"
          transparent
          onRequestClose={() => chooseGroupSize(chosenGroupSize)}
        >
          <View style={styles.triageBackdrop}>
            <View
              style={[
                styles.triageSheet,
                { paddingBottom: Math.max(insets.bottom + spacing.md, spacing.xl) },
              ]}
            >
              <View style={styles.triageHandle} />
              <Text style={styles.triageTitle}>
                Including you, how many people are here?
              </Text>
              <Text style={styles.triageSubtitle}>
                {/* Reassures the frightened reporter that this does not
                    delay the report — which by design has already gone. */}
                Your answer is already on its way. This tells the rescuer
                at the door how many people to expect.
              </Text>

              <View style={styles.groupSizeRow} testID="group-size-row">
                <GroupSizePill
                  label="Just me"
                  selected={chosenGroupSize === "just_me"}
                  onPress={() => chooseGroupSize("just_me")}
                  testID="group-size-just-me"
                />
                <GroupSizePill
                  label="2"
                  selected={chosenGroupSize === "2"}
                  onPress={() => chooseGroupSize("2")}
                  testID="group-size-2"
                />
                <GroupSizePill
                  label="3"
                  selected={chosenGroupSize === "3"}
                  onPress={() => chooseGroupSize("3")}
                  testID="group-size-3"
                />
                <GroupSizePill
                  label="4"
                  selected={chosenGroupSize === "4"}
                  onPress={() => chooseGroupSize("4")}
                  testID="group-size-4"
                />
                <GroupSizePill
                  label="5 or more"
                  selected={chosenGroupSize === "5_plus"}
                  onPress={() => chooseGroupSize("5_plus")}
                  testID="group-size-5-plus"
                  wide
                />
              </View>

              <Pressable
                onPress={() => chooseGroupSize(null)}
                style={styles.triageCancel}
                hitSlop={8}
                testID="group-size-skip"
                accessibilityLabel="Skip this question"
              >
                <Text style={styles.triageCancelText}>Skip</Text>
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

// #185: one pill in the group-size sheet. 44pt tall (Apple HIG minimum)
// so it can be tapped by someone whose hands are shaking. `wide` is
// used for "5 or more" which needs more horizontal room than a digit.
function GroupSizePill({
  label,
  selected,
  onPress,
  testID,
  wide = false,
}: {
  label: string;
  selected: boolean;
  onPress: () => void;
  testID?: string;
  wide?: boolean;
}) {
  return (
    <Pressable
      onPress={onPress}
      testID={testID}
      style={({ pressed }) => [
        styles.groupSizePill,
        wide && styles.groupSizePillWide,
        selected && styles.groupSizePillSelected,
        pressed && { opacity: 0.9, transform: [{ scale: 0.98 }] },
      ]}
      accessibilityRole="button"
      accessibilityState={{ selected }}
      accessibilityLabel={label}
      hitSlop={4}
    >
      <Text
        style={[
          styles.groupSizePillText,
          selected && styles.groupSizePillTextSelected,
        ]}
      >
        {label}
      </Text>
    </Pressable>
  );
}

// #185: user-facing sentence for the sent-toast "Reported:" line, so
// the reporter can see what we captured and correct it if they mis-tapped
// under stress. Buckets are opaque strings; only phrase them here.
//
// null → the field was skipped; render the "unknown" phrasing so the
// user can add it later. NEVER render as "just 1" — a skip is not an
// answer of one.
function groupSizeSentence(size: GroupSize | null): string {
  if (size === "just_me") return "Reported: just you here.";
  if (size === "5_plus") return "Reported: you and 4 or more others here.";
  if (size === "2" || size === "3" || size === "4") {
    const others = parseInt(size, 10) - 1;
    return `Reported: you and ${others} ${others === 1 ? "other" : "others"} here.`;
  }
  return "How many people are here? Tap to add.";
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
    fontSize: 14,
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
    fontSize: 14,
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
    fontSize: 14,
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
  // #286 practice-run question: where did the sound come from?
  wristAsk: {
    marginTop: spacing.md,
    padding: spacing.lg,
    borderRadius: radius.lg,
    backgroundColor: "rgba(255,255,255,0.08)",
    borderWidth: 1,
    borderColor: "rgba(255,255,255,0.2)",
  },
  wristAskTitle: {
    color: "#FFFFFF",
    fontSize: 16,
    fontWeight: "700",
    marginBottom: spacing.md,
    textAlign: "center",
  },
  wristAskRow: { flexDirection: "row", gap: spacing.md },
  wristAskBtn: {
    flex: 1,
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "center",
    gap: 6,
    minHeight: 48,
    borderRadius: radius.md,
    backgroundColor: "#E7EDF5",
  },
  wristAskBtnText: { color: "#0F1115", fontSize: 15, fontWeight: "700" },
  wristWarn: {
    marginTop: spacing.md,
    padding: spacing.lg,
    borderRadius: radius.lg,
    backgroundColor: "#4A0E0E",
    borderWidth: 1,
    borderColor: "#E64545",
  },
  wristWarnText: { color: "#FFE1E1", fontSize: 15, lineHeight: 22 },
  wristOk: {
    marginTop: spacing.md,
    padding: spacing.lg,
    borderRadius: radius.lg,
    backgroundColor: "#0F2818",
    borderWidth: 1,
    borderColor: "#1F8A3A",
  },
  wristOkText: { color: "#B3E5C4", fontSize: 15, lineHeight: 22 },

  // #271 check-in request: calm, short lines, one idea each.
  checkinPanel: {
    alignItems: "center",
    paddingHorizontal: spacing.lg,
    gap: 8,
  },
  checkinBadge: {
    width: 88,
    height: 88,
    borderRadius: 44,
    alignItems: "center",
    justifyContent: "center",
    backgroundColor: "rgba(191,211,242,0.12)",
    marginBottom: spacing.md,
  },
  checkinLead: {
    color: "#DCE6F7",
    fontSize: 19,
    lineHeight: 27,
    fontWeight: "700",
    textAlign: "center",
  },
  checkinHeading: {
    color: "#FFFFFF",
    fontSize: 30,
    lineHeight: 36,
    fontWeight: "900",
    textAlign: "center",
    marginTop: spacing.md,
  },
  checkinBody: {
    color: "#9FB3D1",
    fontSize: 16,
    lineHeight: 23,
    textAlign: "center",
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
    textAlign: "center",
    lineHeight: 44,
  },
  headingCompact: {
    fontSize: 30,
    lineHeight: 34,
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
    fontWeight: "700",
    marginBottom: 4,
  },
  metricLabelCompact: {
    fontSize: 10,
  },
  metricValue: {
    color: colors.onSurface,
    fontSize: 28,
    fontWeight: "900",
  },
  metricUnit: {
    fontSize: 14,
    fontWeight: "600",
    color: "rgba(255,255,255,0.7)",
  },
  // #292: "Not known" is a sentence, not a reading — it must not be
  // styled like one, or the eye reads it as a value.
  metricValueMissing: {
    fontSize: 15,
    fontWeight: "600",
    color: "rgba(255,255,255,0.75)",
  },
  // #292: on a practice there is no event, so the strip says so in words
  // instead of showing three empty fields that look like a fault.
  practiceReadingsNote: {
    flex: 1,
    color: "rgba(255,255,255,0.88)",
    fontSize: 15,
    lineHeight: 21,
    fontWeight: "600",
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
  // #193 (2026-08-30 — Paul): the "still trying" toast. Amber, never
  // green — the colour semantics on this screen are locked:
  //   red     = danger / warning
  //   amber   = pending / in-flight (NOT a failure, but NOT success)
  //   green   = confirmed by our server
  // The visible design difference between amber and green is deliberate
  // — a glance must be enough to tell "in progress" apart from "done".
  pendingToast: {
    flexDirection: "row",
    alignItems: "center",
    gap: spacing.sm,
    backgroundColor: "rgba(80, 55, 20, 0.92)",
    borderWidth: 1,
    borderColor: "rgba(255, 200, 100, 0.6)",
    paddingHorizontal: spacing.md,
    paddingVertical: spacing.md,
    borderRadius: radius.md,
  },
  pendingTitle: {
    color: "#FFE7B5",
    fontSize: 15,
    fontWeight: "800",
    lineHeight: 20,
  },
  pendingBody: {
    marginTop: 2,
    color: "rgba(255, 231, 181, 0.85)",
    fontSize: 13,
    lineHeight: 17,
  },
  pendingRetryBtn: {
    paddingVertical: 8,
    paddingHorizontal: 12,
    borderRadius: 8,
    backgroundColor: "rgba(255, 215, 154, 0.15)",
    borderWidth: 1,
    borderColor: "rgba(255, 215, 154, 0.6)",
  },
  pendingRetryBtnText: {
    color: "#FFE7B5",
    fontSize: 13,
    fontWeight: "800",
  },
  pendingSpinning: {
    // Reanimated is not used here to keep this tiny — the sync icon is
    // visually enough on its own; a static rotate would over-promise
    // real-time activity when a retry can be minutes away.
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
  // #193: amber tint while unconfirmed — clearly NOT the "done" green.
  // The button also shows "Not sent yet" text while in this state, and
  // is disabled so a re-tap doesn't create a duplicate report.
  safeBtnPending: {
    backgroundColor: "#8B6A00",
    shadowOpacity: 0.3,
  },
  safeBtnText: {
    color: colors.onSuccess,
    fontSize: 22,
    fontWeight: "900",
  },
  dismissBtn: {
    alignItems: "center",
    paddingVertical: spacing.md,
  },
  dismissText: {
    color: "rgba(255,255,255,0.7)",
    fontSize: 15,
    fontWeight: "700",
  },
  // #250 (Batch 7 D1): "if you don't answer" note. Small enough not
  // to compete with the buttons, prominent enough to read at a
  // glance while the siren is going.
  unansweredNote: {
    color: "rgba(255,255,255,0.82)",
    fontSize: 14,
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
  // #193: darker amber while unconfirmed — visually distinct from the
  // "please tap me" primary state; button is also disabled so the user
  // does not accidentally re-submit while retry is in flight.
  trappedBtnPending: {
    backgroundColor: "#8B6A00",
    borderColor: "#6E4F00",
  },
  trappedBtnText: {
    color: "#fff",
    fontSize: 20,
    fontWeight: "900",
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
  },

  /* #185: group-size sheet + reported-line on the toasts.
   *
   * Pills are 56pt tall (well over the 44pt HIG minimum). "5 or more"
   * uses .wide because a two-word label needs the horizontal room.
   * The row wraps, so on a small screen the pills flow to a second
   * line rather than get clipped.
   *
   * The reported-line on the success toast is DELIBERATELY visible as
   * a tap target: an underlined "Change" hint sits next to the value
   * so a frightened reporter can correct a mis-tap without hunting.
   */
  groupSizeRow: {
    flexDirection: "row",
    flexWrap: "wrap",
    gap: spacing.sm,
    marginBottom: spacing.md,
  },
  groupSizePill: {
    minHeight: 56,
    minWidth: 88,
    flexGrow: 1,
    flexBasis: "28%",
    alignItems: "center",
    justifyContent: "center",
    borderRadius: radius.lg,
    borderWidth: 2,
    borderColor: "rgba(255,255,255,0.25)",
    backgroundColor: "rgba(255,255,255,0.06)",
    paddingHorizontal: spacing.md,
  },
  groupSizePillWide: {
    // "5 or more" — full row on its own so the wrap doesn't leave an
    // awkward orphan next to a digit pill.
    flexBasis: "100%",
  },
  groupSizePillSelected: {
    borderColor: "#4EE0A5",
    backgroundColor: "rgba(78,224,165,0.18)",
  },
  groupSizePillText: {
    color: "#fff",
    fontSize: 20,
    fontWeight: "700",
  },
  groupSizePillTextSelected: {
    color: "#DFFBEC",
  },

  // Line inside the safe success toast.
  groupSizeLine: {
    color: colors.onSuccess,
    fontSize: 14,
    fontWeight: "600",
    marginTop: 4,
    opacity: 0.95,
  },
  groupSizeLineAction: {
    textDecorationLine: "underline",
    fontWeight: "800",
  },
  // Line inside the trapped toast (white-on-severity-colour).
  trappedGroupSizeLine: {
    color: "rgba(255,255,255,0.92)",
    fontSize: 15,
    fontWeight: "600",
    lineHeight: 20,
    marginTop: 6,
  },
  trappedGroupSizeLineAction: {
    textDecorationLine: "underline",
    fontWeight: "800",
  },
});
