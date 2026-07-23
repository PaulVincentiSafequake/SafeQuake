import { useRouter } from "expo-router";
import { StatusBar } from "expo-status-bar";
import { Ionicons } from "@expo/vector-icons";
import { LinearGradient } from "expo-linear-gradient";
import * as Haptics from "expo-haptics";
import * as Location from "expo-location";
import * as Battery from "expo-battery";
import { useAudioPlayer, setAudioModeAsync } from "expo-audio";
import { useEffect, useRef, useState } from "react";
import {
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
import { postStatus } from "@/src/utils/checkin";
import { cancelCheckInReminders } from "@/src/utils/reminders";

const SIREN_SOURCE = require("../assets/audio/siren.mp3");

type Status = "idle" | "sending" | "sent" | "error";

export default function AlertScreen() {
  const router = useRouter();
  const insets = useSafeAreaInsets();
  const [status, setStatus] = useState<Status>("idle");
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const [elapsed, setElapsed] = useState(0);

  const pulse = useSharedValue(1);
  const ring = useSharedValue(0);
  // Warm-up: keep the freshest high-accuracy GPS fix we've received so far
  const latestFixRef = useRef<Location.LocationObject | null>(null);

  // Looping siren — starts the moment the Alert screen mounts, stops when
  // the user marks themselves safe / dismisses / navigates away.
  const sirenPlayer = useAudioPlayer(SIREN_SOURCE);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        // playsInSilentMode → the siren plays through the physical silent
        // switch on iOS. This is the intended behaviour for an emergency alarm.
        await setAudioModeAsync({
          playsInSilentMode: true,
          shouldPlayInBackground: false,
          interruptionMode: "doNotMix",
        });
        if (cancelled) return;
        sirenPlayer.loop = true;
        sirenPlayer.volume = 1.0;
        sirenPlayer.play();
      } catch (e) {
        console.log("[QuakeGuard] siren start failed:", (e as Error)?.message);
      }
    })();
    return () => {
      cancelled = true;
      try {
        sirenPlayer.pause();
        sirenPlayer.seekTo(0);
      } catch {
        // ignore
      }
    };
  }, [sirenPlayer]);

  const stopSiren = () => {
    try {
      sirenPlayer.pause();
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
    const nav = setTimeout(() => router.replace("/"), 1200);
    return () => clearTimeout(nav);
  }, [status, router]);

  const handleImSafe = async () => {
    if (status === "sending" || status === "sent") return;
    setStatus("sending");
    setErrorMsg(null);
    Haptics.notificationAsync(Haptics.NotificationFeedbackType.Success).catch(
      () => {},
    );

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
        status: "safe",
        location: { latitude, longitude, accuracy, error: locationError },
        battery: { level: batteryLevel, state: batteryState },
      });
      console.log("[QuakeGuard] I'm Safe → response status:", res.status);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      // Cancel any pending / delivered reminder notifications now that the
      // user has actively marked themselves safe.
      await cancelCheckInReminders();
      stopSiren();
      setStatus("sent");
    } catch (e: any) {
      console.log("[QuakeGuard] I'm Safe → error:", e?.message);
      setErrorMsg(e?.message ?? "Network error");
      setStatus("error");
    }
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
              <Ionicons name="alert-circle" size={16} color={colors.warning} />
              <Text style={styles.errorText}>{errorMsg}. Tap again to retry.</Text>
            </View>
          )}
          {status === "sent" && (
            <View style={styles.successToast} testID="alert-success-toast">
              <Ionicons name="checkmark-circle" size={16} color={colors.onSuccess} />
              <Text style={styles.successText}>Report received. Stay safe.</Text>
            </View>
          )}

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
              name={status === "sent" ? "checkmark" : "shield-checkmark"}
              size={26}
              color={colors.onSuccess}
            />
            <Text style={styles.safeBtnText}>
              {status === "sending"
                ? "SENDING…"
                : status === "sent"
                  ? "MARKED SAFE"
                  : "I'M SAFE"}
            </Text>
          </Pressable>

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
            <Text style={styles.dismissText}>Dismiss alert</Text>
          </Pressable>
        </View>
      </SafeAreaView>
    </View>
  );
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
    color: "rgba(255,255,255,0.85)",
    fontSize: 15,
    textAlign: "center",
    lineHeight: 22,
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
    color: "rgba(255,255,255,0.6)",
    fontSize: 10,
    letterSpacing: 1.5,
    fontWeight: "700",
    marginBottom: 4,
  },
  metricValue: {
    color: colors.onSurface,
    fontSize: 24,
    fontWeight: "900",
    letterSpacing: 1,
  },
  metricUnit: {
    fontSize: 12,
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
    fontSize: 13,
    fontWeight: "600",
  },
  successToast: {
    flexDirection: "row",
    alignItems: "center",
    gap: spacing.sm,
    backgroundColor: colors.success,
    padding: spacing.md,
    borderRadius: radius.md,
  },
  successText: {
    flex: 1,
    color: colors.onSuccess,
    fontSize: 13,
    fontWeight: "700",
  },
  safeBtn: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "center",
    gap: spacing.md,
    height: 64,
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
    fontSize: 18,
    fontWeight: "900",
    letterSpacing: 3,
  },
  dismissBtn: {
    alignItems: "center",
    paddingVertical: spacing.sm,
  },
  dismissText: {
    color: "rgba(255,255,255,0.55)",
    fontSize: 13,
    fontWeight: "600",
    letterSpacing: 1,
  },
});
