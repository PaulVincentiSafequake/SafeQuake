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
