import * as Notifications from "expo-notifications";
import Constants from "expo-constants";
import { Platform } from "react-native";

import { getDeviceId } from "@/src/utils/checkin";

const BACKEND_URL =
  process.env.EXPO_PUBLIC_BACKEND_URL ??
  Constants.expoConfig?.extra?.EXPO_PUBLIC_BACKEND_URL;

/**
 * Ask for notification permission (regular loud alerts, NOT critical alerts —
 * those need Apple's separate entitlement) and register this device's native
 * push token with our FastAPI backend so it can broadcast alerts.
 */
export async function registerForPushNotifications(): Promise<void> {
  if (Platform.OS === "web") return;
  if (!BACKEND_URL) {
    console.log("[QuakeGuard] no EXPO_PUBLIC_BACKEND_URL; skipping push register");
    return;
  }

  try {
    const current = await Notifications.getPermissionsAsync();
    let granted = current.granted;
    if (!granted && current.canAskAgain) {
      const req = await Notifications.requestPermissionsAsync({
        ios: {
          allowAlert: true,
          allowSound: true,
          allowBadge: false,
          // Critical alerts entitlement approved by Apple — the remote
          // "EARTHQUAKE ALERT" push can now punch through silent/DND/Focus.
          allowCriticalAlerts: true,
          allowProvisional: false,
        },
        android: {},
      });
      granted = req.granted || req.status === "granted";
    }
    if (!granted) return;

    // Native FCM (Android) / APNs (iOS) token via Emergent push relay
    const tokenResp = await Notifications.getDevicePushTokenAsync();
    const device_token = tokenResp.data;
    const user_id = await getDeviceId();

    await fetch(`${BACKEND_URL}/api/register-push`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        user_id,
        platform: Platform.OS,
        device_token,
      }),
    });
    console.log("[QuakeGuard] push token registered for", user_id);
  } catch (e) {
    console.log("[QuakeGuard] registerForPush failed:", (e as Error)?.message);
  }
}

/**
 * Ask our FastAPI backend to broadcast an earthquake alert push to every
 * OTHER registered device (excludes the current device via triggeredBy).
 */
export async function broadcastAlert(opts?: {
  magnitude?: number;
  distance_km?: number;
  intensity?: string;
}): Promise<void> {
  if (!BACKEND_URL) return;
  try {
    const triggeredBy = await getDeviceId();
    await fetch(`${BACKEND_URL}/api/trigger-alert`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        triggeredBy,
        magnitude: opts?.magnitude ?? 6.4,
        distance_km: opts?.distance_km ?? 12,
        intensity: opts?.intensity ?? "VII",
      }),
    });
    console.log("[QuakeGuard] broadcast requested");
  } catch (e) {
    console.log("[QuakeGuard] broadcastAlert failed:", (e as Error)?.message);
  }
}
