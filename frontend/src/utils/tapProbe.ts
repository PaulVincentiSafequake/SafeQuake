// #208 diagnostic probe (v1.0.40, build 40 — Neo, 2026-08-20 — Paul):
//
// A trapped user tapping a critical earthquake alert from the lock screen
// on iOS lands on the Seismic-activity detail screen instead of the
// check-in screen. Symptom-side we can see it; root cause is invisible to
// us because we do not know what payload keys are actually reaching the
// device. iOS may be dropping keys, coalescing notifications, or the
// alert payload the server sends may already be missing the routing
// discriminators.
//
// The strict directive is: NO routing changes, NO payload fallback
// changes. This module is READ-ONLY logging. It records the last N
// notification taps to AsyncStorage — timestamp, source (foreground
// response listener vs. cold-start getLastNotificationResponseAsync),
// action identifier, raw payload data as delivered, the discriminators
// the routing code cares about, and the route the routing code chose.
//
// The user then reproduces the bug with v1.0.40 installed, opens
// Diagnostics, hits Copy, and pastes the log back. From that we can see
// exactly which key is missing (or misspelled) and fix the CORRECT layer
// — server payload, notification receipt, or tap routing — instead of
// guessing again.

import AsyncStorage from "@react-native-async-storage/async-storage";

const KEY = "diag.tapLog";
const MAX_ENTRIES = 5;

export type TapSource = "response" | "lastResponse";

export interface TapEntry {
  /** ISO-8601 UTC timestamp of when the tap was received by JS. */
  ts: string;
  /** Which listener the tap came through. */
  source: TapSource;
  /**
   * Notification action identifier as delivered by iOS/Android.
   * "com.apple.default.action" or Expo's default is the body tap;
   * "VIEW_MAP" / "CLOSE" / "RECHECK_*" are our custom actions.
   */
  actionIdentifier: string | null;
  /**
   * The raw `data` block of the notification, verbatim from
   * `response.notification.request.content.data`. This is the object
   * the routing code inspects — if a routing key is missing, this is
   * where it will show up as missing.
   */
  rawPayload: Record<string, any>;
  /** Extracted key routing discriminators, for at-a-glance triage. */
  kind: string;
  action_url: string | null;
  hasCheckId: boolean;
  hasMagnitude: boolean;
  hasUnid: boolean;
  /**
   * The route path the router.push/replace call was invoked with — the
   * ONE thing the user actually experiences after the tap. If this
   * ever says "/quake/..." when kind/action_url/magnitude look
   * critical, we have the smoking gun.
   */
  chosenRoute: string;
}

/**
 * Append a tap entry, keeping only the last MAX_ENTRIES. Failures are
 * swallowed — this must never break the notification tap handler.
 */
export async function recordTap(entry: TapEntry): Promise<void> {
  try {
    const existing = await getTapLog();
    // Newest first: easier to read on a small screen.
    const next = [entry, ...existing].slice(0, MAX_ENTRIES);
    await AsyncStorage.setItem(KEY, JSON.stringify(next));
  } catch {
    // Non-fatal — the probe is diagnostic only.
  }
}

/** Returns the log newest-first. Empty array if nothing recorded / on error. */
export async function getTapLog(): Promise<TapEntry[]> {
  try {
    const raw = await AsyncStorage.getItem(KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? (parsed as TapEntry[]) : [];
  } catch {
    return [];
  }
}

/** Wipe the log. Non-fatal on error. */
export async function clearTapLog(): Promise<void> {
  try {
    await AsyncStorage.removeItem(KEY);
  } catch {
    // Non-fatal.
  }
}

/**
 * Helper the tap handler uses to build the extracted discriminator
 * fields consistently. Keeps the routing logic itself untouched — this
 * function only reads.
 */
export function buildLogFields(
  data: Record<string, any>,
): Pick<TapEntry, "kind" | "action_url" | "hasCheckId" | "hasMagnitude" | "hasUnid"> {
  const kind = String(data?.kind ?? data?.type ?? "").trim();
  const action_url =
    typeof data?.action_url === "string" ? data.action_url : null;
  const hasCheckId =
    data?.check_id != null && String(data.check_id).length > 0;
  const hasMagnitude = data?.magnitude != null && data.magnitude !== "";
  const hasUnid = data?.unid != null && String(data.unid).length > 0;
  return { kind, action_url, hasCheckId, hasMagnitude, hasUnid };
}
