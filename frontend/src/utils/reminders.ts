import * as Notifications from "expo-notifications";
import { Platform } from "react-native";

// Foreground behaviour: while the app is open, still show the banner + play
// sound (this is what makes reminders actually visible when the user is
// staring at the alert screen).
Notifications.setNotificationHandler({
  handleNotification: async () => ({
    shouldShowBanner: true,
    shouldShowList: true,
    shouldPlaySound: true,
    shouldSetBadge: false,
  }),
});

const CHANNEL_ID = "quakeguard-critical";
const REMINDER_TAG = "quakeguard-checkin-reminder";
// Persistent lock-screen card shown after a successful "trapped" submission.
// Carries the victim's short code + first name so a responder picking up an
// unconscious person's locked phone can match it to a pin on the dashboard
// without unlocking. Uses a fixed identifier so re-submissions replace, not
// stack. Cancelled on I'm Safe / dismiss.
const RESCUE_INFO_ID = "quakeangel-rescue-info";
const RESCUE_INFO_CHANNEL_ID = "quakeangel-rescue-info";

export async function ensureNotificationSetup(): Promise<boolean> {
  // Android channel — MAX importance so it makes sound and bypasses DND when
  // the user has granted the "priority" permission.
  if (Platform.OS === "android") {
    try {
      await Notifications.setNotificationChannelAsync(CHANNEL_ID, {
        name: "Earthquake safety reminders",
        importance: Notifications.AndroidImportance.MAX,
        sound: "default",
        vibrationPattern: [0, 400, 300, 400],
        bypassDnd: true,
        lockscreenVisibility:
          Notifications.AndroidNotificationVisibility.PUBLIC,
      });
    } catch {
      // ignore
    }
  }

  try {
    const current = await Notifications.getPermissionsAsync();
    if (current.granted) return true;
    if (!current.canAskAgain) return false;

    const req = await Notifications.requestPermissionsAsync({
      ios: {
        // Apple has approved the com.apple.developer.usernotifications.
        // critical-alerts entitlement for this app — reminders now bypass
        // the physical silent switch, DND, and Focus modes.
        allowAlert: true,
        allowSound: true,
        allowBadge: false,
        allowCriticalAlerts: true,
        allowProvisional: false,
      },
      android: {},
    });
    return req.granted || req.status === "granted";
  } catch {
    return false;
  }
}

// Schedule N staggered reminders (60s, 150s, 240s, ...) so the user gets
// nagged every ~90s until they tap I'm Safe. iOS local notifications don't
// support open-ended repeating with a custom interval, so we schedule a small
// batch and top up as needed.
//
// #207 / #296 (2026-08-24 — Paul, live test): only the FIRST reminder is a
// Critical Alert. The rest are `time-sensitive`.
//
// Why: Critical Alerts ignore the physical silent switch and play at full
// volume regardless of what the user has chosen. That is the right trade for
// the alert itself, and for ONE reminder in case they slept through it. It is
// the wrong trade eight times over: Paul received four in a row on one phone
// during testing, which drains the battery, risks the entitlement Apple
// granted us through visible overuse, and — worst of all — teaches people
// that a Critical Alert from this app can safely be ignored. The one alarm
// that must never be ignored is the one they learn to.
//
// `time-sensitive` still breaks through Focus and Do Not Disturb, so a
// reminder still reaches someone who is not looking at their phone. Same
// doctrine as the trapped re-checks in backend/apns.py.
export async function scheduleCheckInReminders(
  count = 8,
  everySeconds = 90,
  firstDelaySeconds = 60,
): Promise<string[]> {
  const ids: string[] = [];
  for (let i = 0; i < count; i++) {
    const seconds = firstDelaySeconds + i * everySeconds;
    try {
      const id = await Notifications.scheduleNotificationAsync({
        identifier: `${REMINDER_TAG}-${i}`,
        content: {
          title: "Are you safe?",
          body: "Earthquake alert active. Tap to open Quake Angel and mark yourself safe.",
          sound: "default",
          interruptionLevel: i === 0 ? "critical" : "timeSensitive",
          data: { kind: "quakeguard-reminder", action_url: "/alert" },
          ...(Platform.OS === "android" && {
            channelId: CHANNEL_ID,
            priority: Notifications.AndroidNotificationPriority.MAX,
            vibrate: [0, 400, 300, 400],
          }),
        },
        trigger: {
          type: Notifications.SchedulableTriggerInputTypes.TIME_INTERVAL,
          seconds,
          repeats: false,
          channelId: Platform.OS === "android" ? CHANNEL_ID : undefined,
        } as Notifications.TimeIntervalTriggerInput,
      });
      ids.push(id);
    } catch (e) {
      console.log("[QuakeGuard] schedule reminder failed:", (e as Error)?.message);
    }
  }
  return ids;
}

