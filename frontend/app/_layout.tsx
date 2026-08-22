import { Stack, useRouter, usePathname } from "expo-router";
import * as SplashScreen from "expo-splash-screen";
import * as Notifications from "expo-notifications";
import * as TaskManager from "expo-task-manager";
import * as Linking from "expo-linking";
import { setAudioModeAsync } from "expo-audio";
import { useEffect, useRef } from "react";
import { AppState, type AppStateStatus, LogBox, Platform } from "react-native";
import { SafeAreaProvider } from "react-native-safe-area-context";
import AsyncStorage from "@react-native-async-storage/async-storage";

import { useIconFonts } from "@/src/hooks/use-icon-fonts";
import { registerForPushNotifications } from "@/src/utils/push";
import { getDeviceId } from "@/src/utils/checkin";
import { flushRecheckQueue, submitRecheckAnswer } from "@/src/utils/recheck";
import {
  clearActiveAlert,
  markAlertActive,
  shouldRedirectToAlert,
  toAlertQuery,
} from "@/src/utils/activeAlert";
import { isAlertScreenMounted, publishAlert } from "@/src/utils/alertBus";
import {
  cancelCheckInReminders,
  ensureNotificationSetup,
  scheduleCheckInReminders,
} from "@/src/utils/reminders";
import { recordTap, buildLogFields, type TapSource } from "@/src/utils/tapProbe";
import { reportPresentedPushes, reportPushSeen } from "@/src/utils/pushReceipt";
import {
  recordTremorNoticeOpened,
  recordTremorNoticeReceived,
} from "@/src/utils/tremorNotices";

/**
 * #305: which pushes are tremor notices — the quiet "a shake happened
 * nearby" messages — as opposed to our operational ones. Listed by
 * exclusion, deliberately: a tremor notice is the default kind of push, so
 * a new operational kind added later cannot be miscounted as noise.
 */
const OPERATIONAL_PUSH_KINDS = [
  "critical_alert",
  "check_in_request",
  "recheck",
  "cancel_reminders",
  "alert_stood_down",
  "incident_closed",
  "quakeguard-reminder",
];
function isTremorNotice(kind: string): boolean {
  return !OPERATIONAL_PUSH_KINDS.includes(kind);
}

const ONBOARDING_DONE_KEY = "quakeguard_onboarding_done";

// Notification categories (batch 5, B9).
//
// TREMOR_INFO carries the two action buttons for INFORMATIONAL tremor
// notices only. The critical earthquake alert deliberately has NO category
// at all — during a real event nothing may compete with I'M SAFE /
// I'M TRAPPED, and a separate category id is the structural guarantee that
// the two can never share configuration.
const TREMOR_CATEGORY_ID = "TREMOR_INFO";
const ACTION_VIEW_MAP = "VIEW_MAP";
const ACTION_CLOSE = "CLOSE";

// RECHECK_V1 carries the answer buttons for the C1 re-check ladder — sent
// ONLY to someone who has themselves reported being trapped.
//
// SAME / WORSE / MUCH WORSE all run with `opensAppToForeground: false`, which
// means they submit straight from the lock screen with no Face ID and no
// passcode. That is the PRIMARY answer path by design: Face ID under dust, in
// darkness, at an odd angle will often fail, and then it is a passcode,
// one-handed, injured (Paul, 2026-08-18). BETTER is deliberately in-app only
// — it is the rarest and least time-critical answer, and it must not be the
// easiest button to hit by accident.
//
// Android shows up to three inline actions, so the ordering here puts the
// answers that change something urgent first.
const RECHECK_CATEGORY_ID = "RECHECK_V1";
const ACTION_RECHECK_SAME = "RECHECK_SAME";
const ACTION_RECHECK_WORSE = "RECHECK_WORSE";
const ACTION_RECHECK_MUCH_WORSE = "RECHECK_MUCH_WORSE";
const RECHECK_ACTION_ANSWERS: Record<string, "same" | "worse" | "much_worse"> = {
  [ACTION_RECHECK_SAME]: "same",
  [ACTION_RECHECK_WORSE]: "worse",
  [ACTION_RECHECK_MUCH_WORSE]: "much_worse",
};

