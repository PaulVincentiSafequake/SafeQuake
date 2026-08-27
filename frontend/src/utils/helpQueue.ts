// Persistent offline queue for status check-ins ("I'm Safe" / "I need help").
//
// #193 (2026-08-30 — Paul): the worst failure this app can have is a person
// tapping "I need help" with no signal, seeing the report as sent, and
// nobody ever knowing they exist. This module makes that impossible:
//
//   1. Every check-in is written to persistent storage BEFORE any network
//      call. If the phone dies or is thrown across the room, the report
//      is still in AsyncStorage and will retry on next launch.
//
//   2. The truth signal is HTTP 2xx from OUR backend (/api/status). The
//      legacy Render endpoint is still dual-posted for the old rescuer
//      view, but its success does NOT count as "reached us" — the rescuer
//      dashboard is fed from our backend, so that is where confirmation
//      must come from.
//
//   3. Retry runs forever: 2s, 5s, 10s, 30s, 60s, 120s, then every 5 min.
//      Also kicks on app foreground and on any explicit user tap.
//
//   4. Consumers subscribe to queue events and get told the moment a
//      specific report is CONFIRMED. Until that moment, nothing in the
//      UI may say "sent", show a tick, or turn green.
//
// The rule this module enforces (from Paul's brief):
//   "The phone must hold onto that report and keep trying until our
//    server confirms it has really arrived. Until then, the person must
//    never see a tick, a green mark, or any wording suggesting it was
//    sent. It should honestly say it's still trying, and tell them
//    plainly the moment it gets through."

import AsyncStorage from "@react-native-async-storage/async-storage";
import { AppState, type AppStateStatus } from "react-native";
import Constants from "expo-constants";

import { markBackendContact } from "@/src/utils/readiness";
import { SAFE_ENDPOINT } from "@/src/theme";

const QUEUE_KEY = "qa_help_queue_v1";
// Retry schedule in ms. After the last entry we keep retrying at the last
// interval forever. Chosen so the first four attempts finish inside a
// minute (the user is watching the screen), and after that we back off to
// once every 5 minutes (the app is likely in the background but retry
// still needs to fire).
const RETRY_SCHEDULE_MS = [
  2_000,
  5_000,
  10_000,
  30_000,
  60_000,
  120_000,
  300_000,
];
const MAX_INTERVAL_MS = RETRY_SCHEDULE_MS[RETRY_SCHEDULE_MS.length - 1];

// Per-request network timeout — a socket that hangs forever without either
// a response or an error would starve the queue.
const FETCH_TIMEOUT_MS = 12_000;

const BACKEND_URL =
  process.env.EXPO_PUBLIC_BACKEND_URL ??
  (Constants.expoConfig?.extra?.EXPO_PUBLIC_BACKEND_URL as string | undefined);

export type QueueItemKind = "safe" | "trapped" | "not_responding";

export interface QueueItem {
  /** Client-generated stable ID. Persisted with the item so UI subscribers
   *  can track "their" report across app restarts. */
  id: string;
  /** The exact JSON payload we will POST. Snapshot at enqueue time so a
   *  later retry sends the same body — no field the user has since
   *  changed, no location we no longer have. */
  payload: Record<string, any>;
  kind: QueueItemKind;
  /** ISO timestamp of when the user first tapped. */
  enqueued_at: string;
  attempts: number;
  /** ISO timestamp of the last delivery attempt. null if never tried. */
  last_attempt_at: string | null;
  /** Human-readable last error, for the diagnostic screen. Never surfaced
   *  as failure copy — the user only sees "still trying" until we win. */
  last_error: string | null;
  /** ISO timestamp of the moment the backend returned 2xx. When non-null
   *  the item is CONFIRMED and eligible for removal from the queue. */
  confirmed_at: string | null;
}

type Listener = (items: QueueItem[]) => void;

let memoryQueue: QueueItem[] = [];
let hydrated = false;
let hydratePromise: Promise<void> | null = null;
const listeners = new Set<Listener>();

let flushTimer: ReturnType<typeof setTimeout> | null = null;
let flushing = false;
// Track the last time we scheduled the next flush so a manual kick doesn't
// double up on top of an already-scheduled attempt.
let nextFlushAt: number | null = null;

let appStateSub: { remove: () => void } | null = null;
let backgroundLoopStarted = false;

