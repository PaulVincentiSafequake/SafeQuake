import { Stack, useRouter } from "expo-router";
import * as SplashScreen from "expo-splash-screen";
import * as Notifications from "expo-notifications";
import * as Linking from "expo-linking";
import { setAudioModeAsync } from "expo-audio";
import { useEffect } from "react";
import { LogBox, Platform } from "react-native";
import { SafeAreaProvider } from "react-native-safe-area-context";
import AsyncStorage from "@react-native-async-storage/async-storage";

import { useIconFonts } from "@/src/hooks/use-icon-fonts";
import { registerForPushNotifications } from "@/src/utils/push";

const ONBOARDING_DONE_KEY = "quakeguard_onboarding_done";

// Disable logbox errors etc so that users can see the app
// and agent works as expected.
LogBox.ignoreAllLogs(true);

// Keep the native splash visible from cold start until icon fonts register.
SplashScreen.preventAutoHideAsync();

// ---- Module-scope notification setup (before any component mounts) ----
if (Platform.OS !== "web") {
  Notifications.setNotificationHandler({
    handleNotification: async () => ({
      shouldShowBanner: true,
      shouldShowList: true,
      shouldPlaySound: true,
      shouldSetBadge: false,
    }),
  });
}

if (Platform.OS === "android") {
  // Fire and forget — channel must exist before any push arrives
  Notifications.setNotificationChannelAsync("default", {
    name: "Default",
    importance: Notifications.AndroidImportance.MAX,
    sound: "default",
  }).catch(() => {});
  Notifications.setNotificationChannelAsync("quakeguard-critical", {
    name: "Earthquake safety reminders",
    importance: Notifications.AndroidImportance.MAX,
    sound: "default",
    vibrationPattern: [0, 400, 300, 400],
    bypassDnd: true,
    lockscreenVisibility: Notifications.AndroidNotificationVisibility.PUBLIC,
  }).catch(() => {});
}

// Module-scope audio-session init so the siren on /alert can play through
// the physical silent switch on iOS. Runs BEFORE any useAudioPlayer is
// instantiated.
if (Platform.OS !== "web") {
  setAudioModeAsync({
    playsInSilentMode: true,
    shouldPlayInBackground: false,
    interruptionMode: "doNotMix",
    allowsRecording: false,
  }).catch((e) =>
    console.log("[QuakeGuard] cold-start setAudioModeAsync err:", e?.message),
  );
}