// Background handler for the operator's "cancel all pending reminders" kill
// switch (batch 5, B1). The backend sends a SILENT push (content-available,
// no alert, no sound) so stopping a false alarm never costs the user another
// loud notification. Registered as a background task so it works with the
// app killed or backgrounded, not only in the foreground.
const CANCEL_REMINDERS_TASK = "quakeangel-cancel-reminders";

if (Platform.OS !== "web") {
  TaskManager.defineTask(CANCEL_REMINDERS_TASK, async ({ data, error }) => {
    if (error) return;
    try {
      const payload: any = (data as any)?.notification ?? data;
      const body =
        payload?.data?.body ??
        payload?.request?.content?.data ??
        payload?.data ??
        payload;
      const kind = String(body?.kind ?? "").trim();
      if (kind === "cancel_reminders") {
        await cancelCheckInReminders();
        console.log("[QuakeAngel] reminders cancelled by operator kill switch");
      }
    } catch (e) {
      console.log("[QuakeAngel] cancel-reminders task err:", (e as Error)?.message);
    }
  });
}

// Disable logbox errors etc so that users can see the app
// and agent works as expected.
LogBox.ignoreAllLogs(true);

// Keep the native splash visible from cold start until icon fonts register.
SplashScreen.preventAutoHideAsync();

// ---- Module-scope notification setup (before any component mounts) ----
if (Platform.OS !== "web") {
  Notifications.setNotificationHandler({
    handleNotification: async () => ({
      shouldShowBanner: true,
      shouldShowList: true,
      shouldPlaySound: true,
      shouldSetBadge: false,
    }),
  });
}

if (Platform.OS === "android") {
  // Fire and forget — channel must exist before any push arrives
  Notifications.setNotificationChannelAsync("default", {
    name: "Default",
    importance: Notifications.AndroidImportance.MAX,
    sound: "default",
  }).catch(() => {});
  Notifications.setNotificationChannelAsync("quakeguard-critical", {
    name: "Earthquake safety reminders",
    importance: Notifications.AndroidImportance.MAX,
    sound: "default",
    vibrationPattern: [0, 400, 300, 400],
    bypassDnd: true,
    lockscreenVisibility: Notifications.AndroidNotificationVisibility.PUBLIC,
  }).catch(() => {});
}

// Module-scope audio-session init so the siren on /alert can play through
// the physical silent switch on iOS. Runs BEFORE any useAudioPlayer is
// instantiated.
if (Platform.OS !== "web") {
  setAudioModeAsync({
    playsInSilentMode: true,
    shouldPlayInBackground: false,
    interruptionMode: "doNotMix",
    allowsRecording: false,
  }).catch((e) =>
    console.log("[QuakeGuard] cold-start setAudioModeAsync err:", e?.message),
  );
}

// Register the tremor-notice category + the silent-push background task.
// Both are fire-and-forget: failures degrade the feature, never the app.
if (Platform.OS !== "web") {
  Notifications.setNotificationCategoryAsync(TREMOR_CATEGORY_ID, [
    {
      identifier: ACTION_VIEW_MAP,
      buttonTitle: "See location on map",
      options: { opensAppToForeground: true },
    },
    {
      identifier: ACTION_CLOSE,
      buttonTitle: "Close",
      options: { opensAppToForeground: false, isDestructive: false },
    },
  ]).catch(() => {});

  Notifications.setNotificationCategoryAsync(RECHECK_CATEGORY_ID, [
    {
      identifier: ACTION_RECHECK_WORSE,
      buttonTitle: "Worse",
      options: { opensAppToForeground: false },
    },
    {
      identifier: ACTION_RECHECK_MUCH_WORSE,
      buttonTitle: "Much worse",
      options: { opensAppToForeground: false, isDestructive: true },
    },
    {
      identifier: ACTION_RECHECK_SAME,
      buttonTitle: "No change",
      options: { opensAppToForeground: false },
    },
  ]).catch(() => {});

  Notifications.registerTaskAsync(CANCEL_REMINDERS_TASK).catch(() => {});
}

