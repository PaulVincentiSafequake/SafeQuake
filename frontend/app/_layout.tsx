import { Stack, useRouter } from "expo-router";
import * as SplashScreen from "expo-splash-screen";
import * as Notifications from "expo-notifications";
import * as Linking from "expo-linking";
import { setAudioModeAsync } from "expo-audio";
import { useEffect } from "react";
import { LogBox, Platform } from "react-native";
import { SafeAreaProvider } from "react-native-safe-area-context";

import { useIconFonts } from "@/src/hooks/use-icon-fonts";
import { registerForPushNotifications } from "@/src/utils/push";

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

  // Register push token on cold start (retries on every app open)
  useEffect(() => {
    if (Platform.OS === "web") return;
    registerForPushNotifications().catch((e) =>
      console.log("[QuakeGuard] push register error:", e?.message),
    );
  }, []);

  // Handle notification taps → open /alert
  useEffect(() => {
    if (Platform.OS === "web") return;

    const handleUrl = (url: string | undefined) => {
      if (!url) return;
      if (url.startsWith("http")) {
        Linking.openURL(url).catch(() => {});
      } else {
        router.push(url as any);
      }
    };

    const tapSub = Notifications.addNotificationResponseReceivedListener(
      (response) => {
        const data = (response.notification.request.content.data ?? {}) as any;
        // Reminder notifications also lead to /alert
        const url = data.action_url || data.deeplink || "/alert";
        handleUrl(url);
      },
    );

    // Cold-start tap (app was killed when notification arrived)
    Notifications.getLastNotificationResponseAsync().then((response) => {
      if (!response) return;
      const data = (response.notification.request.content.data ?? {}) as any;
      const url = data.action_url || data.deeplink || "/alert";
      handleUrl(url);
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