function genId(): string {
  // Timestamp + random suffix is enough here — this is a per-device
  // identifier used only within this queue; there's no cross-device
  // collision surface.
  return `hq-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
}

async function hydrate(): Promise<void> {
  if (hydrated) return;
  if (hydratePromise) return hydratePromise;
  hydratePromise = (async () => {
    try {
      const raw = await AsyncStorage.getItem(QUEUE_KEY);
      if (raw) {
        const parsed = JSON.parse(raw);
        if (Array.isArray(parsed)) {
          // Drop any item that somehow lost required fields — defensive
          // against a corrupt store.
          memoryQueue = parsed.filter(
            (it) =>
              it &&
              typeof it.id === "string" &&
              it.payload &&
              typeof it.payload === "object" &&
              (it.kind === "safe" ||
                it.kind === "trapped" ||
                it.kind === "not_responding"),
          );
        }
      }
    } catch (e) {
      console.log("[helpQueue] hydrate failed:", (e as Error)?.message);
    } finally {
      hydrated = true;
    }
  })();
  return hydratePromise;
}

async function persist(): Promise<void> {
  try {
    await AsyncStorage.setItem(QUEUE_KEY, JSON.stringify(memoryQueue));
  } catch (e) {
    console.log("[helpQueue] persist failed:", (e as Error)?.message);
  }
}

function emit(): void {
  // Snapshot copy so subscribers can safely mutate their local state.
  const snap = memoryQueue.map((it) => ({ ...it }));
  for (const l of Array.from(listeners)) {
    try {
      l(snap);
    } catch (e) {
      console.log("[helpQueue] listener threw:", (e as Error)?.message);
    }
  }
}

/** Register a subscriber. Fires synchronously with the current queue and
 *  then again on every change. Returns an unsubscribe function. */
export function subscribe(cb: Listener): () => void {
  listeners.add(cb);
  // Best-effort immediate emit (may be pre-hydrate — subscribers will get
  // a second emit once hydration finishes).
  try {
    cb(memoryQueue.map((it) => ({ ...it })));
  } catch {
    // ignore
  }
  hydrate().then(() => {
    try {
      cb(memoryQueue.map((it) => ({ ...it })));
    } catch {
      // ignore
    }
  });
  return () => {
    listeners.delete(cb);
  };
}

/** Read the current queue (post-hydration). */
export async function getQueue(): Promise<QueueItem[]> {
  await hydrate();
  return memoryQueue.map((it) => ({ ...it }));
}

/** Enqueue a new item and kick a flush. Returns the item ID so the caller
 *  can subscribe to updates for that specific report. */
export async function enqueue(
  payload: Record<string, any>,
  kind: QueueItemKind,
): Promise<string> {
  await hydrate();
  const item: QueueItem = {
    id: genId(),
    payload,
    kind,
    enqueued_at: new Date().toISOString(),
    attempts: 0,
    last_attempt_at: null,
    last_error: null,
    confirmed_at: null,
  };
  memoryQueue.push(item);
  await persist();
  emit();
  // Kick a flush immediately.
  scheduleFlush(0);
  return item.id;
}

/** Find one queue item by ID. Used by UI subscribers that only care about
 *  a single report. */
export function findItem(id: string): QueueItem | null {
  const it = memoryQueue.find((x) => x.id === id);
  return it ? { ...it } : null;
}

/** Remove an item that has already been confirmed. Used by consumers that
 *  want to clear the "delivered" state after showing the toast. */
export async function removeItem(id: string): Promise<void> {
  await hydrate();
  const before = memoryQueue.length;
  memoryQueue = memoryQueue.filter((x) => x.id !== id);
  if (memoryQueue.length !== before) {
    await persist();
    emit();
  }
}

function fetchWithTimeout(
  url: string,
  init: RequestInit,
  timeoutMs = FETCH_TIMEOUT_MS,
): Promise<Response> {
  return new Promise((resolve, reject) => {
    const t = setTimeout(() => {
      reject(new Error(`timeout_${timeoutMs}ms`));
    }, timeoutMs);
    fetch(url, init)
      .then((r) => {
        clearTimeout(t);
        resolve(r);
      })
      .catch((e) => {
        clearTimeout(t);
        reject(e);
      });
  });
}

/** Attempt delivery of a single queue item. Returns whether it was
 *  confirmed on this pass. */
async function attempt(item: QueueItem): Promise<boolean> {
  const body = JSON.stringify(item.payload);
  // Fire the legacy Render endpoint fire-and-forget in parallel — its
  // success/failure does NOT gate confirmation, but keeping the old
  // pathway alive avoids regressing anything that still reads it.
  try {
    fetchWithTimeout(SAFE_ENDPOINT, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body,
    }).catch(() => {
      // legacy endpoint failure is not fatal here.
    });
  } catch {
    // ignore — outer catch is only for the truth call
  }

  // The truth call: our backend.
  if (!BACKEND_URL) {
    // No backend URL means the client build is misconfigured. Treat as a
    // recoverable failure — we'll retry, and if the user reinstalls
    // against a fixed build the retry will eventually succeed.
    return false;
  }
  try {
    const res = await fetchWithTimeout(`${BACKEND_URL}/api/status`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body,
    });
    if (res.ok) {
      // 2xx = the server has really received it. Mark confirmed.
      const now = new Date().toISOString();
      const idx = memoryQueue.findIndex((x) => x.id === item.id);
      if (idx >= 0) {
        memoryQueue[idx] = {
          ...memoryQueue[idx],
          confirmed_at: now,
          last_attempt_at: now,
          last_error: null,
          attempts: memoryQueue[idx].attempts + 1,
        };
      }
      // Signal to readiness that the backend contacted us successfully.
      try {
        markBackendContact();
      } catch {
        // ignore
      }
      return true;
    }
    // Non-2xx (4xx / 5xx) — record and retry. 4xx is technically the
    // server telling us "this will never succeed", but we still retry
    // because a 4xx during a genuine outage is often a proxy error page,
    // not the app rejecting the payload. Better to keep trying than to
    // silently drop a help request.
    const idx = memoryQueue.findIndex((x) => x.id === item.id);
    if (idx >= 0) {
      memoryQueue[idx] = {
        ...memoryQueue[idx],
        last_attempt_at: new Date().toISOString(),
        last_error: `HTTP ${res.status}`,
        attempts: memoryQueue[idx].attempts + 1,
      };
    }
    return false;
  } catch (e: any) {
    const idx = memoryQueue.findIndex((x) => x.id === item.id);
    if (idx >= 0) {
      memoryQueue[idx] = {
        ...memoryQueue[idx],
        last_attempt_at: new Date().toISOString(),
        last_error: String(e?.message ?? e ?? "network_error"),
        attempts: memoryQueue[idx].attempts + 1,
      };
    }
    return false;
  }
}

async function flushOnce(): Promise<void> {
  if (flushing) return;
  flushing = true;
  try {
    await hydrate();
    // Only attempt items that are NOT yet confirmed. Confirmed items are
    // kept in the queue briefly so UI subscribers can pick them up; the
    // consumer of the "delivered" event calls removeItem() when done.
    const pending = memoryQueue.filter((x) => !x.confirmed_at);
    if (pending.length === 0) {
      nextFlushAt = null;
      return;
    }
    let anyProgress = false;
    for (const it of pending) {
      // Re-fetch from memoryQueue because a previous attempt in this loop
      // may have mutated it.
      const live = memoryQueue.find((x) => x.id === it.id);
      if (!live || live.confirmed_at) continue;
      const ok = await attempt(live);
      if (ok) anyProgress = true;
    }
    await persist();
    emit();
    // If anything is still unconfirmed, schedule the next flush.
    const remaining = memoryQueue.filter((x) => !x.confirmed_at);
    if (remaining.length > 0) {
      const maxAttempts = Math.max(...remaining.map((x) => x.attempts));
      const delay =
        RETRY_SCHEDULE_MS[
          Math.min(maxAttempts, RETRY_SCHEDULE_MS.length - 1)
        ] ?? MAX_INTERVAL_MS;
      scheduleFlush(delay);
    } else {
      nextFlushAt = null;
    }
    // Silence unused warning — anyProgress is intentionally computed for
    // possible telemetry hooks later.
    void anyProgress;
  } finally {
    flushing = false;
  }
}

function scheduleFlush(delayMs: number): void {
  const wantAt = Date.now() + delayMs;
  // If a flush is already scheduled to run at or before wantAt, do nothing.
  if (flushTimer && nextFlushAt != null && nextFlushAt <= wantAt) {
    return;
  }
  if (flushTimer) {
    clearTimeout(flushTimer);
    flushTimer = null;
  }
  nextFlushAt = wantAt;
  flushTimer = setTimeout(() => {
    flushTimer = null;
    nextFlushAt = null;
    void flushOnce();
  }, Math.max(0, delayMs));
}

/** Explicit user-initiated kick — e.g., a "Try now" button. Also called
 *  automatically on AppState -> active. */
export function kickFlush(): void {
  scheduleFlush(0);
}

/** Start the background loop. Idempotent. Should be called from the app
 *  root on boot so retries continue even if the user is not on /alert. */
export function startBackgroundLoop(): void {
  if (backgroundLoopStarted) return;
  backgroundLoopStarted = true;

  // On app foreground, kick a flush immediately — that's when a phone
  // that regained signal is most likely to be able to reach us.
  const onAppState = (state: AppStateStatus) => {
    if (state === "active") {
      hydrate().then(() => {
        const pending = memoryQueue.some((x) => !x.confirmed_at);
        if (pending) kickFlush();
      });
    }
  };
  appStateSub = AppState.addEventListener("change", onAppState);

  // Kick once on boot in case there is a report left over from a previous
  // session that never made it through.
  hydrate().then(() => {
    const pending = memoryQueue.some((x) => !x.confirmed_at);
    if (pending) kickFlush();
  });
}

/** Stop the background loop. Test-only. */
export function _stopBackgroundLoopForTests(): void {
  if (appStateSub) {
    appStateSub.remove();
    appStateSub = null;
  }
  if (flushTimer) {
    clearTimeout(flushTimer);
    flushTimer = null;
    nextFlushAt = null;
  }
  backgroundLoopStarted = false;
}

/** Test-only: wipe both memory and persisted state. */
export async function _resetForTests(): Promise<void> {
  memoryQueue = [];
  hydrated = false;
  hydratePromise = null;
  try {
    await AsyncStorage.removeItem(QUEUE_KEY);
  } catch {
    // ignore
  }
}
