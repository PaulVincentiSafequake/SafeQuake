# App-side change history by build
Requested by Paul, batch 5. Answers "what has genuinely never run on a
device", after the 3 August build was found to be two weeks stale.

## Why the gap happened
`app.json` `expo.version` sat at **1.0.22 from 22 July to 17 August**. Every
build in that window carried the same version string, so builds were
indistinguishable from each other and from Paul's install — there was no way
to tell, from the phone, which code you were holding. The version is now
bumped on every batch that touches app code, and every app-side change is
reported with the version it lands in. That is the whole point of the rule.

## Build 1022 / version 1.0.22 — Paul's install until 17 Aug (dated 3 Aug)
Everything below this line was written AFTER the code in that build, and had
never run on a device before 1.0.23.

## Version 1.0.23 — first build after the version unfreeze (17 Aug)
Contains 3½ weeks of accumulated app work:

| Change | Written | Issue |
|---|---|---|
| Triage labels rewritten + mobility question skipped for red and green | 31 Jul | #51 |
| Apple Watch advisory (shared component, onboarding + Diagnostics) | 31 Jul | — |
| Check-in reminders reworked (staggered local notifications) | 4 Aug | — |
| Push registration changes (onboarding-gated first prompt) | 5 Aug | — |
| Informational quake detail screen (`/quake/[unid]`) | 6 Aug | preview-tap-siren fix |
| Tremor notification settings screen | 6 Aug | 2026-08-06 requirement 1 |
| In-app seismic map | 6 Aug | #107 |
| Subscription entitlement banner | 6 Aug | — |
| Optional first name + short rescue code, lock-screen rescue card | Jul–Aug | #58 |

Paul confirmed on 17 Aug that the seismic map, the first-name/rescue-code
screens and the notification settings screen all appeared with this install —
they were built, never shipped. Two of them had been reopened as "possibly
never built".

## Version 1.0.24 — batch 5 (this build)
| Change | Item |
|---|---|
| In-app TEST trigger no longer schedules check-in reminders; reminders arm only on a real alert | B1 |
| App answers the operator's silent "cancel all reminders" push (background task) | B1 |
| Alert screen: data strip can never be covered by the action buttons at any screen height; compact layout below 760pt | B2 |
| "Dismiss alert" removed from the alert screen | #14 |
| Seismic detail screen: real distance from the user, stated Malta fallback, sentence never renders with a gap | B3 |
| Notification taps now forward every event field to the detail screen (they previously navigated with no params) | B3 |
| Tremor notification settings: four options merged to three; "everything" states it includes imperceptible tremors | B4 |
| "woken by the siren" → "alerted by the siren"; onboarding waking claim reworded | B5 |
| Post-update Apple Watch notice carries the "why this matters" copy + "I don't use an Apple Watch" permanent opt-out | B6 |
| "Places I care about" — optional named places with tremor notices | B8 |
| Tremor notices carry "See location on map" / "Close" actions; critical alert deliberately carries none | B9 |
| `ITSAppUsesNonExemptEncryption: false` (stops the export-compliance question on every build), explicit `CFBundleDisplayName: Quake Angel`, `UIBackgroundModes: remote-notification` | housekeeping |

## Notes on build numbers
`app.json` sets the marketing version only; the iOS build number and Android
versionCode are assigned by the Emergent build pipeline at publish time and
increment per build. So the precise pairing is "version 1.0.24, first build
generated after this publish" — quote the version, read the build number off
TestFlight.

## Version 1.0.25 — #169 siren fix (17 Aug, evening)
**Version 1.0.24 / build 125 does NOT contain any of this.** It was snapshotted
before the siren fix existed.

| Change | Item |
|---|---|
| Trigger Test Alert now passes `?siren=1&test=1` — the test uses the identical playback path as a real alert. Silent since 2026-08-06 (commit d3e8d81) | #169 |
| Siren and check-in reminders are independent decisions (`siren=1` vs `test=1`) — conflating them is what hid #169 | #169 |
| Android real alerts now carry `kind: "critical_alert"` (backend) — previously a real alert tapped on Android opened the informational screen and armed no siren | #169 follow-up |
| Foreground real alerts route to the check-in screen instead of leaving the user where they were | #169 follow-up |
| Aftershock guard: a second alert publishes to the OPEN alert screen instead of remounting it, so an in-progress answer survives; no siren resurrection | Paul's edge case |
| Diagnostics shows version + build number read from the installed binary, plus a hard-coded "fixes in this build" marker | build-gap prevention |

## Version 1.0.27 — GDPR + timestamps + C1 phase 1 (18 Jun 2026)

Paul's build before this was **1.0.25 (126)**; he never installed 1.0.26, which
is why nearly all of batch 6 read as unfixed. Everything in 1.0.26 is still in
1.0.27.

