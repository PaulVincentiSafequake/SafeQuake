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
          // 'critical' bypasses the physical silent switch, DND, and Focus
          // modes. Requires the com.apple.developer.usernotifications.
          // critical-alerts entitlement (approved by Apple for this app).
          interruptionLevel: "critical",
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
    // Also clear any that already fired
    await Notifications.dismissAllNotificationsAsync();
  } catch {
    // ignore
  }
}
