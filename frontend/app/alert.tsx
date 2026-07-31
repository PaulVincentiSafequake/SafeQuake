import { useRouter } from "expo-router";
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
import { postStatus, type Mobility, type TriageSeverity } from "@/src/utils/checkin";
import { cancelCheckInReminders } from "@/src/utils/reminders";

const SIREN_SOURCE = require("../assets/audio/siren.mp3");

type Status = "idle" | "sending" | "sent" | "error";
type OutcomeKind = "safe" | "trapped";

export default function AlertScreen() {
  const router = useRouter();
  const insets = useSafeAreaInsets();
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
  const shouldPlayRef = useRef(true);

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
      } catch {
        // ignore — audio hardware may be claimed by another app
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
      const res = await postStatus({
        status: kind === "safe" ? "safe" : "trapped",
        severity: kind === "trapped" ? severity : null,
        mobility: kind === "trapped" ? mobility : null,
        location: { latitude, longitude, accuracy, error: locationError },
        battery: { level: batteryLevel, state: batteryState },
      });
      console.log(
        `[QuakeGuard] ${kind}${severity ? "/" + severity : ""}${mobility ? "/" + mobility : ""} → response status:`,
        res.status,
      );
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      setStatus("sent");
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
    // Severity chosen → close severity sheet, remember it, and open the
    // mobility follow-up. Submission is deferred until the user answers
    // "Can you move?". stopSiren() is called defensively at every step of
    // the trapped flow — the kill-switch effect will also catch any
    // resurrection, but calling it here means we don't rely on that safety
    // net firing in time.
    stopSiren();
    setTriageOpen(false);
    setPendingSeverity(severity);
    setMobilityOpen(true);
    Haptics.impactAsync(Haptics.ImpactFeedbackStyle.Light).catch(() => {});
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

  // Back arrow inside the mobility sheet: reopen severity picker so the
  // user can re-answer without losing their place in the flow.
  const backToSeverity = () => {
    stopSiren();
    setMobilityOpen(false);
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

        {/* Center: pulsing warning */}
        <View style={styles.center}>
          <View style={styles.pulseWrap}>
            <Animated.View style={[styles.pulseRing, ringStyle]} />
            <Animated.View style={[styles.pulseRing, styles.pulseRingInner, ringStyle]} />
            <Animated.View style={[styles.iconBubble, iconStyle]}>
              <Ionicons name="warning" size={72} color={colors.onBrandPrimary} />
            </Animated.View>
          </View>

          <Text style={styles.heading}>EARTHQUAKE{"\n"}DETECTED</Text>
          <Text style={styles.subheading}>
            Drop. Cover. Hold on.{"\n"}Move to open space when shaking stops.
          </Text>

          <View style={styles.metricsRow}>
            <View style={styles.metric}>
              <Text style={styles.metricLabel}>MAGNITUDE</Text>
              <Text style={styles.metricValue}>6.4</Text>
            </View>
            <View style={styles.metricDivider} />
            <View style={styles.metric}>
              <Text style={styles.metricLabel}>DISTANCE</Text>
              <Text style={styles.metricValue}>12<Text style={styles.metricUnit}>km</Text></Text>
            </View>
            <View style={styles.metricDivider} />
            <View style={styles.metric}>
              <Text style={styles.metricLabel}>INTENSITY</Text>
              <Text style={styles.metricValue}>VII</Text>
            </View>
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
                {chosenMobility ? (
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

          {/* Primary: I'M SAFE (green) — hidden once a trapped report was sent */}
          {!(status === "sent" && outcome === "trapped") && (
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

          {/* Secondary: TRAPPED / NEED HELP (amber) — opens triage sheet */}
          {status !== "sent" && (
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
                  : "I'M TRAPPED / NEED HELP"}
              </Text>
            </Pressable>
          )}

          <Pressable
            onPress={() => {
              stopSiren();
              cancelCheckInReminders().catch(() => {});
              if (router.canGoBack()) {
                router.back();
              } else {
                router.replace("/");
              }
            }}
            style={styles.dismissBtn}
            hitSlop={12}
            testID="alert-dismiss-btn"
          >
            <Text style={styles.dismissText}>
              {status === "sent" && outcome === "trapped"
                ? "Back to home"
                : "Dismiss alert"}
            </Text>
          </Pressable>
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
  heading: {
    color: colors.onSurface,
    fontSize: 40,
    fontWeight: "900",
    letterSpacing: 3,
    textAlign: "center",
    lineHeight: 44,
  },
  subheading: {
    marginTop: spacing.md,
    color: "rgba(255,255,255,0.9)",
    fontSize: 19,
    fontWeight: "500",
    textAlign: "center",
    lineHeight: 26,
  },
  metricsRow: {
    marginTop: spacing.xl,
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