| Change | Ref |
|---|---|
| `parseUtc` — backend timestamps without an offset were read as LOCAL time, showing events two hours early on a Malta phone | A0 follow-up |
| `/recheck` screen — four ~64 pt buttons, SAME · WORSE · MUCH WORSE · BETTER | C1 |
| Lock-screen answers (WORSE / MUCH WORSE / SAME) that submit without unlocking | C1 / D2 |
| `recheck.wav` (~1 s) bundled for the critical-interruption re-check sound | C1 |
| Offline answer queue, stamped with the time of the tap | C1 |
| Diagnostics: "Reset Apple Watch reminder" so B6 can be re-tested without reinstalling | C4 |
| Diagnostics "fixes in this build" marker updated (still hard-coded, deliberately) | process |
| "I NEED HELP" replaces "I'M TRAPPED / NEED HELP" on the alert screen | batch 6 triage |
| "Can you get out on your own?" asked after a GREEN (minor injury) report — mobility describes the body, egress describes the building | batch 6 triage |
| Tremor settings, third option: the clause moved INTO the title — "Everything nearby — including tremors too small to feel". An option must state what it costs you on the line you choose it by | batch 6 B3 |

**Batch 6 items that are ALREADY in 1.0.26/1.0.27 and need no further work** —
they read as outstanding only because Paul is still on 1.0.25 (126):
- **B1** — magnitude / distance / intensity sit in their own row ABOVE the
  I'M SAFE button, with `flexShrink: 0`, so the graphic gives up space on a
  short phone instead of the numbers sliding under the button. Verified at
  390×844 and 320×568.
- **B1** — "Dismiss alert" was removed with #14; the only ways off the alert
  screen are I'M SAFE and I NEED HELP.
- **B4** — "woken by the siren" is gone from the whole app; the wording is
  "alerted by the siren". Zero matches remain in the app source.

Backend changes in the same landing need a **Publish**; dashboard changes need
the GitHub push. Order: Publish backend → push dashboard → build iOS.

## Version 1.0.28 — batch 6 B3 + aftershock rehearsal (18 Jun 2026)

Everything in 1.0.26 and 1.0.27 is in this build. Paul's installed build before
this is **1.0.25 (126)**. Bumped from 1.0.27 rather than adding silently to it:
1.0.27's contents had already been quoted to him, and a version number whose
contents change after it was announced is exactly the build-gap confusion that
cost eleven days on #169.

| Change | Ref |
|---|---|
| Tremor settings, third option: "Everything nearby — including tremors too small to feel" (clause moved into the TITLE) | batch 6 B3 |
| Diagnostics → "Rehearse an aftershock mid-answer": opens the alert screen and publishes a second alert through the real alert bus 12 s later, so the mid-answer case can be seen ON THE PHONE | Paul, 2026-06-18 |
| Diagnostics "fixes in this build" marker now names the version | process |

**Why the rehearsal button had to exist:** Home's Trigger Test Alert navigates
locally (`router.push("/alert?siren=1&test=1")`), so it delivers no push — and
once you are on the alert screen the button is behind you. There was therefore
no way to reproduce "a second alert arrives while I am part-way through
answering" on a real phone without broadcasting a live alert to every user. The
rehearsal uses the SAME bus and the SAME listener a real second push hits; the
only step it does not exercise is APNs delivery, and the on-screen text says so.

Verified in preview: opened the alert screen, tapped I NEED HELP, left the
injury sheet open unanswered — at 00:12 the amber "Another alert just arrived —
M5.1. Your answer below still applies." notice appeared with the sheet and the
part-finished answer untouched.

---

## 1.0.44 (build 44) — 22 Aug 2026

Paul's installed build before this is **1.0.41 (41)**. 1.0.42 and 1.0.43 were
never built: the work landed in the repo while the version stayed at 41, which
is why this jumps straight to 44 — the numbers Paul was quoted in conversation
are kept, and no build number is ever reused (Apple rejects that anyway).

| Change | Ref |
|---|---|
| One source decides whether the siren can sound, so settings and the home screen can no longer contradict each other | #280 |
| Permanent home-screen warning above the rescue code when the phone cannot sound the siren, show our messages, or share a location. Not dismissible; prints nothing when there is no problem | #281 |
| Home screen layout fixed at the cause — no absolutely-positioned bar over the content | #282 |
| Plain sentences, no shouting. Capitals only for IMMEDIATE, SERIOUS, MINOR and DROP. COVER. HOLD ON. | #283 |
| "Early warning" wording removed everywhere | #284 |
| Setup split into three screens, one decision each | #285 |
| Practice run asks where the siren came out; "my watch" restores the Watch reminder. The reminder can no longer be dismissed for good | #286 |
| Tremor notices held for 20 minutes (`apns-expiration`) instead of "deliver once, never store" | #287 |
| Diagnostics → "What's fixed in it" rewritten for this build, so a stale install is visible on the phone | process |

Dashboard: unchanged in this build. The #278 sentence-case change to the ask
history is built in `/app/memory/dashboard_build/index.html` but deliberately
NOT pushed — Paul chose to ship it with the next dashboard round.
