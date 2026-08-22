/**
 * ONE source of truth for "can this phone actually do its job?"
 *
 * #280 (2026-08-22 — Paul):
 *   "On the Notifications screen, the red box says 'Critical Alerts turned
 *    OFF'. Directly beneath it the green box says 'Alerts for dangerous
 *    earthquakes are always on and cannot be switched off.' Both cannot be
 *    true, and the green one is the dangerous falsehood — it tells someone
 *    they are protected when they are not. Fix the cause, not the sentence.
 *    Whatever produces the green box must read the same source as the red
 *    one."
 *
 * That contradiction existed because three screens each decided for
 * themselves what was true: the red banner read the live iOS permission,
 * the green panel was a hard-coded sentence, and the preset helper text was
 * a third hard-coded sentence. Duplication was the bug. Everything that
 * makes a claim about the siren now reads `useReadiness()`, so a single
 * device fact produces a single set of words.
 *
 * It also answers the wider question Paul asked next: "Any state where this
 * app cannot do its job must say so on the home screen, not in a settings
 * page." So this module reports every such state, not only the siren.
 *
 * WHAT THIS DELIBERATELY DOES NOT DO
 *   - It never claims anybody is watching a dashboard.
 *   - It never claims a notification was seen.
 *   - It never says "everything is fine" — silence here means we found no
 *     problem, which is not the same thing, and no screen prints a
 *     reassurance from it.
 */
import { useCallback, useEffect, useState } from "react";
import { AppState, Platform } from "react-native";
import * as Location from "expo-location";
import * as Notifications from "expo-notifications";
import AsyncStorage from "@react-native-async-storage/async-storage";

/** Last time this phone successfully reached our backend, ISO string. */
export const LAST_CONTACT_KEY = "quakeangel_last_backend_contact";

/** After this long with no contact, the home screen says so. */
const OFFLINE_HOURS = 12;

export type ProblemId =
  | "notifications_off"   // iOS will not show our messages at all
  | "siren_off"           // notifications allowed, Critical Alerts refused
  | "location_off"        // we cannot say where they are
  | "no_contact";         // this phone has not reached us for a long time

export type Problem = {
  id: ProblemId;
  /** One short line. Says what will happen, not how we feel about it. */
  headline: string;
  /** What to do, in everyday words. */
  action: string;
  /** true = the siren cannot sound. Ranks above everything else. */
  critical: boolean;
};

export type Readiness = {
  loading: boolean;
  /** iOS/Android permission to show anything at all. */
  notificationsAllowed: boolean;
  /** iOS only: the entitlement that lets the siren sound on silent. */
  criticalAlertsAllowed: boolean;
  /** false once the person has said no twice / "don't ask again". */
  canAskAgain: boolean;
  locationAllowed: boolean;
  /**
   * The single fact everything else is derived from: will an earthquake
   * alert sound the siren on THIS phone?
   *
   * #280 round 2: the first fix still left two conditions. The banner asked
   * `!notificationsAllowed || !criticalAlertsAllowed`; the sentence asked
   * the same thing but only `if (Platform.OS === "ios")`. On any other
   * platform the screen printed the reassurance under the warning — the
   * very contradiction we were fixing. One boolean now, computed once.
   */
  sirenWillSound: boolean;
  hoursSinceContact: number | null;
  /** Worst first. Empty means we found no problem — NOT "all is well". */
  problems: Problem[];
  /** The one sentence the whole app uses about the siren. */
  sirenSentence: string;
  refresh: () => void;
};

export function markBackendContact(): void {
  AsyncStorage.setItem(LAST_CONTACT_KEY, new Date().toISOString()).catch(() => {});
}

