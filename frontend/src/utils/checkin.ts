import AsyncStorage from "@react-native-async-storage/async-storage";
import Constants from "expo-constants";

import { SAFE_ENDPOINT } from "@/src/theme";

const DEVICE_ID_KEY = "quakeguard_device_id";
const DISPLAY_NAME_KEY = "quakeangel_display_name";
const NAME_PROMPTED_KEY = "quakeangel_name_prompted";

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

/**
 * The 5-char field-facing "rescue code" that lets an on-site responder confirm
 * a pin against the physical phone on/near a victim. Derived from the last
 * five chars of the device id, uppercased.
 *
 * Only ever used as a LOCAL tie-breaker among 2-3 pins already narrowed down
 * by GPS proximity — not as a globally unique identifier. Kept in sync with
 * the backend's `_short_code()` derivation so a responder reading the phone
 * screen sees the same string as the dashboard pin label.
 */
export function shortCodeFromDeviceId(deviceId: string): string {
  return String(deviceId).slice(-5).toUpperCase();
}

export async function getShortCode(): Promise<string> {
  const id = await getDeviceId();
  return shortCodeFromDeviceId(id);
}

/**
 * Local sanitizer mirroring the backend's `_sanitize_display_name`:
 * trims, strips ASCII control chars (keeps unicode letters like é / 京),
 * caps at 40 chars. Empty / whitespace-only returns null so callers can
 * treat "no name" and "explicitly cleared" identically.
 */
export function sanitizeDisplayName(raw: string | null | undefined): string | null {
  if (raw == null) return null;
  const s = String(raw);
  // Keep printable ASCII (0x20-0x7E) + everything ≥ 0x80 (all unicode).
  // Drops \n\r\t and other control chars that would break dashboard rendering.
  let cleaned = "";
  for (let i = 0; i < s.length; i++) {
    const c = s.charCodeAt(i);
    if ((c >= 32 && c < 127) || c >= 128) cleaned += s[i];
  }
  cleaned = cleaned.trim();
  if (!cleaned) return null;
  if (cleaned.length > 40) cleaned = cleaned.slice(0, 40).trimEnd();
  return cleaned || null;
}

export async function getDisplayName(): Promise<string | null> {
  try {
    const raw = await AsyncStorage.getItem(DISPLAY_NAME_KEY);
    return sanitizeDisplayName(raw);
  } catch {
    return null;
  }
}

/**
 * Persist (or clear) the user's optional first name.
 * Passing null / empty / whitespace-only clears storage entirely so the
 * dashboard falls back to short_code-only display on the next check-in.
 * Also marks the "we've asked once" flag so the onboarding prompt never
 * reappears after the user has made an explicit choice either way.
 */
export async function setDisplayName(name: string | null): Promise<string | null> {
  const clean = sanitizeDisplayName(name);
  try {
    if (clean) {
      await AsyncStorage.setItem(DISPLAY_NAME_KEY, clean);
    } else {
      await AsyncStorage.removeItem(DISPLAY_NAME_KEY);
    }
    await AsyncStorage.setItem(NAME_PROMPTED_KEY, "1");
  } catch (e) {
    console.log("[QuakeAngel] setDisplayName persist failed:", (e as Error)?.message);
  }
  return clean;
}

/** Has the first-launch name prompt already been shown? Used by the home
 *  screen to decide whether to auto-open the name modal exactly once. */
export async function wasNamePrompted(): Promise<boolean> {
  try {
    return (await AsyncStorage.getItem(NAME_PROMPTED_KEY)) === "1";
  } catch {
    return false;
  }
}

/** Mark the prompt as shown without changing the stored name — used when the
 *  user taps "Skip" on the first-launch modal (they didn't set a name, but
 *  we shouldn't ask again). */
export async function markNamePrompted(): Promise<void> {
  try {
    await AsyncStorage.setItem(NAME_PROMPTED_KEY, "1");
  } catch {
    // ignore
  }
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
  // Best-effort — if AsyncStorage is unavailable we send null and the
  // dashboard falls back to short_code-only display for this check-in.
  const displayName = await getDisplayName().catch(() => null);
  const { status, severity, mobility, location, battery } = opts;

  const payload: Record<string, any> = {
    deviceId,
    status,
    // severity is only meaningful for `trapped`; backend also enforces this.
    severity: status === "trapped" ? (severity ?? null) : null,
    // mobility ("mobile" | "trapped") is likewise trapped-only; backend
    // normalizer will null it out for other statuses defensively.
    mobility: status === "trapped" ? (mobility ?? null) : null,
    // Optional first name for responder-side identification. Nullable —
    // dashboard falls back to short_code alone when null.
    display_name: displayName,
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
