/**
 * The Apple Watch reminder: when to show it, and what an answer means.
 *
 * #286 (2026-08-22 — Paul):
 *   "'I don't own an Apple Watch — don't show this again' is a decision
 *    someone makes today about a phone they may pair to a Watch next
 *    Christmas. The moment they do, they fall into exactly the trap this
 *    notice exists to prevent, and nothing will ever tell them. Never let a
 *    safety reminder be permanently dismissed on the basis of something that
 *    can change."
 *
 * THE TECHNICAL QUESTION HE ASKED — CAN WE DETECT A PAIRED WATCH?
 *
 * The property exists: `WCSession.isPaired` in Apple's WatchConnectivity
 * framework. It is NOT reachable from this app as it stands, for one
 * structural reason: WCSession requires a companion watchOS target in the
 * same app bundle. Quake Angel has no Apple Watch app, so there is no
 * WCSession to activate — `WCSession.isSupported()` and any bridge built on
 * it (react-native-watch-connectivity, expo-watch-connectivity) both depend
 * on that companion target existing. There is no other public API on iOS
 * that reports Watch pairing to an iPhone app, and no entitlement for it.
 *
 * So: detection is possible only by building and shipping an actual Apple
 * Watch app. That is a real piece of work, not a flag, and it also means an
 * App Store review of a watchOS binary. Until that exists we take ROUTE B,
 * exactly as Paul specified it:
 *
 *   - No permanent dismissal. "I don't have one" snoozes for 90 days.
 *   - Re-ask after any major iOS version change, because that is when the
 *     mirroring toggle resets anyway.
 *   - The reminder stays findable in Settings › Notifications, permanently.
 *   - After the practice siren we ask where the sound came from. If it came
 *     from the wrist, the app has just discovered the problem itself and
 *     says so, whatever the person answered before.
 *
 * Nothing in here decides anything on its own: home reads these helpers,
 * settings reads these helpers, onboarding reads these helpers. One place.
 */
import AsyncStorage from "@react-native-async-storage/async-storage";
import * as Device from "expo-device";

export const WATCH_CONFIRMED_AT_KEY = "quakeguard_watch_confirmed_at";
export const WATCH_CONFIRMED_VERSION_KEY = "quakeguard_watch_confirmed_version";
/** Snooze — never a permanent no (#286). */
export const WATCH_SNOOZE_UNTIL_KEY = "quakeangel_watch_snooze_until";
/** The iOS major version in force when they last answered. */
export const WATCH_ANSWERED_OS_KEY = "quakeangel_watch_answered_os_major";
/** Set when the practice siren played on the wrist instead of the phone. */
export const WATCH_HEARD_ON_WRIST_KEY = "quakeangel_watch_heard_on_wrist";

/** 90 days. Long enough not to nag, short enough to catch a new Watch. */
export const WATCH_SNOOZE_DAYS = 90;

function iosMajor(): string {
  const v = Device.osVersion ?? "";
  return v.split(".")[0] || "unknown";
}

export async function confirmWatchChecked(): Promise<void> {
  const now = new Date().toISOString();
  await AsyncStorage.multiSet([
    [WATCH_CONFIRMED_AT_KEY, now],
    [WATCH_ANSWERED_OS_KEY, iosMajor()],
    [WATCH_HEARD_ON_WRIST_KEY, ""],
  ]).catch(() => {});
  await AsyncStorage.removeItem(WATCH_SNOOZE_UNTIL_KEY).catch(() => {});
}

/** "I don't have one — ask me again in a few months." */
export async function snoozeWatchReminder(): Promise<void> {
  const until = new Date(Date.now() + WATCH_SNOOZE_DAYS * 86_400_000).toISOString();
  await AsyncStorage.multiSet([
    [WATCH_SNOOZE_UNTIL_KEY, until],
    [WATCH_ANSWERED_OS_KEY, iosMajor()],
  ]).catch(() => {});
}

/** The practice siren came out of the Watch, not the phone. */
export async function recordHeardOnWrist(): Promise<void> {
  await AsyncStorage.multiSet([
    [WATCH_HEARD_ON_WRIST_KEY, new Date().toISOString()],
  ]).catch(() => {});
  // Their earlier "I don't have one" is now known to be out of date.
  await AsyncStorage.removeItem(WATCH_SNOOZE_UNTIL_KEY).catch(() => {});
  await AsyncStorage.removeItem(WATCH_CONFIRMED_AT_KEY).catch(() => {});
}

export type WatchReminderReason =
  | "heard_on_wrist"   // the app found out for itself
  | "new_ios"          // major iOS update since they answered
  | "snooze_expired"   // the few months are up
  | "never_answered";

export async function watchReminderDue(): Promise<
  { due: false } | { due: true; reason: WatchReminderReason }
> {
  try {
    const [wrist, snooze, answeredOs, confirmedAt] = await Promise.all([
      AsyncStorage.getItem(WATCH_HEARD_ON_WRIST_KEY),
      AsyncStorage.getItem(WATCH_SNOOZE_UNTIL_KEY),
      AsyncStorage.getItem(WATCH_ANSWERED_OS_KEY),
      AsyncStorage.getItem(WATCH_CONFIRMED_AT_KEY),
    ]);
    // Strongest evidence first: we heard it on the wrist ourselves.
    if (wrist) return { due: true, reason: "heard_on_wrist" };
    // A major iOS update resets the mirroring toggle, so any earlier
    // answer — including "I don't have one" — is stale.
    if (answeredOs && answeredOs !== iosMajor()) {
      return { due: true, reason: "new_ios" };
    }
    if (snooze) {
      return new Date(snooze).getTime() > Date.now()
        ? { due: false }
        : { due: true, reason: "snooze_expired" };
    }
    if (!confirmedAt) return { due: true, reason: "never_answered" };
    return { due: false };
  } catch {
    // If we cannot read our own notes, ask. Asking costs a tap; not
    // asking can cost a siren nobody hears.
    return { due: true, reason: "never_answered" };
  }
}

/** One line, plain, saying why we are asking again. */
export function watchReminderWhy(reason: WatchReminderReason): string {
  switch (reason) {
    case "heard_on_wrist":
      return "Your practice siren played on your watch, not your phone. That is the problem this warns about.";
    case "new_ios":
      return "Your iPhone software has changed since you last checked. That can switch watch notifications back on.";
    case "snooze_expired":
      return "You said a few months ago that you had no Apple Watch. Checking again in case that has changed.";
    default:
      return "If you wear an Apple Watch, your phone may send the alert to your wrist instead of sounding out loud.";
  }
}
