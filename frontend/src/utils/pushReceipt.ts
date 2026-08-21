/**
 * #276 (2026-08-21 — Paul): tell the backend when a question actually
 * arrived on this phone.
 *
 *   "The card must distinguish 'the phone received our question and nobody
 *    answered' from 'we cannot confirm the phone ever saw it'. Those are
 *    different facts and only one is worrying."
 *
 * Apple returning 200 means Apple accepted the push. It does not mean the
 * phone showed it. Without this receipt, a notification lost in transit and
 * a person ignoring us look identical on the operator's board — and the
 * operator would send help towards somebody who is fine.
 *
 * Best effort by design. A missing receipt means "we cannot confirm", never
 * "they ignored us", so a failure here costs us caution, not correctness.
 * Nothing here blocks the UI and nothing here throws.
 */
import Constants from "expo-constants";
import * as Notifications from "expo-notifications";
import { Platform } from "react-native";

import { getDeviceId } from "@/src/utils/checkin";

const BACKEND_URL =
  process.env.EXPO_PUBLIC_BACKEND_URL ??
  Constants.expoConfig?.extra?.EXPO_PUBLIC_BACKEND_URL;

/** Questions we are expected to confirm. Anything else is left alone. */
const ASKING_KINDS = ["check_in_request", "recheck", "critical_alert", "quakeguard-reminder"];

// Never send the same receipt twice in one app session.
const sent = new Set<string>();

export async function reportPushSeen(
  data: Record<string, any> | null | undefined,
  how: "shown" | "tapped" | "woke",
): Promise<void> {
  try {
    if (Platform.OS === "web" || !BACKEND_URL || !data) return;
    const kind = String(data.kind ?? "");
    if (!ASKING_KINDS.includes(kind)) return;
    const checkId = data.check_id != null ? String(data.check_id) : null;
    const key = `${kind}:${checkId ?? "none"}:${how}`;
    if (sent.has(key)) return;
    sent.add(key);

    const deviceId = await getDeviceId();
    await fetch(`${BACKEND_URL}/api/push/receipt`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        device_id: deviceId,
        check_id: checkId,
        kind,
        how,
        seen_at: new Date().toISOString(),
      }),
    });
  } catch {
    // Silence here is the honest outcome: the operator's card will say we
    // cannot confirm the question arrived, which is exactly true.
  }
}

/**
 * On launch and on return to the foreground, confirm anything of ours still
 * sitting in Notification Centre. This catches the case where the phone
 * received the question hours ago while the app was closed.
 */
export async function reportPresentedPushes(): Promise<void> {
  try {
    if (Platform.OS === "web") return;
    const presented = await Notifications.getPresentedNotificationsAsync();
    for (const n of presented) {
      const data = (n.request?.content?.data ?? {}) as Record<string, any>;
      await reportPushSeen(data, "shown");
    }
  } catch {
    /* best effort */
  }
}
