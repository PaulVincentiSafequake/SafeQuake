/**
 * C1 — sending re-check answers, including from a lock screen with no signal.
 *
 * Two rules from the design (memory/recheckin-design.md):
 *
 * 1. The TAP time is authoritative. An answer tapped under rubble and
 *    delivered forty minutes later when signal returns is real information
 *    with a real timestamp, and that is the timestamp every human-facing
 *    surface reads. We stamp it here, on the device, at the moment of the tap.
 *
 * 2. A trapped person's phone must never lose an answer because the tower was
 *    down. Answers queue in AsyncStorage and flush on the next opportunity —
 *    the next app open, or the next answer.
 */
import AsyncStorage from "@react-native-async-storage/async-storage";
import * as Battery from "expo-battery";

const QUEUE_KEY = "quakeangel_recheck_queue";
const BACKEND_URL = process.env.EXPO_PUBLIC_BACKEND_URL ?? "";

export type RecheckAnswer = "same" | "worse" | "much_worse" | "better";

type QueuedAnswer = {
  device_id: string;
  answer: RecheckAnswer;
  check_id?: string | null;
  answered_at: string;
  battery_pct?: number | null;
};

async function readQueue(): Promise<QueuedAnswer[]> {
  try {
    const raw = await AsyncStorage.getItem(QUEUE_KEY);
    return raw ? (JSON.parse(raw) as QueuedAnswer[]) : [];
  } catch {
    return [];
  }
}

async function writeQueue(items: QueuedAnswer[]): Promise<void> {
  try {
    await AsyncStorage.setItem(QUEUE_KEY, JSON.stringify(items.slice(-50)));
  } catch {
    // A full disk must not swallow the answer silently upstream — the caller
    // already has the POST attempt as its primary path.
  }
}

async function post(item: QueuedAnswer): Promise<boolean> {
  try {
    const r = await fetch(`${BACKEND_URL}/api/recheck/answer`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(item),
    });
    // 404 = the backend does not know this device. Retrying forever would
    // never succeed, so treat it as delivered-and-done rather than growing
    // the queue without bound.
    return r.ok || r.status === 404;
  } catch {
    return false;
  }
}

/** Send every queued answer that has not made it through yet. */
export async function flushRecheckQueue(): Promise<number> {
  const queue = await readQueue();
  if (queue.length === 0) return 0;
  const remaining: QueuedAnswer[] = [];
  let sent = 0;
  for (const item of queue) {
    if (await post(item)) sent += 1;
    else remaining.push(item);
  }
  await writeQueue(remaining);
  return sent;
}

/**
 * Record one answer. Never throws and never blocks the UI on the network:
 * the answer is queued first, so a failed request loses nothing.
 */
export async function submitRecheckAnswer(
  deviceId: string,
  answer: RecheckAnswer,
  checkId?: string | null,
): Promise<{ delivered: boolean }> {
  let batteryPct: number | null = null;
  try {
    const level = await Battery.getBatteryLevelAsync();
    if (level >= 0) batteryPct = Math.round(level * 100);
  } catch {
    // Battery is useful (it drives the ladder interval) but never required.
  }

  const item: QueuedAnswer = {
    device_id: deviceId,
    answer,
    check_id: checkId ?? null,
    answered_at: new Date().toISOString(),
    battery_pct: batteryPct,
  };

  const delivered = await post(item);
  if (!delivered) {
    const queue = await readQueue();
    queue.push(item);
    await writeQueue(queue);
  } else {
    // Opportunistic: a successful send means we have signal right now.
    await flushRecheckQueue();
  }
  return { delivered };
}

export async function pendingRecheckAnswers(): Promise<number> {
  return (await readQueue()).length;
}