export default function RootLayout() {
  const [loaded, error] = useIconFonts();
  const router = useRouter();

  useEffect(() => {
    if (loaded || error) {
      SplashScreen.hideAsync();
    }
  }, [loaded, error]);

  // Register push token on cold start (retries on every app open).
  //
  // On iOS we gate the FIRST-EVER permission prompt behind an /onboarding
  // screen so the user sees the Apple Watch caveat right next to the ask.
  // On every subsequent launch (or on Android) we register silently.
  useEffect(() => {
    if (Platform.OS === "web") return;

    (async () => {
      if (Platform.OS === "ios") {
        try {
          const done = await AsyncStorage.getItem(ONBOARDING_DONE_KEY);
          if (!done) {
            const perm = await Notifications.getPermissionsAsync();
            // Only intercept if we still have a chance to prompt. If the
            // user already granted (upgrading from an older build) or
            // already denied permanently, just mark onboarding done and
            // continue silently — the note lives on /diag for reference.
            if (!perm.granted && perm.canAskAgain) {
              router.replace("/onboarding");
              return;
            }
            await AsyncStorage.setItem(ONBOARDING_DONE_KEY, "1");
          }
        } catch (e) {
          console.log("[QuakeGuard] onboarding gate err:", (e as Error)?.message);
        }
      }

      registerForPushNotifications().catch((e) =>
        console.log("[QuakeGuard] push register error:", e?.message),
      );
    })();
  }, [router]);

  // Handle notification taps → route by payload `kind`, fail-safe to informational.
  //
  // BUG-2026-08-06-preview-tap-siren: previously this handler defaulted
  // to /alert for any notification without an explicit action_url. That
  // meant a preview notification (M2.7 event 1,300km away) tapped by the
  // user opened the full EARTHQUAKE DETECTED screen + siren. That is the
  // exact alert-fatigue failure the preview constraints exist to prevent.
  //
  // Fix: route by `data.kind`:
  //   - "critical_alert" → /alert (existing critical-alert flow)
  //   - "emsc_preview"   → /quake/[unid] (informational detail, no siren)
  //   - anything else / missing → /quake/[unid] fallback (informational)
  //
  // Fail-safe philosophy: a missed siren on tap is recoverable (the
  // notification itself already carried siren+haptics if it was real);
  // a spurious siren on tap destroys trust permanently. Default MUST
  // be informational, not critical.
  useEffect(() => {
    if (Platform.OS === "web") return;

    const handleTap = (data: Record<string, any>) => {
      const kind = String(data.kind || "").trim();
      const unid = data.unid ? String(data.unid) : null;

      // Real critical alert — route to /alert with event details as params.
      // The `siren=1` param signals the alert screen that it should play
      // the siren on mount. Missing `siren=1` = no siren (fail-safe).
      if (kind === "critical_alert") {
        const params = new URLSearchParams();
        params.set("siren", "1");
        if (data.magnitude != null)   params.set("magnitude", String(data.magnitude));
        if (data.distance_km != null) params.set("distance_km", String(data.distance_km));
        if (data.intensity != null)   params.set("intensity", String(data.intensity));
        if (data.depth_km != null)    params.set("depth_km", String(data.depth_km));
        if (data.region != null)      params.set("region", String(data.region));
        if (data.unid != null)        params.set("unid", String(data.unid));
        router.push(("/alert?" + params.toString()) as any);
        return;
      }

      // Check-in reminder ("Are you safe?" follow-up) — routes to /alert
      // for the check-in flow, but explicitly NO siren. The user is
      // being reminded to check in, not being alerted afresh.
      if (kind === "quakeguard-reminder") {
        router.push("/alert?siren=0&reminder=1" as any);
        return;
      }

      // Explicit informational deep-link (non-/alert). Web URLs still open externally.
      const explicit = data.action_url || data.deeplink;
      if (explicit && typeof explicit === "string" && explicit !== "/alert") {
        if (explicit.startsWith("http")) {
          Linking.openURL(explicit).catch(() => {});
        } else {
          router.push(explicit as any);
        }
        return;
      }

      // Preview or unknown kind → informational detail screen (fail-safe).
      // A siren on a mistaken tap destroys trust permanently; a missed
      // siren is recoverable because the notification itself carried
      // sound+haptics if it was truly critical.
      const params = new URLSearchParams();
      Object.entries(data).forEach(([k, v]) => {
        if (v != null && k !== "kind" && k !== "action_url" && k !== "deeplink" && k !== "aps") {
          params.set(k, String(v));
        }
      });
      const qs = params.toString();
      const path = unid ? `/quake/${encodeURIComponent(unid)}` : "/quake/unknown";
      router.push((path + (qs ? "?" + qs : "")) as any);
    };

    const tapSub = Notifications.addNotificationResponseReceivedListener(
      (response) => {
        const data = (response.notification.request.content.data ?? {}) as any;
        handleTap(data);
      },
    );

    // Cold-start tap (app was killed when notification arrived)
    Notifications.getLastNotificationResponseAsync().then((response) => {
      if (!response) return;
      const data = (response.notification.request.content.data ?? {}) as any;
      handleTap(data);
    });

    return () => {
      try {
        tapSub.remove();
      } catch {
        // web shim
      }
    };
  }, [router]);

  // If the CDN is unreachable we fall through on error rather than wedging
  // the app — icons will tofu, but the app still boots.
  if (!loaded && !error) return null;

  return (
    <SafeAreaProvider>
      <Stack screenOptions={{ headerShown: false }} />
    </SafeAreaProvider>
  );
}
