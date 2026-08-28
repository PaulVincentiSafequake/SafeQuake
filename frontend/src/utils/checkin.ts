import AsyncStorage from "@react-native-async-storage/async-storage";

import { enqueue as enqueueHelp, type QueueItemKind } from "@/src/utils/helpQueue";

const DEVICE_ID_KEY = "quakeguard_device_id";
const DISPLAY_NAME_KEY = "quakeangel_display_name";
const NAME_PROMPTED_KEY = "quakeangel_name_prompted";

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
 * Build a status payload and hand it to the persistent offline queue. Returns
 * the queue item ID so callers can subscribe to `helpQueue` events for this
 * specific report.
 *
 * #193 (2026-08-30 — Paul): NEVER treats "the fetch returned" as "the server
 * received it". The truth signal is a 2xx from our backend, delivered later
 * via the queue's subscribe API. Every UI surface must key its "sent" state
 * off `confirmed_at`, not off the return of this function.
 */
/**
 * Egress is NOT mobility (2026-06-18). Mobility describes the body; egress
 * describes the building. Someone can be fully mobile and still unable to
 * leave — jammed door, beam pinning a limb without injuring it, collapsed
 * stairwell, blocked basement — and only egress decides whether a team with
 * cutting gear is needed. Asked of GREEN reports only: they have just told us
 * they can walk, so asking about mobility again would be noise, while "minor
 * injury but cannot get out" is otherwise invisible to the operator.
 */
// #289: "not_answered" is a real answer — they chose a severity and then
// left the way-out question. The board says "we do not know" rather than
// filing them as walking wounded, which is the lowest priority there is.
export type Egress = "can_exit" | "cannot_exit" | "not_answered";

// #185 (2026-09-01 — Paul): "Including you, how many people are here?"
// Answered in one tap by a frightened reporter, purely so a rescuer at
// that address knows what to expect on arrival.
//
// ⚠️ ANTI-DOUBLE-COUNT CONTRACT — READ BEFORE YOU TOUCH:
// This is NOT a count of casualties. It NEVER contributes to any
// headline / total / dashboard number. Headline counts stay counts of
// PEOPLE WHO HAVE REPORTED. Summing this into anything would double-count
// every reporter and produce phantom casualties. If you find yourself
// writing arithmetic on `group_size`, stop.
//
// Buckets:
//   "just_me" → the reporter alone (1)
//   "2" | "3" | "4" → exactly that many including the reporter
//   "5_plus" → the reporter plus four or more others (rescuer treats as "at least 5")
//   null / missing → the reporter skipped; render as "unknown group size",
//                    never as "just 1".
export type GroupSize = "just_me" | "2" | "3" | "4" | "5_plus";

export async function submitStatus(opts: {
  status: CheckInStatus;
  severity?: TriageSeverity | null;
  mobility?: Mobility | null;
  egress?: Egress | null;
  // #185: optional group-size bucket. Skippable and never counted.
  group_size?: GroupSize | null;
  location?: LocationPayload;
  battery?: BatteryPayload;
}): Promise<string> {
  const deviceId = await getDeviceId();
  // Best-effort — if AsyncStorage is unavailable we send null and the
  // dashboard falls back to short_code-only display for this check-in.
  const displayName = await getDisplayName().catch(() => null);
  const { status, severity, mobility, egress, group_size, location, battery } = opts;

  const payload: Record<string, any> = {
    deviceId,
    status,
    // severity is only meaningful for `trapped`; backend also enforces this.
    severity: status === "trapped" ? (severity ?? null) : null,
    // mobility ("mobile" | "trapped") is likewise trapped-only; backend
    // normalizer will null it out for other statuses defensively.
    mobility: status === "trapped" ? (mobility ?? null) : null,
    // egress ("can_exit" | "cannot_exit") — trapped-only, asked of green
    // reports. A "cannot_exit" must surface the person as needing extraction
    // even though their injuries are minor.
    egress: status === "trapped" ? (egress ?? null) : null,
    // #185: group_size travels regardless of status. A "safe" reporter at an
    // address with four others is exactly as useful to a rescuer knocking
    // on that door as a trapped one — and this is the only field that
    // tells them. Never used in any count.
    group_size: group_size ?? null,
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
    `[QuakeGuard] enqueue (${status}${severity ? "/" + severity : ""}${mobility ? "/" + mobility : ""}) →`,
    JSON.stringify(payload),
  );

  const kind: QueueItemKind =
    status === "trapped"
      ? "trapped"
      : status === "safe"
        ? "safe"
        : "not_responding";
  const itemId = await enqueueHelp(payload, kind);
  return itemId;
}