export default function RootLayout() {
  const [loaded, error] = useIconFonts();
  const router = useRouter();
  const pathname = usePathname();

  useEffect(() => {
    if (loaded || error) {
      SplashScreen.hideAsync();
    }
  }, [loaded, error]);

  // ─────────────────────────────────────────────────────────────────
  // #208 (Neo, 2026-08-20 — Paul):
  //   "Locked my iPhone with the app already open on Diagnostics.
  //    Triggered a real earthquake alert from the dashboard. The alert
  //    arrived correctly on the locked phone; I tapped it; it took me
  //    straight back to the Diagnostics screen I had been on — NOT the
  //    check-in screen. The check-in screen must take over in every
  //    resume case, on ANY screen, not just Home."
  //
  // Root cause (Neo audit): `shouldRedirectToAlert()` was called from
  // ONE place only — `app/index.tsx` (Home). Every other screen was a
  // dead end for a live unanswered alert. The notification tap handler
  // in this file DOES call `router.push("/alert")`, but on a phone
  // resuming from lock+background that push can race the router's
  // ready state (iOS suspends JS while locked; on unlock the response
  // fires before the router has finished mounting) and silently no-op.
  // Home compensated because its own mount check fired again; every
  // other screen has no such compensator.
  //
  // Fix: a SINGLE global watcher, in the root layout (which wraps
  // every route), that reads `shouldRedirectToAlert()` and force-
  // navigates to /alert whenever:
  //   1. The layout mounts (covers cold start on any deep-link).
  //   2. AppState transitions to "active" (covers every resume:
  //      background→foreground, lock→unlock, phone-call return,
  //      Control Center dismiss).
  //   3. The pathname changes to anything except /alert or /recheck.
  //      (Belt: catches the case where the tap handler pushed us
  //       somewhere else because the payload lacked the critical
  //       marker, and the persisted `activeAlert` record is the
  //       authoritative "someone still owes an answer" signal.)
  //
  // Exemptions — we do NOT redirect if the user is already on:
  //   - /alert   (they're on the check-in screen)
  //   - /recheck (they're on the follow-up "still ok?" screen — a
  //              different but equally valid answer path)
  //   - /onboarding (first-launch flow; the alert would jump them
  //                 into the middle of it with no permissions).
  //
  // Landing after answer: /alert already navigates to Home on submit
  // (via router.replace("/") on the "sent" transition). We use
  // `router.replace` here too — never `router.push` — so the back
  // stack does not accumulate a Diagnostics screen underneath /alert,
  // which would let the user swipe-back into the very screen they
  // were rescued from.
  const lastRedirectedAt = useRef<number>(0);
  useEffect(() => {
    if (Platform.OS === "web") return;
    let cancelled = false;

    const checkAndRedirect = () => {
      // Don't stack a redirect on top of a redirect we just did — the
      // AppState listener and the pathname effect can both fire within
      // a few ms during a lock→unlock transition. 750ms is longer than
      // any legitimate navigation and short enough that a fresh alert
      // 1s later still gets picked up.
      if (Date.now() - lastRedirectedAt.current < 750) return;
      const currentPath = pathname ?? "";
      // Already on a check-in screen — leave alone.
      if (
        currentPath === "/alert" ||
        currentPath.startsWith("/alert?") ||
        currentPath === "/recheck" ||
        currentPath.startsWith("/recheck?") ||
        currentPath === "/onboarding"
      ) return;
      shouldRedirectToAlert()
        .then((a) => {
          if (cancelled || !a) return;
          lastRedirectedAt.current = Date.now();
          router.replace(("/alert" + toAlertQuery(a)) as any);
        })
        .catch(() => { /* non-fatal: home screen still checks on its own path */ });
    };

    // (1) mount + every pathname change fires this effect.
    checkAndRedirect();

    // (2) resume-from-background — the specific case Paul screenshotted.
    const sub = AppState.addEventListener("change", (next: AppStateStatus) => {
      if (next === "active") {
        // A tiny delay lets the router settle after iOS wakes JS —
        // without it the very first replace() after unlock is
        // occasionally a no-op (the same race that made the original
        // tap handler's router.push land nowhere).
        setTimeout(checkAndRedirect, 150);
        // #276: confirm anything of ours still sitting in Notification
        // Centre. This is how a question that arrived while the app was
        // closed gets confirmed to the operator's board at all.
        reportPresentedPushes();
      }
    });

    return () => {
      cancelled = true;
      sub.remove();
    };
  }, [router, pathname]);
  // ─────────────────────────────────────────────────────────────────

  // Register push token on cold start (retries on every app open).
  //
  // On iOS we gate the FIRST-EVER permission prompt behind an /onboarding
  // screen so the user sees the Apple Watch caveat right next to the ask.
  // On every subsequent launch (or on Android) we register silently.
  useEffect(() => {
    if (Platform.OS === "web") return;

    (async () => {
      if (Platform.OS === "ios") {
        try {
          const done = await AsyncStorage.getItem(ONBOARDING_DONE_KEY);
          if (!done) {
            const perm = await Notifications.getPermissionsAsync();
            // Only intercept if we still have a chance to prompt. If the
            // user already granted (upgrading from an older build) or
            // already denied permanently, just mark onboarding done and
            // continue silently — the note lives on /diag for reference.
            if (!perm.granted && perm.canAskAgain) {
              router.replace("/onboarding");
              return;
            }
            await AsyncStorage.setItem(ONBOARDING_DONE_KEY, "1");
          }
        } catch (e) {
          console.log("[QuakeGuard] onboarding gate err:", (e as Error)?.message);
        }
      }

      registerForPushNotifications().catch((e) =>
        console.log("[QuakeGuard] push register error:", e?.message),
      );

      // C1: any re-check answer tapped while offline goes out on app open.
      flushRecheckQueue().catch(() => {});
      // #276: on cold start too — a question may have been sitting on the
      // lock screen since before the app was opened.
      reportPresentedPushes();
    })();
  }, [router]);

  // Handle notification taps → route by payload `kind`, fail-safe to informational.
  //
  // BUG-2026-08-06-preview-tap-siren: previously this handler defaulted
  // to /alert for any notification without an explicit action_url. That
  // meant a preview notification (M2.7 event 1,300km away) tapped by the
  // user opened the full EARTHQUAKE DETECTED screen + siren. That is the
  // exact alert-fatigue failure the preview constraints exist to prevent.
  //
  // Fix: route by `data.kind`:
  //   - "critical_alert" → /alert (existing critical-alert flow)
  //   - "emsc_preview"   → /quake/[unid] (informational detail, no siren)
  //   - anything else / missing → /quake/[unid] fallback (informational)
  //
  // Fail-safe philosophy: a missed siren on tap is recoverable (the
  // notification itself already carried siren+haptics if it was real);
  // a spurious siren on tap destroys trust permanently. Default MUST
  // be informational, not critical.
  useEffect(() => {
    if (Platform.OS === "web") return;

    const handleTap = (
      data: Record<string, any>,
      tapCtx?: { source: TapSource; actionIdentifier: string | null },
    ) => {
      // #208 probe (v1.0.40): additive-only logger. Records the router
      // decision made below for the last N taps so we can see, on the
      // user's own phone, which routing key was (or wasn't) present in
      // the payload iOS actually delivered. Does NOT alter routing.
      const logChoice = (chosenRoute: string) => {
        if (!tapCtx) return;
        recordTap({
          ts: new Date().toISOString(),
          source: tapCtx.source,
          actionIdentifier: tapCtx.actionIdentifier,
          rawPayload: data,
          ...buildLogFields(data),
          chosenRoute,
        }).catch(() => {});
      };

      // #208 defence-in-depth. `kind` is the primary router key, but for the
      // re-check path — where the tap comes from someone who reported they
      // are trapped — we treat ANY of the following as a positive signal
      // and NEVER fall through to the informational stats screen:
      //   - kind === "recheck"
      //   - type === "recheck" (alias, in case an older payload used it)
      //   - action_url === "/recheck"
      //   - a non-empty check_id (unambiguous: only re-checks carry it)
      //
      // Rationale (Paul, 2026-08-19): a trapped person tapping a "still OK?"
      // notification and landing on a stats page means they cannot report
      // status. That failure mode must be one bad field away from a
      // life-safety miss — so we require ALL four to be wrong before the
      // handler is even allowed to consider another screen.
      const kind = String(data.kind || data.type || "").trim();
      const actionUrl = typeof data.action_url === "string" ? data.action_url : "";
      const hasCheckId = data.check_id != null && String(data.check_id).length > 0;
      // #273 sweep: `check_id` is the one presence test kept, and it is
      // now fenced. Only re-check prompts carry a check_id (apns.py
      // send_recheck_prompts), and the failure direction is safe — it
      // routes a trapped person to the screen where they can answer, and
      // it can never start the siren. But it must never be able to
      // capture a notification that has already said what it is, so any
      // payload carrying a DIFFERENT explicit kind is excluded outright.
      const NON_RECHECK_KINDS = [
        "critical_alert", "emsc_preview", "tremor_notice",
        "quakeguard-reminder", "alert_stood_down", "incident_closed",
        "cancel_reminders",
        // #271: an operator's check-in request carries a check_id too, but
        // it belongs on the calm check-in screen, not the four-button
        // "has anything changed?" screen for someone already trapped.
        "check_in_request",
      ];
      const looksLikeRecheck =
        kind === "recheck" ||
        actionUrl === "/recheck" ||
        (hasCheckId && !NON_RECHECK_KINDS.includes(kind));
      const unid = data.unid ? String(data.unid) : null;

      // Re-check prompt (C1). Tapping the BODY opens the four-button screen;
      // the lock-screen action buttons are handled before this ever runs.
      //
      // Placed BEFORE the critical-alert branch because a trapped user's
      // "still OK?" tap must never be misclassified. `check_id` is the
      // hard-guarantee marker: only /api/rechecks payloads carry it, and if
      // it is present nothing else on the phone should be able to steer this
      // tap away from /recheck.
      if (looksLikeRecheck) {
        const params = new URLSearchParams();
        if (data.check_id != null) params.set("check_id", String(data.check_id));
        const route = "/recheck" + (params.toString() ? "?" + params.toString() : "");
        logChoice(route);
        router.push(route as any);
        return;
      }

      // Real critical alert — route to /alert with event details as params.
      // The `siren=1` param signals the alert screen that it should play
      // the siren on mount. Missing `siren=1` = no siren (fail-safe).
      //
      // #273 (2026-08-21 — Paul, verified live on production): THE SIREN
      // MUST BE ROUTED ON WHAT THE NOTIFICATION SAYS IT IS, NEVER ON WHAT
      // FIELDS HAPPEN TO BE PRESENT.
      //
      // What happened: Paul tapped a "PREVIEW · Seismic activity" notice
      // whose own text said "Test notification, no action needed" and got
      // the full red EARTHQUAKE DETECTED screen with the siren playing.
      //
      // His diagnosis was exactly right. The rule below used to be:
      //     kind === "critical_alert" || action_url === "/alert" || hasMagnitude
      // and preview payloads DO carry a magnitude (apns.py
      // _build_preview_payload). It only ever looked safe because iOS was
      // delivering empty data — the #208 payload fix made the data arrive,
      // so every preview tap started matching on magnitude. The presence
      // test was unsafe from the day it was written; broken data was
      // hiding it.
      //
      // Why this is not cosmetic: the critical-alert entitlement is
      // granted for genuine emergencies (#207). Sirening a routine notice
      // risks losing it, and without it the app cannot wake a locked
      // silenced phone — which is the entire product. It also teaches
      // people to distrust the siren. Same fault as #109, by a new route.
      //
      // The rule now:
      //   * kind === "critical_alert"  → /alert, siren ON. Nothing else
      //     may turn the siren on, ever.
      //   * no kind at all AND action_url === "/alert" → /alert, siren
      //     OFF. Routing is preserved for a malformed real alert (that is
      //     the #208 guarantee) but the siren is withheld, because a
      //     missed siren is recoverable and a false siren is not.
      //   * any other kind → never the alert screen, whatever fields the
      //     payload happens to carry.
      // `magnitude` is no longer consulted for routing anywhere.
      const isCriticalAlert = kind === "critical_alert";
      const isUnlabelledAlertRoute = kind === "" && actionUrl === "/alert";
      if (isCriticalAlert || isUnlabelledAlertRoute) {
        // Persist the alert as "unanswered" so opening the app later —
        // via the home screen, not the notification — still lands the
        // person on the check-in screen. Cleared when they submit safe
        // or trapped. Non-blocking.
        //
        // #273: ONLY a genuine critical alert may write this. Before the
        // fix, a preview tap wrote it too, so the phone went on forcing
        // the check-in screen for an incident that never existed.
        if (isCriticalAlert) {
          markAlertActive({
            kind: "critical_alert",
            magnitude: data.magnitude != null ? Number(data.magnitude) : null,
            distance_km: data.distance_km != null ? Number(data.distance_km) : null,
            intensity: data.intensity != null ? String(data.intensity) : null,
            region: data.region != null ? String(data.region) : null,
            unid: data.unid != null ? String(data.unid) : null,
          }).catch(() => {});
        }
        const params = new URLSearchParams();
        params.set("siren", isCriticalAlert ? "1" : "0");
        if (data.magnitude != null)   params.set("magnitude", String(data.magnitude));
        if (data.distance_km != null) params.set("distance_km", String(data.distance_km));
        if (data.intensity != null)   params.set("intensity", String(data.intensity));
        if (data.depth_km != null)    params.set("depth_km", String(data.depth_km));
        if (data.region != null)      params.set("region", String(data.region));
        if (data.unid != null)        params.set("unid", String(data.unid));
        const route = "/alert?" + params.toString();
        logChoice(route);
        router.push(route as any);
        return;
      }

      // #271: an operator pressed "Ask them to check in". This is NOT an
      // alert — nothing has happened. It opens the same check-in screen,
      // with the same I'M SAFE / I NEED HELP buttons and the same submit
      // path (so a help report from here reaches the working board exactly
      // as one made during an alert), but in its calm form and with the
      // siren firmly off. It never writes the unanswered-alert marker:
      // there is no incident to force anyone back to.
      if (kind === "check_in_request") {
        const p = new URLSearchParams();
        p.set("siren", "0");
        p.set("checkin", "1");
        if (data.check_id != null) p.set("check_id", String(data.check_id));
        const route = "/alert?" + p.toString();
        logChoice(route);
        router.push(route as any);
        return;
      }

      // Check-in reminder ("Are you safe?" follow-up) — routes to /alert
      // for the check-in flow, but explicitly NO siren. The user is
      // being reminded to check in, not being alerted afresh.
      if (kind === "quakeguard-reminder") {
        // A reminder tap is also an "unanswered alert" signal — the
        // person still needs to say safe or trapped.
        markAlertActive({
          kind: "quakeguard-reminder",
          magnitude: null, distance_km: null, intensity: null,
          region: null, unid: null,
        }).catch(() => {});
        const route = "/alert?siren=0&reminder=1";
        logChoice(route);
        router.push(route as any);
        return;
      }

      // #199 / #202 companion path (2026-08-19 night, Paul):
      //   "The unanswered-alert flag is cleared only by a check-in. If
      //    an alert is stood down as a false alarm (#199) or an incident
      //    is closed (#202), every phone would keep forcing people to
      //    the check-in screen with no way out."
      //
      // A stand-down push clears the local unanswered-alert marker so
      // the home screen stops redirecting to /alert. If the user is
      // currently ON /alert, we publish through the alert bus so the
      // screen can show a "This alert has been stood down" note and
      // route them home cleanly — never leave them stranded on a
      // check-in screen for an incident that no longer exists.
      if (kind === "alert_stood_down" || kind === "incident_closed") {
        clearActiveAlert().catch(() => {});
        // If the user tapped this notification, take them home.
        // Publishing on the bus is a signal to any mounted /alert
        // screen; the home screen itself needs no message here.
        publishAlert({
          magnitude: null, distance_km: null, intensity: null,
          depth_km: null, region: null, unid: data.unid ?? null,
          // A synthetic "stood down" flag the alert screen watches for.
          stood_down: true,
          stood_down_reason:
            typeof data.reason === "string" ? data.reason : "false_alarm",
        } as any);
        logChoice("/");
        router.replace("/" as any);
        return;
      }

      // External web links still open in the browser.
      const explicit = data.action_url || data.deeplink;
      if (explicit && typeof explicit === "string" && explicit.startsWith("http")) {
        logChoice(`external:${explicit}`);
        Linking.openURL(explicit).catch(() => {});
        return;
      }

      // Preview / tremor notice / unknown kind → informational detail screen
      // (fail-safe). A siren on a mistaken tap destroys trust permanently; a
      // missed siren is recoverable because the notification itself carried
      // sound+haptics if it was truly critical.
      //
      // NOTE (batch 5, B3): every event field in the payload is forwarded as
      // a query param. This branch used to be short-circuited by an
      // `action_url === "/quake/<unid>"` check that navigated WITHOUT any
      // params, which is why the detail screen showed "—" for distance,
      // depth and coordinates when opened from a notification.
      const params = new URLSearchParams();
      Object.entries(data).forEach(([k, v]) => {
        if (v != null && k !== "kind" && k !== "action_url" && k !== "deeplink" && k !== "aps") {
          params.set(k, String(v));
        }
      });
      const qs = params.toString();
      const path = unid
        ? `/quake/${encodeURIComponent(unid)}`
        : typeof explicit === "string" && explicit !== "/alert"
          ? explicit
          : "/quake/unknown";
      const fallbackRoute = path + (qs ? "?" + qs : "");
      // #305: they opened it. Opening even one means the notices are being
      // read, and the "want fewer?" question is never shown.
      if (isTremorNotice(kind)) recordTremorNoticeOpened().catch(() => {});
      logChoice(fallbackRoute);
      router.push(fallbackRoute as any);
    };

    const tapSub = Notifications.addNotificationResponseReceivedListener(
      (response) => {
        const data = (response.notification.request.content.data ?? {}) as any;
        // #276: a tap is the strongest possible proof it arrived.
        reportPushSeen(data, "tapped");
        // Action buttons on tremor notices (batch 5, B9). "Close" must do
        // nothing at all — no navigation, no app launch. Tapping the
        // notification body behaves exactly like "See location on map",
        // which is what users expect.
        if (response.actionIdentifier === ACTION_CLOSE) return;

        // C1: an answer tapped on the lock screen submits WITHOUT opening the
        // app. It is queued locally first, so no signal never loses it.
        const recheckAnswer = RECHECK_ACTION_ANSWERS[response.actionIdentifier];
        if (recheckAnswer) {
          getDeviceId()
            .then((id) => submitRecheckAnswer(id, recheckAnswer, data.check_id))
            .catch(() => {});
          return;
        }

        handleTap(data, {
          source: "response",
          actionIdentifier: response.actionIdentifier ?? null,
        });
      },
    );

    // Reminders + operator kill switch while the app is running (batch 5, B1).
    //   - a genuine critical alert arriving now arms the reminder sequence
    //     even before the user taps the notification;
    //   - the operator's silent "cancel reminders" push clears them.
    const recvSub = Notifications.addNotificationReceivedListener((n) => {
      const data = (n.request.content.data ?? {}) as any;
      const kind = String(data.kind ?? "").trim();
      // #276: the phone has the question in its hands. Tell the operator's
      // board so "no answer" can be told apart from "never arrived".
      reportPushSeen(data, "shown");
      // #305: the app counts its own noise. A tremor notice is anything
      // that is not one of our operational pushes — the same set that the
      // tap handler sends to the informational detail screen.
      if (isTremorNotice(kind)) recordTremorNoticeReceived().catch(() => {});
      if (kind === "cancel_reminders") {
        cancelCheckInReminders().catch(() => {});
        return;
      }
      // #199/#202 (R4 companion): a stand-down / incident-closed push
      // arriving while the app is open must clear the unanswered-alert
      // flag AND signal any mounted /alert screen so the user isn't
      // stuck on a check-in for an incident that no longer exists.
      // Silent push — no navigation from the receipt path, only state.
      if (kind === "alert_stood_down" || kind === "incident_closed") {
        clearActiveAlert().catch(() => {});
        publishAlert({
          magnitude: null, distance_km: null, intensity: null,
          depth_km: null, region: null, unid: data.unid ?? null,
          stood_down: true,
          stood_down_reason:
            typeof data.reason === "string" ? data.reason : "false_alarm",
        } as any);
        return;
      }
      if (kind === "critical_alert") {
        // #208 R4 (Batch 7): mark this alert as unanswered so opening
        // the app later — via the home screen, without the
        // notification — still lands on /alert. Cleared by the check-in
        // submit path. Non-blocking.
        markAlertActive({
          kind: "critical_alert",
          magnitude: data.magnitude != null ? Number(data.magnitude) : null,
          distance_km: data.distance_km != null ? Number(data.distance_km) : null,
          intensity: data.intensity != null ? String(data.intensity) : null,
          region: data.region != null ? String(data.region) : null,
          unid: data.unid != null ? String(data.unid) : null,
        }).catch(() => {});
        // #169 follow-up — CLOSE THE FOREGROUND GAP, WITHOUT STACKING.
        //
        // If a real alert lands while the app is already open, iOS plays the
        // push sound once (siren.caf) and shows a banner, but nothing else
        // happened: the user was left on whatever screen they were on, with
        // no looping siren and no check-in screen, unless they happened to
        // tap the banner. So we route them to the alert screen.
        //
        // AFTERSHOCK SAFETY (Paul, 2026-08-17): if that screen is ALREADY
        // open we must not navigate again. router.push would mount a second
        // alert screen, discarding an in-progress answer (open triage sheet,
        // chosen severity, mobility answer) and leaving the first screen's
        // siren looping underneath the new one. A trapped person must never
        // have to report themselves twice because the ground shook again.
        // Instead the new event is published to the open screen, which
        // updates the readings and says so without touching their answer.
        if (isAlertScreenMounted()) {
          publishAlert(data);
        } else {
          handleTap({ ...data, kind: "critical_alert" });
        }
        (async () => {
          const ok = await ensureNotificationSetup();
          if (!ok) return;
          await cancelCheckInReminders();
          await scheduleCheckInReminders();
        })().catch(() => {});
      }
    });

    // Cold-start tap (app was killed when notification arrived).
    //
    // Two cases matter here for a trapped user:
    //   1. They tapped the BODY of a re-check notification — must land on
    //      /recheck, never on /quake/[unid] (defect #208).
    //   2. They tapped a lock-screen action button (SAME / WORSE / MUCH
    //      WORSE) but the app was killed, so iOS may still deliver the
    //      response on next launch — submit the answer immediately.
    Notifications.getLastNotificationResponseAsync().then((response) => {
      if (!response) return;
      const data = (response.notification.request.content.data ?? {}) as any;
      if (response.actionIdentifier === ACTION_CLOSE) return;
      const recheckAnswer = RECHECK_ACTION_ANSWERS[response.actionIdentifier];
      if (recheckAnswer) {
        getDeviceId()
          .then((id) => submitRecheckAnswer(id, recheckAnswer, data.check_id))
          .catch(() => {});
        return;
      }
      handleTap(data, {
        source: "lastResponse",
        actionIdentifier: response.actionIdentifier ?? null,
      });
    });

    return () => {
      try {
        tapSub.remove();
        recvSub.remove();
      } catch {
        // web shim
      }
    };
  }, [router]);

  // If the CDN is unreachable we fall through on error rather than wedging
  // the app — icons will tofu, but the app still boots.
  if (!loaded && !error) return null;

  return (
    <SafeAreaProvider>
      <Stack screenOptions={{ headerShown: false }} />
    </SafeAreaProvider>
  );
}