export async function cancelCheckInReminders(): Promise<void> {
  try {
    const scheduled = await Notifications.getAllScheduledNotificationsAsync();
    await Promise.all(
      scheduled
        .filter((n) => n.identifier?.startsWith(REMINDER_TAG))
        .map((n) => Notifications.cancelScheduledNotificationAsync(n.identifier)),
    );
  } catch {
    // ignore
  }

  // Surgically dismiss only reminder-tagged notifications that already fired.
  // We used to call dismissAllNotificationsAsync() here, but that wiped the
  // persistent rescue-info card too — which must survive I'm-Safe / dismiss
  // / home-navigation so responders can still read it off the lock screen.
  try {
    const presented = await Notifications.getPresentedNotificationsAsync();
    await Promise.all(
      presented
        .filter((n) => {
          const id = n.request?.identifier;
          return typeof id === "string" && id.startsWith(REMINDER_TAG);
        })
        .map((n) =>
          Notifications.dismissNotificationAsync(n.request.identifier),
        ),
    );
  } catch {
    // ignore — dismissal is best-effort; the reminders will eventually
    // fall out of the notification center on their own.
  }
}

/**
 * Post a persistent lock-screen notification carrying the victim's rescue
 * short code + optional first name. Fired the moment a "trapped" check-in
 * is confirmed, so a responder who picks up the phone (possibly locked, on
 * an unconscious person) can glance at the lock screen and match it to the
 * corresponding pin on the dashboard.
 *
 * Behavior by platform:
 * - iOS: interruptionLevel="passive" so it does NOT wake the screen or make
 *   sound (the triage siren has already served its purpose — this is just a
 *   sticky info card). Appears in Notification Center and on the lock
 *   screen until the user clears it. NOT a critical alert.
 * - Android: MAX-importance channel with `sticky:true` so it stays pinned
 *   at the top of the notification shade and can't be swiped away until
 *   we cancel it programmatically (I'm Safe / dismiss).
 *
 * Idempotent — re-calling replaces the previous card in place.
 */
export async function postRescueInfoNotification(
  shortCode: string,
  displayName: string | null,
): Promise<void> {
  if (!shortCode) return;

  if (Platform.OS === "android") {
    try {
      await Notifications.setNotificationChannelAsync(RESCUE_INFO_CHANNEL_ID, {
        name: "Rescue info card",
        importance: Notifications.AndroidImportance.MAX,
        // No sound / vibration — this is a persistent info card, not an alert.
        sound: undefined,
        vibrationPattern: undefined,
        lockscreenVisibility:
          Notifications.AndroidNotificationVisibility.PUBLIC,
      });
    } catch {
      // ignore — fall through with default channel
    }
  }

  const title = displayName ? `Rescue Info · ${displayName}` : "Rescue Info";
  const body = `Rescue Code: ${shortCode}\nShow this to first responders.`;

  try {
    // Dismiss any previously-fired copy so the OS shows the freshest values.
    // We intentionally do NOT dismissAll — that would wipe unrelated
    // Quake Angel notifications (e.g. active reminders).
    try {
      await Notifications.dismissNotificationAsync(RESCUE_INFO_ID);
    } catch {
      // ignore — nothing to dismiss
    }

    await Notifications.scheduleNotificationAsync({
      identifier: RESCUE_INFO_ID,
      content: {
        title,
        body,
        // iOS "passive" = shows in Notification Center + on lock screen when
        // locked, without lighting the screen or playing sound. Exactly the
        // sticky-info-card behavior we want after the trapped confirmation
        // has already displayed on the app screen.
        interruptionLevel: "passive",
        // Data payload so a tap can deep-link into the app if we later add a
        // handler — not required for the lock-screen-visibility win.
        data: {
          kind: "quakeangel-rescue-info",
          short_code: shortCode,
          display_name: displayName ?? null,
          action_url: "/alert",
        },
        ...(Platform.OS === "android" && {
          channelId: RESCUE_INFO_CHANNEL_ID,
          sticky: true,
          priority: Notifications.AndroidNotificationPriority.HIGH,
        }),
      },
      // Immediate delivery.
      trigger: null,
    });
  } catch (e) {
    console.log(
      "[QuakeAngel] postRescueInfoNotification failed:",
      (e as Error)?.message,
    );
  }
}

/**
 * Cancel the persistent rescue-info card. Call this from I'm Safe (the
 * person is no longer trapped, so the code is stale) and from Dismiss
 * (they're leaving the alert flow entirely). Not called during the
 * trapped submission itself — the whole point is that it survives.
 */
export async function cancelRescueInfoNotification(): Promise<void> {
  try {
    await Notifications.cancelScheduledNotificationAsync(RESCUE_INFO_ID);
  } catch {
    // ignore — may not be scheduled
  }
  try {
    await Notifications.dismissNotificationAsync(RESCUE_INFO_ID);
  } catch {
    // ignore — may not be presented
  }
}

