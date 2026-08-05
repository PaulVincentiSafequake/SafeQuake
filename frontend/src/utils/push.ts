import * as Notifications from "expo-notifications";
import Constants from "expo-constants";
import { Platform } from "react-native";
import AsyncStorage from "@react-native-async-storage/async-storage";

import { getDeviceId } from "@/src/utils/checkin";

const BACKEND_URL =
  process.env.EXPO_PUBLIC_BACKEND_URL ??
  Constants.expoConfig?.extra?.EXPO_PUBLIC_BACKEND_URL;

const LAST_TOKEN_KEY = "quakeguard_last_push_token";
const LAST_REGISTER_KEY = "quakeguard_last_register_meta";

export interface DiagInfo {
  user_id: string;
  platform: string;
  device_token: string | null;
  token_length: number;
  token_fingerprint: string | null;
  last_registered_at: string | null;
  last_register_status: string | null;
  backend_url: string;
  app_version: string | null;
  build_number: string | null;
}

function fingerprint(token: string | null): string | null {
  if (!token) return null;
  if (token.length <= 16) return token;
  return `${token.slice(0, 8)}…${token.slice(-8)}`;
}

export async function getDiagInfo(): Promise<DiagInfo> {
  const user_id = await getDeviceId();
  const device_token = await AsyncStorage.getItem(LAST_TOKEN_KEY);
  const meta = await AsyncStorage.getItem(LAST_REGISTER_KEY);
  let last_registered_at: string | null = null;
  let last_register_status: string | null = null;
  if (meta) {
    try {
      const parsed = JSON.parse(meta);
      last_registered_at = parsed.at ?? null;
      last_register_status = parsed.status ?? null;
    } catch {}
  }
  return {
    user_id,
    platform: Platform.OS,
    device_token,
    token_length: device_token?.length ?? 0,
    token_fingerprint: fingerprint(device_token),
    last_registered_at,
    last_register_status,
    backend_url: BACKEND_URL ?? "(not set)",
    app_version: (Constants.expoConfig?.version as string) ?? null,
    build_number:
      (Constants.expoConfig?.ios?.buildNumber as string) ??
      (Constants.expoConfig?.android?.versionCode as unknown as string) ??
      null,
  };
}

/**
 * Ask for notification permission (including iOS Critical Alerts — the app
 * has Apple's com.apple.developer.usernotifications.critical-alerts
 * entitlement) and register this device's native push token with our
 * FastAPI backend so it can broadcast alerts.
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
    if (!granted) {
      await AsyncStorage.setItem(
        LAST_REGISTER_KEY,
        JSON.stringify({ at: new Date().toISOString(), status: "permission_denied" }),
      );
      return;
    }

    // Native FCM (Android) / APNs (iOS) token via Emergent push relay
    const tokenResp = await Notifications.getDevicePushTokenAsync();
    const device_token = tokenResp.data;
    const user_id = await getDeviceId();

    await AsyncStorage.setItem(LAST_TOKEN_KEY, device_token);

    let statusLabel = "unknown";
    try {
      const resp = await fetch(`${BACKEND_URL}/api/register-push`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          user_id,
          platform: Platform.OS,
          device_token,
        }),
      });
      statusLabel = `HTTP ${resp.status}`;
    } catch (e) {
      statusLabel = `network error: ${(e as Error)?.message ?? "unknown"}`;
    }
    await AsyncStorage.setItem(
      LAST_REGISTER_KEY,
      JSON.stringify({ at: new Date().toISOString(), status: statusLabel }),
    );
    console.log("[QuakeGuard] push token registered for", user_id, statusLabel);
  } catch (e) {
    console.log("[QuakeGuard] registerForPush failed:", (e as Error)?.message);
    await AsyncStorage.setItem(
      LAST_REGISTER_KEY,
      JSON.stringify({
        at: new Date().toISOString(),
        status: `error: ${(e as Error)?.message ?? "unknown"}`,
      }),
    );
  }
}

/**
 * @deprecated As of 2026-08 (Task #9), the mobile "TRIGGER TEST ALERT" button
 * no longer fans out to every registered device — it's a local drill only.
 * The dashboard's operator-authenticated flow is the only production path
 * that broadcasts. This function is retained for one release cycle in case
 * we reintroduce it as an admin-gated action; it will 401 in production
 * because the mobile app does not carry a JWT. Do NOT call from new code.
 *
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
