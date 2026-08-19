/**
 * #208 (Batch 7 R4) — Active-alert state.
 *
 * There is currently no way, on a build 130 phone, to get from a
 * received earthquake alert to the check-in screen if you don't tap
 * the notification. This module fixes the second half of that: opening
 * the app while an alert is live and unanswered lands you on the
 * check-in screen, not the home screen.
 *
 *   PRIMARY BUG (Paul, 2026-08-19 night):
 *     "Route 2 — opening the app directly, with the alert still live.
 *      Lands on the normal home screen ... No indication anywhere that
 *      an earthquake alert is currently active and unanswered."
 *
 * Rules the design is built to:
 *   - An unanswered alert is the most important thing in the app's world
 *     at that moment; the home screen must not hide it.
 *   - Answering (safe / trapped / rescued) is the ONLY way to clear it.
 *     Swiping the notification away, restarting the phone, killing the
 *     app, quiet hours, or the siren timing out (D1 #250) MUST NOT clear
 *     it. Silence is not an answer — rule 9.2.
 *   - The record is stored on-device (AsyncStorage) so it survives a
 *     cold start without waiting on a server call.
 *   - "Live" means: within a bounded window (default 12h) since the
 *     alert arrived. After that we consider the incident stale for the
 *     purposes of home-screen redirect only — the person can still
 *     answer via the notification if it's still there.
 *
 * What we DO NOT do here:
 *   - Any UI. This is state + accessors.
 *   - Any siren. Siren is /alert's job.
 *   - Any network. This state is local and durable.
 */

import AsyncStorage from "@react-native-async-storage/async-storage";

const KEY = "quakeguard.activeAlert.v1";

/**
 * Alerts older than this fall out of "redirect on home" but the answer
 * screen remains reachable via the notification if it's still pending.
 * 12h is long enough to cover an alert that arrives at bedtime, a
 * night's sleep, and the morning routine — and short enough that a
 * week-old test alert doesn't ambush the person opening the app.
 */
const REDIRECT_WINDOW_MS = 12 * 60 * 60 * 1000;

export type ActiveAlert = {
  /** ISO timestamp when the alert was received on this device. */
  received_at: string;
  /** From the payload — used to render magnitude on /alert. */
  magnitude: number | null;
  distance_km: number | null;
  intensity: string | null;
  region: string | null;
  unid: string | null;
  /**
   * The kind that put us into this state. "critical_alert" for a real
   * earthquake alert; "quakeguard-reminder" for the post-quake check-in
   * reminder (also unanswered, also should redirect).
   */
  kind: "critical_alert" | "quakeguard-reminder";
};

/**
 * Record that a new alert has arrived. Called from the notification
 * received listener AND the tap handler — both, because we don't know
 * whether the user tapped or the notification was delivered to a
 * foreground app. Idempotent on the same (received_at, unid) pair.
 */
export async function markAlertActive(a: Omit<ActiveAlert, "received_at"> & {
  received_at?: string;
}): Promise<void> {
  const record: ActiveAlert = {
    received_at: a.received_at ?? new Date().toISOString(),
    magnitude: a.magnitude ?? null,
    distance_km: a.distance_km ?? null,
    intensity: a.intensity ?? null,
    region: a.region ?? null,
    unid: a.unid ?? null,
    kind: a.kind,
  };
  try {
    await AsyncStorage.setItem(KEY, JSON.stringify(record));
  } catch {
    // AsyncStorage failure is non-fatal — the notification tap path
    // still works, we just lose the "cold-open redirect" benefit.
  }
}

/** Called when the user submits a status (safe / trapped / rescued). */
export async function clearActiveAlert(): Promise<void> {
  try { await AsyncStorage.removeItem(KEY); } catch { /* non-fatal */ }
}

/**
 * Read the current active alert, if any.
 * Returns null when nothing is active OR the record is corrupt.
 */
export async function getActiveAlert(): Promise<ActiveAlert | null> {
  try {
    const raw = await AsyncStorage.getItem(KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as ActiveAlert;
    if (!parsed || typeof parsed !== "object") return null;
    return parsed;
  } catch {
    return null;
  }
}

/**
 * Should the home screen redirect to /alert right now?
 *
 * Yes if:
 *   - There IS an active alert record, AND
 *   - It is within REDIRECT_WINDOW_MS of now.
 *
 * The window check is deliberately here (not in `getActiveAlert`) so
 * a stale record can still be inspected (e.g. from Diagnostics), but
 * won't ambush someone opening the app hours later.
 */
export async function shouldRedirectToAlert(): Promise<ActiveAlert | null> {
  const a = await getActiveAlert();
  if (!a) return null;
  const receivedMs = Date.parse(a.received_at);
  if (Number.isNaN(receivedMs)) return null;
  const ageMs = Date.now() - receivedMs;
  if (ageMs < 0 || ageMs > REDIRECT_WINDOW_MS) return null;
  return a;
}

/** URL search string suitable for router.replace("/alert" + toQuery(...)). */
export function toAlertQuery(a: ActiveAlert): string {
  const p = new URLSearchParams();
  // siren=0 on cold-open — the sirenfired at time of receipt.
  // Reopening the app minutes later must not fire it again.
  p.set("siren", "0");
  p.set("reopen", "1");
  if (a.magnitude != null) p.set("magnitude", String(a.magnitude));
  if (a.distance_km != null) p.set("distance_km", String(a.distance_km));
  if (a.intensity != null) p.set("intensity", String(a.intensity));
  if (a.region != null) p.set("region", String(a.region));
  if (a.unid != null) p.set("unid", String(a.unid));
  return "?" + p.toString();
}