async function readPermissions() {
  if (Platform.OS === "web") {
    return { notificationsAllowed: true, criticalAlertsAllowed: true, canAskAgain: true };
  }
  const perm = await Notifications.getPermissionsAsync();
  const ios = (perm as any).ios ?? {};
  // On iOS `allowsCriticalAlerts` is undefined until the system has an
  // opinion. Unknown is NOT treated as granted — we would rather warn
  // wrongly than promise a siren we cannot sound.
  const critical =
    ios.allowsCriticalAlerts === true ||
    (Platform.OS !== "ios" && perm.granted);
  return {
    notificationsAllowed: perm.granted === true,
    criticalAlertsAllowed: perm.granted === true && critical,
    canAskAgain: perm.canAskAgain !== false,
  };
}

export function useReadiness(): Readiness {
  const [state, setState] = useState<
    Omit<Readiness, "problems" | "sirenSentence" | "refresh" | "sirenWillSound">
  >({
    loading: true,
    notificationsAllowed: false,
    criticalAlertsAllowed: false,
    canAskAgain: true,
    locationAllowed: false,
    hoursSinceContact: null,
  });

  const load = useCallback(async () => {
    try {
      const perms = await readPermissions();
      let locationAllowed = false;
      try {
        const loc = await Location.getForegroundPermissionsAsync();
        locationAllowed = loc.granted === true;
      } catch { /* web / no module — leave false, the banner explains it */ }
      let hoursSinceContact: number | null = null;
      try {
        const last = await AsyncStorage.getItem(LAST_CONTACT_KEY);
        if (last) {
          const ms = Date.now() - new Date(last).getTime();
          if (ms > 0) hoursSinceContact = ms / 3_600_000;
        }
      } catch { /* no record yet */ }
      setState({ loading: false, ...perms, locationAllowed, hoursSinceContact });
    } catch {
      setState((s) => ({ ...s, loading: false }));
    }
  }, []);

  useEffect(() => {
    load();
    // Permissions change outside the app, in iOS Settings. Re-read every
    // time we come back, or the home screen would keep warning about
    // something the person has just fixed — and keep reassuring after
    // something has just been switched off.
    const sub = AppState.addEventListener("change", (next) => {
      if (next === "active") load();
    });
    return () => sub.remove();
  }, [load]);

  const sirenWillSound =
    state.notificationsAllowed && state.criticalAlertsAllowed;

  const problems: Problem[] = [];
  if (!state.loading) {
    if (!state.notificationsAllowed) {
      problems.push({
        id: "notifications_off",
        headline: "Your phone will not show our messages at all.",
        action: state.canAskAgain
          ? "Tap to turn notifications on."
          : "Tap to open your phone settings and turn notifications on.",
        critical: true,
      });
    } else if (!sirenWillSound) {
      problems.push({
        id: "siren_off",
        headline: "Your phone will not sound the siren.",
        action: "Tap to fix this.",
        critical: true,
      });
    }
    if (!state.locationAllowed) {
      problems.push({
        id: "location_off",
        headline: "We cannot tell anyone where you are.",
        action: "Tap to allow location. Your rescue code still works without it.",
        critical: false,
      });
    }
    if (state.hoursSinceContact != null && state.hoursSinceContact >= OFFLINE_HOURS) {
      const hrs = Math.floor(state.hoursSinceContact);
      problems.push({
        id: "no_contact",
        headline:
          `This phone has not reached us for ${hrs} hours.`,
        action: "Check your internet. Alerts cannot arrive while it is offline.",
        critical: false,
      });
    }
  }

  // The single sentence about the siren. Every screen prints THIS.
  const sirenSentence = state.loading
    ? "Checking whether the siren can sound on this phone…"
    : !state.notificationsAllowed
      ? "This phone will not show our messages, so the siren cannot sound."
      : !sirenWillSound
        ? "Critical Alerts are off, so an earthquake alert will not sound the siren on this phone."
        : "An earthquake alert always sounds the siren on this phone, even on silent. You cannot switch that off in the app.";

  return { ...state, sirenWillSound, problems, sirenSentence, refresh: load };
}
