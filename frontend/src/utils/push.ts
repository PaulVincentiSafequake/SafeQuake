import * as Notifications from "expo-notifications";
import Constants from "expo-constants";
import * as Application from "expo-application";
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
  last_register_detail: string | null;
  backend_url: string;
  app_version: string | null;
  build_number: string | null;
  // #266 / #260 (Neo, 2026-08-20 — Paul): a server-side read-back so
  // the "Is this working?" screen never claims "signed up" from local
  // state alone. Populated by getServerRegistrationStatus() which
  // calls GET /api/register-push/status/{user_id}. Any of these can
  // be null if the read-back hasn't completed (no network, backend
  // down) — the diag screen treats null as "not yet confirmed" (red)
  // rather than showing a stale green from a previous session.
  server_has_device: boolean | null;
  server_last_seen_at: string | null;
  server_dead_token: boolean;
  server_last_attempt_error: string | null;
  server_last_attempt_status: number | null;
  relay_healthy: boolean | null;
}

// #266 / #260: the shape returned by /api/register-push/status/{user_id}.
// Kept exactly parallel to the FastAPI response so a schema change on
// the backend surfaces as a TS error, not a silent wrong-shape read.
export interface ServerRegistrationStatus {
  registered: boolean;
  platform: string | null;
  last_seen_at: string | null;
  dead_token: boolean;
  dead_token_reason: string | null;
  last_attempt: {
    at: string | null;
    relay_status: number | null;
    relay_error: string | null;
    persisted: boolean;
  } | null;
  relay_healthy: boolean | null;
}

function fingerprint(token: string | null): string | null {
  if (!token) return null;
  if (token.length <= 16) return token;
  return `${token.slice(0, 8)}…${token.slice(-8)}`;
}

export async function getServerRegistrationStatus(
  user_id: string,
): Promise<ServerRegistrationStatus | null> {
  // #266 / #260 (Neo, 2026-08-20 — Paul): the "Is this working?"
  // screen uses this read-back — NOT the local token length — as
  // the source of truth for "your phone is on the server's alert
  // list". Returning null means "we couldn't ask the server"; the
  // caller treats that as red (not-yet-confirmed) rather than
  // painting a stale green from local state.
  if (Platform.OS === "web") return null;
  if (!BACKEND_URL) return null;
  try {
    const resp = await fetch(
      `${BACKEND_URL}/api/register-push/status/${encodeURIComponent(user_id)}`,
      { method: "GET", headers: { "Content-Type": "application/json" } },
    );
    if (!resp.ok) return null;
    const data = (await resp.json()) as ServerRegistrationStatus;
    return data;
  } catch {
    return null;
  }
}

export async function getDiagInfo(): Promise<DiagInfo> {
  const user_id = await getDeviceId();
  const device_token = await AsyncStorage.getItem(LAST_TOKEN_KEY);
  const meta = await AsyncStorage.getItem(LAST_REGISTER_KEY);
  let last_registered_at: string | null = null;
  let last_register_status: string | null = null;
  let last_register_detail: string | null = null;
  if (meta) {
    try {
      const parsed = JSON.parse(meta);
      last_registered_at = parsed.at ?? null;
      last_register_status = parsed.status ?? null;
      last_register_detail = parsed.detail ?? null;
    } catch {}
  }

  // #266 / #260 (Neo, 2026-08-20 — Paul): read the server-side
  // truth alongside local state. If the network call fails we
  // leave `server_*` fields at null so the diag row stays honestly
  // red instead of showing a stale positive from a previous run.
  const serverStatus = await getServerRegistrationStatus(user_id);

  return {
    user_id,
    platform: Platform.OS,
    device_token,
    token_length: device_token?.length ?? 0,
    token_fingerprint: fingerprint(device_token),
    last_registered_at,
    last_register_status,
    last_register_detail,
    backend_url: BACKEND_URL ?? "(not set)",
    // Read from the INSTALLED BINARY, not from app.json (#169 aftermath).
    // Constants.expoConfig.ios.buildNumber is null here because the build
    // number is assigned by the build pipeline, so Diagnostics showed "—"
    // and there was no way for anyone to tell which build a phone was
    // holding — which is exactly how a 3-week-old build went unnoticed.
    // expo-application reads the real values out of the binary.
    // On web expo-application reports a placeholder "1.0.0", so app.json is
    // the truth there; on a device the binary is the truth.
    app_version:
      (Platform.OS === "web"
        ? (Constants.expoConfig?.version as string)
        : Application.nativeApplicationVersion) ??
      (Constants.expoConfig?.version as string) ??
      null,
    build_number:
      Application.nativeBuildVersion ??
      (Constants.expoConfig?.ios?.buildNumber as string) ??
      (Constants.expoConfig?.android?.versionCode as unknown as string) ??
      null,
    server_has_device: serverStatus ? serverStatus.registered : null,
    server_last_seen_at: serverStatus?.last_seen_at ?? null,
    server_dead_token: !!serverStatus?.dead_token,
    server_last_attempt_error: serverStatus?.last_attempt?.relay_error ?? null,
    server_last_attempt_status: serverStatus?.last_attempt?.relay_status ?? null,
    relay_healthy: serverStatus?.relay_healthy ?? null,
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
    let statusDetail: string | null = null;
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
      // #266 / #260 (Neo, 2026-08-20 — Paul): if the server refused us
      // (any non-2xx), read the plain-English `detail` back so we can
      // show it verbatim on the Diag screen. Without this we'd have
      // "HTTP 502" on screen for a user who has no idea what that
      // means; with this they see "Registrations are being refused by
      // our push provider…" from the server itself.
      if (!resp.ok) {
        try {
          const data = await resp.json();
          if (data && typeof data.detail === "string") {
            statusDetail = data.detail;
          }
        } catch {}
      }
    } catch (e) {
      statusLabel = `network error: ${(e as Error)?.message ?? "unknown"}`;
    }
    await AsyncStorage.setItem(
      LAST_REGISTER_KEY,
      JSON.stringify({
        at: new Date().toISOString(),
        status: statusLabel,
        detail: statusDetail,
      }),
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