// ─────────────────────────────────────────────────────────────────
// #193 (2026-08-30 — Paul): local notifications for a queued help
// report that has NOT yet reached our server.
//
//   "…the phone must hold onto that report and keep trying until our
//    server confirms it has really arrived. Until then, the person must
//    never see a tick, a green mark, or any wording suggesting it was
//    sent. It should honestly say it's still trying, and tell them
//    plainly the moment it gets through."
//
// Two identifiers:
//   HELP_PENDING_ID   — the "not through yet" card. Passive on iOS
//                       (no wake, no sound), MAX-importance sticky on
//                       Android. Persists across app close, so if the
//                       user locks the phone right after tapping and
//                       forgets, the pending state is still visible.
//   HELP_DELIVERED_ID — a one-shot "we've reached the rescue team"
//                       card. Default sound, active interruption on
//                       iOS — this IS a moment we want the user to
//                       notice, even if they're on another screen.
//
// The pair replaces one another: posting delivered cancels pending
// (handled by the caller in _layout.tsx).
const HELP_PENDING_ID = "quakeangel-help-pending";
const HELP_DELIVERED_ID = "quakeangel-help-delivered";
const HELP_CHANNEL_ID = "quakeangel-help-status";

async function ensureHelpChannel(): Promise<void> {
  if (Platform.OS !== "android") return;
  try {
    await Notifications.setNotificationChannelAsync(HELP_CHANNEL_ID, {
      name: "Help request delivery",
      importance: Notifications.AndroidImportance.HIGH,
      sound: "default",
      // No aggressive vibration on the pending card — the whole point is
      // to be visible without adding pressure to someone already
      // panicked. The delivered card gets default sound only.
      vibrationPattern: [0, 200],
      lockscreenVisibility:
        Notifications.AndroidNotificationVisibility.PUBLIC,
    });
  } catch {
    // ignore
  }
}

/** Post/refresh the persistent "still trying" card. Safe to call
 *  multiple times — replaces in place. */
export async function postHelpPendingNotification(): Promise<void> {
  await ensureHelpChannel();
  try {
    await Notifications.dismissNotificationAsync(HELP_PENDING_ID);
  } catch {
    // ignore — nothing to dismiss
  }
  try {
    await Notifications.scheduleNotificationAsync({
      identifier: HELP_PENDING_ID,
      content: {
        // #321 (2026-08-31 — Paul): use the same wording as the alert
        // screen's pending button — "Not sent yet". It is true whether
        // the fault is the user's signal or our server, and states no
        // cause we don't know.
        title: "Not sent yet",
        body:
          "Your phone has not been able to reach the rescue team yet. It will keep trying and tell you the moment it gets through.",
        // Passive on iOS — do NOT wake the screen or play sound. This
        // must be visible without adding fresh panic. If the user has
        // no signal, another sound achieves nothing.
        interruptionLevel: "passive",
        sound: null as unknown as string,
        data: { kind: "quakeangel-help-pending", action_url: "/alert" },
        ...(Platform.OS === "android" && {
          channelId: HELP_CHANNEL_ID,
          sticky: true,
          priority: Notifications.AndroidNotificationPriority.HIGH,
        }),
      },
      trigger: null,
    });
  } catch (e) {
    console.log(
      "[QuakeAngel] postHelpPendingNotification failed:",
      (e as Error)?.message,
    );
  }
}

/** Post the one-shot "reached the rescue team" card. */
export async function postHelpDeliveredNotification(): Promise<void> {
  await ensureHelpChannel();
  try {
    await Notifications.dismissNotificationAsync(HELP_DELIVERED_ID);
  } catch {
    // ignore
  }
  try {
    await Notifications.scheduleNotificationAsync({
      identifier: HELP_DELIVERED_ID,
      content: {
        title: "Your help request reached the rescue team",
        body:
          "The rescue team now knows you need help. Stay calm and save your battery.",
        // Active on iOS — this is the moment the user has been waiting
        // for. We want them to notice even from another app.
        interruptionLevel: "active",
        sound: "default",
        data: { kind: "quakeangel-help-delivered", action_url: "/alert" },
        ...(Platform.OS === "android" && {
          channelId: HELP_CHANNEL_ID,
          priority: Notifications.AndroidNotificationPriority.HIGH,
        }),
      },
      trigger: null,
    });
  } catch (e) {
    console.log(
      "[QuakeAngel] postHelpDeliveredNotification failed:",
      (e as Error)?.message,
    );
  }
}

/** Cancel any pending "still trying" card. Fired the moment the queue
 *  reports the item has been confirmed (i.e., delivered notification is
 *  about to fire) or the user manually clears state. */
export async function cancelHelpPendingNotification(): Promise<void> {
  try {
    await Notifications.cancelScheduledNotificationAsync(HELP_PENDING_ID);
  } catch {
    // ignore
  }
  try {
    await Notifications.dismissNotificationAsync(HELP_PENDING_ID);
  } catch {
    // ignore
  }
}
