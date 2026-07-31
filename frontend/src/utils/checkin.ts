import AsyncStorage from "@react-native-async-storage/async-storage";
import Constants from "expo-constants";

import { SAFE_ENDPOINT } from "@/src/theme";

const DEVICE_ID_KEY = "quakeguard_device_id";

// Our own backend — the rescuer dashboard fetches real device data from here.
// The app dual-posts to both the external Render endpoint (SAFE_ENDPOINT) and
// this backend so nothing on the Render side breaks during cutover.
const BACKEND_URL =
  process.env.EXPO_PUBLIC_BACKEND_URL ??
  (Constants.expoConfig?.extra?.EXPO_PUBLIC_BACKEND_URL as string | undefined);

export async function getDeviceId(): Promise<string> {
  let id = await AsyncStorage.getItem(DEVICE_ID_KEY);
  if (!id) {
    id = `qg-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
    await AsyncStorage.setItem(DEVICE_ID_KEY, id);
  }
  return id;
}

export type CheckInStatus = "not_responding" | "safe" | "trapped";
export type TriageSeverity = "green" | "yellow" | "red";
/**
 * Follow-up mobility answer captured after severity for `trapped` check-ins.
 *  - "mobile"  → "Yes, I can move"
 *  - "trapped" → "No, I'm trapped/pinned (e.g. under debris)"
 * `null` for non-trapped statuses, or when the user hasn't answered yet.
 */
export type Mobility = "mobile" | "trapped";

export interface LocationPayload {
  latitude: number | null;
  longitude: number | null;
  accuracy: number | null;
  error: string | null;
}

export interface BatteryPayload {
  level: number | null;
  state: string | null;
}

/**
 * POST a status update. Dual-posts to:
 *   1) The external safequake.onrender.com endpoint (SAFE_ENDPOINT) — legacy
 *      receiver, unchanged.
 *   2) Our own backend (${BACKEND_URL}/api/status) — the source of truth for
 *      the rescuer dashboard's GET /api/devices call.
 *
 * Returns the response from the primary (Render) endpoint so callers that
 * inspect res.ok / res.status keep working identically. The backend post
 * runs in parallel and its outcome is logged but never blocks the caller.
 */
export async function postStatus(opts: {
  status: CheckInStatus;
  severity?: TriageSeverity | null;
  mobility?: Mobility | null;
  location?: LocationPayload;
  battery?: BatteryPayload;
}): Promise<Response> {
  const deviceId = await getDeviceId();
  const { status, severity, mobility, location, battery } = opts;

  const payload: Record<string, any> = {
    deviceId,
    status,
    // severity is only meaningful for `trapped`; backend also enforces this.
    severity: status === "trapped" ? (severity ?? null) : null,
    // mobility ("mobile" | "trapped") is likewise trapped-only; backend
    // normalizer will null it out for other statuses defensively.
    mobility: status === "trapped" ? (mobility ?? null) : null,
    client_name: "quakeguard-mobile",
    timestamp: new Date().toISOString(),
    location: location ?? {
      latitude: null,
      longitude: null,
      accuracy: null,
      error: null,
    },
    battery: battery ?? { level: null, state: null },
    batteryLevel: battery?.level ?? null,
    batteryState: battery?.state ?? null,
  };

  const lat = location?.latitude ?? null;
  const lng = location?.longitude ?? null;
  if (lat !== null && lng !== null) {
    payload.latitude = lat;
    payload.longitude = lng;
    payload.lat = lat;
    payload.lng = lng;
    payload.lon = lng;
    payload.accuracy = location?.accuracy ?? null;
    payload.coords = {
      latitude: lat,
      longitude: lng,
      accuracy: location?.accuracy ?? null,
    };
    payload.coordinates = [lng, lat];
    payload.geo = { type: "Point", coordinates: [lng, lat] };
  }

  console.log(
    `[QuakeGuard] POST (${status}${severity ? "/" + severity : ""}${mobility ? "/" + mobility : ""}) →`,
    JSON.stringify(payload),
  );

  // Fire both in parallel. The primary response (Render) is what we return —
  // the backend post is fire-and-forget and its outcome is only logged.
  const renderReq = fetch(SAFE_ENDPOINT, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });

  if (BACKEND_URL) {
    fetch(`${BACKEND_URL}/api/status`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    })
      .then((r) => {
        console.log("[QuakeGuard] backend /api/status →", r.status);
      })
      .catch((e: Error) => {
        console.log("[QuakeGuard] backend /api/status failed:", e?.message);
      });
  }

  return renderReq;
}
