# QuakeGuard — Earthquake Safety App

## Problem
A React Native / Expo mobile app that helps users practice earthquake preparedness and report themselves safe after a shake event.

## Screens
1. **Home (`/`)** — Hero banner "QUAKEGUARD", system-active status pill, 4 safety-protocol cards (Drop, Cover, Hold On, After), info blurb, sticky "Trigger Test Alert" CTA.
2. **Alert (`/alert`)** — Full-screen red gradient emergency state, pulsing warning icon (react-native-reanimated), LIVE ALERT timer, magnitude / distance / intensity metrics, primary green "I'm Safe" CTA, secondary "Dismiss alert" link.

## Key Behaviour
- Tapping **Trigger Test Alert** (`trigger-alert-btn`) heavy-haptic → navigates to `/alert`.
- Tapping **I'm Safe** (`im-safe-btn`):
  - Requests foreground location permission → captures lat/lng/accuracy (8s timeout).
  - Reads battery level + state via `expo-battery`.
  - Generates a stable `deviceId` on first use, cached in AsyncStorage.
  - POSTs JSON to `https://safequake.onrender.com/api/status`:
    ```json
    {
      "deviceId": "qg-...",
      "status": "safe",
      "client_name": "quakeguard-mobile",
      "timestamp": "ISO-8601",
      "location": { "latitude": .., "longitude": .., "accuracy": .., "error": null },
      "battery": { "level": 0..1, "state": "charging|full|unplugged|unknown" }
    }
    ```
  - On HTTP 200 → success toast "Report received. Stay safe." → auto-navigates home after 1.2s.
  - On error → warning toast with retry hint; button remains actionable.
- **Dismiss alert** returns home without POSTing.

## Design
- Personality 7 (Dark-First Tactical) — obsidian bg `#0F1115`, signal red `#FF3B30` accents, success green `#34C759` for the safe CTA.
- Deprecated `expo-av`/`Alert` avoided; toasts and pressables only.

## Stack
- Expo Router (file-based) · expo-location · expo-battery · expo-haptics · expo-linear-gradient · expo-image · react-native-reanimated · AsyncStorage · @expo/vector-icons (Ionicons).

## Permissions Declared
- iOS: `NSLocationWhenInUseUsageDescription` = "Share your location to report you are safe"
- Android: `ACCESS_FINE_LOCATION`, `ACCESS_COARSE_LOCATION`

## Business Enhancement Idea
Add an "Emergency Contacts" screen so tapping **I'm Safe** also fires an SMS/push to designated family members — pushes retention and daily-utility habit-forming beyond emergency-only use.

## Diagnostic: /api/cors-debug
- Public read-only endpoint that returns the deployed CORS allowlist, regex, and a "deploy fingerprint" (sha256-prefix + mtime + process start time of `server.py`).
- Echoes the caller's `Origin` header and reports `allowed` + `allow_reason` (`exact_match` | `regex_match` | `not_allowlisted` | `no_origin_header`).
- Purpose: tell code-vs-deploy drift apart instantly. If the dashboard is empty and this endpoint says `allowed=false` for the dashboard's origin, the fix is a redeploy — not a code change.

## Feature: Rescue-Code + Optional First Name
Helps on-site responders identify WHICH pin corresponds to the physical phone in front of them, especially when 2-3 trapped people are clustered within the same GPS accuracy radius.

### On the phone
- **Rescue code** — last 5 chars of the device ID, uppercased (e.g. `qg-1785757225898-jy34olbg` → `4OLBG`). Shown prominently on the main screen in a persistent pill (`RESCUE CODE: 4OLBG · NAME: Paul`) and on `/diag` for support/radio use. Not globally unique — designed only as a LOCAL tie-breaker after GPS proximity has narrowed candidates to 2-3 pins.
- **Optional first name** — single text field, asked once on first launch via an inline modal (skippable). Stored in AsyncStorage (`quakeangel_display_name`). Editable/removable anytime by tapping the pill. Sanitized locally (trim + strip ASCII control chars + 40-char cap) and re-sanitized on the backend as defence-in-depth. Unicode names (José, Aiko, 京) preserved.
- **Persistent lock-screen card** — after a successful `trapped` submission, a persistent local notification with `interruptionLevel: "passive"` is scheduled: `"Rescue Info · Paul"` / `"Rescue Code: 4OLBG"`. On iOS it lives in Notification Center + on the lock screen until dismissed (no wake, no sound — the siren already served its purpose). On Android it's a MAX-importance `sticky:true` card on a dedicated channel. Cancelled on I'm Safe or Dismiss. Identifier `quakeangel-rescue-info` so re-submissions replace, not stack.

### On the wire
- `POST /api/status`: accepts optional `display_name` field (`Optional[str]`, max 200 pre-sanitize).
- `GET /api/devices`: returns `short_code` (derived, not stored) and `display_name` alongside every device row.
- `GET /api/audit`: enriches every `status`, `rescued`, and `rescue_reverted` event with `short_code` and `display_name` (snapshotted at event time, so historical rows keep their name-of-record).

### Backwards compatibility
- Existing check-ins without `display_name` continue to work — the dashboard falls back to `short_code`-only rendering (or raw `device_id` if `short_code` can't be derived, e.g. very short IDs).
- Snippets accept both a raw `deviceId` string OR the full device object.

## EMSC licensing — resolved 2026-08-04

- FDSN Event service (`fdsnws/event/1/query`) is distributed under **CC BY 4.0**
  per its own per-service documentation at `seismicportal.eu/fdsn-wsevent.html`.
  The umbrella `terms.html` page's default "all rights reserved, non-commercial only"
  clause does NOT apply to this service — the umbrella terms explicitly defer
  to per-service docs, and this service's docs grant CC BY 4.0.
- Testimonies (felt reports) are separately CC BY 4.0 per the umbrella terms.
- User has ALSO emailed EMSC (contact@emsc-csem.org) for written confirmation
  as a belt-and-suspenders documentation step, but we are NOT blocked pending
  their reply — the per-service license grant is self-executing.

### Attribution obligations under CC BY 4.0 (must ship with Phase 2)

- Every auto-triggered quiet notification body ends with `Source: EMSC`.
- `/quake/[unid]` screen shows full credit: `Data © EMSC/CSEM · CC BY 4.0 · seismicportal.eu`,
  license URL tappable.
- App About/Credits screen carries the same full attribution + a one-line
  "indicate changes" clause: "Threshold-based alerting decisions are our own;
  underlying event data is unmodified from EMSC."
- Dashboard audit rows for `triggered_by = "emsc-auto:*"` credit the source
  inline in the audit UI.

## Provider strategy — EMSC + USGS parallel from day one

- USGS FDSN Event service (public domain, US government work) will be
  stood up as a first-class parallel provider from Phase 1, not a fallback
  afterthought. This gives us:
    (a) Commercial-safety redundancy independent of EMSC's licensing.
    (b) Cross-provider corroboration — an event seen by both providers
        is higher confidence than either alone; useful signal for the
        threshold evaluator (future v2 enhancement).
- Provider code lives in `backend/emsc/providers.py` (module structure:
  `emsc/providers.py`, `emsc/evaluator.py`, `emsc/poller.py`, `emsc/seed.py`).

## EMSC Phase 1 — Shadow-mode soak (landed 2026-08-05)

In-process asyncio poll loop, 60s cadence, both providers concurrent,
zero user-facing pushes. Runs for 1-2 weeks to gather calibration data
before any live firing.

### Design decisions (per user direction, 2026-08-05)
- **Wide-net polling:** min_magnitude 2.5, radius 600km, NO depth filter.
  Rationale: filtering during soak destroys the data needed to set the
  filter. Shadow-mode logging is cheap; log everything remotely relevant.
- **Multi-set evaluation:** every event is scored against ALL threshold_sets
  in one pass. Malta seeds `quiet_tier` (M3.0+/100km/sev2.0),
  `critical_tier` (M5.0+/300km/sev4.0), and `neo_original` (M4.0+/300km/sev3.0).
  Answering "how often would quiet_tier have fired?" is a single DB query.
- **No cross-provider dedup:** EMSC and USGS are stored side-by-side and
  tagged by provider. Divergence between the feeds is precisely the signal
  soak is designed to observe. Cross-matching is a Phase 2 concern.
- **Revision tracking:** repeat polls of the same `(provider, external_id)`
  are deduped on unchanged content, but any change to
  magnitude/lat/lon/depth writes a new row with `revision: n+1`.
  Preserves the timeline so we can measure how often a magnitude revision
  would flip an event across a threshold boundary (drives future
  escalation-alert design).
- **Severity formula:** `magnitude - log10(distance_km/10) - depth_km/200`.
  Log-decay on distance, gentle depth penalty. Tunable in
  `emsc/evaluator.py:severity_score()`.

### Admin endpoints (JSON, admin OR operator role)
- `GET /api/admin/emsc/health` — per-provider poller health, `healthy` boolean,
  last_success_at, consecutive_failures, counters. Uptime-monitorable.
- `GET /api/admin/emsc/recent` — recent events with filters: `limit`,
  `since` (ISO-8601), `would_have_fired` (bool), `threshold_set`, `provider`,
  `country_code`. All filters combine with AND; the would/threshold/country
  filters use `$elemMatch` so they all apply to the SAME evaluation entry.
- `GET /api/admin/emsc/config/{country_code}` — inspect the active thresholds.

### Collections created
- `country_configs` — one doc per supported country (Malta seeded on first boot).
  Idempotent — never overwritten by redeploy.
- `emsc_events` — one row per event revision. Indexed on
  `(provider, external_id, revision)` unique.
- `emsc_poller_health` — one doc per provider (upserted). Source of truth
  for "is the poller alive".

### Phase 2 (NOT in this landing)
- Cross-provider dedup / corroboration signal
- Circuit breaker + exponential backoff (Phase 1 uses simple retry)
- Move poller to a separate worker process (Phase 1 is in-process for
  inspection simplicity; safe because no pushes fire)
- Firing pushes based on `would_have_fired` — gated by manual flip of
  `country_configs.shadow_mode: false` after soak analysis

## Task #9 — Per-user Google sign-in (landed 2026-08-05)

Replaces the shared `X-Admin-Token` shared-secret with per-user identities
backed by Google Identity Services (ID-token flow, no client secret, no
redirect callback). Direct architectural fix for the security-incident
failure class from 2026-08-04.

### Architecture
- Dashboard renders Google's sign-in button via GIS.
- On success, GIS returns a Google-signed ID token to JS.
- JS POSTs it to `/api/auth/google` → backend verifies signature/audience/
  issuer/expiry via `google-auth`, looks up the email in our `users`
  allowlist, issues a 15-min HS256 JWT.
- JWT stored in `sessionStorage` (cleared on tab close — appropriate for
  shared dispatcher workstations). Sent as `Authorization: Bearer <jwt>`
  on every admin call.
- Backend re-checks `users.allowed/disabled/session_version` on every request
  so disabling an operator invalidates their JWT immediately, not on next expiry.

### Roles (MVP)
- **admin** — everything, incl. user management + redact-notes.
- **operator** — trigger-alert, mark/unmark-rescued, view audit; cannot
  touch users or redaction.

### Legacy soft-cutover
- `LEGACY_TOKEN_ENABLED=true` in .env keeps `X-Admin-Token` working during
  dashboard-side migration. Legacy callers attributed as `legacy@dashboard`
  in audit trail (grep-friendly for cutover-progress tracking).
- Flip flag to `false` in .env → shared secret is dead, JWT-only.

### Files
- `backend/auth.py` (new, ~240 LOC) — JWT + Google ID token + principal resolution.
- `backend/server.py` — bootstrap first admin on startup; four migrated admin
  endpoints (`mark-rescued`, `unmark-rescued`, `redact-notes`, `trigger-alert`);
  new `/api/auth/google`, `/api/auth/me`, `/api/auth/logout`, `/api/auth/revoke-me`,
  `/api/admin/users` (list/create/disable/enable).
- `memory/dashboard-auth.snippet.html` (new) — shared GIS button + `qaApi()` wrapper.
- `memory/dashboard.js.snippet` — swapped password prompt for JWT via qaApi.
- `memory/dashboard-mark-rescued.snippet.html` — same.
- `memory/dashboard-audit-log.snippet.html` — same (also gets notes when signed in).

### Attribution
- `push_events.triggered_by` — now the authenticated user's email (was `"dashboard"`).
- `status_events.rescued_by` / `reverted_by` — same.
- Redaction marker on notes — includes the redactor's email inline.
- Historic rows untouched. New rows properly attributed from cutover onward.

### Test users / bootstrap
- `pmvincenti@gmail.com` seeded as first admin on backend startup.
- Additional operators added via `POST /api/admin/users` + Google Cloud
  Console test-user allowlist (consent screen is in Testing status).

## Subscription lapse handling (planned — landing after EMSC Phase 1)

### Business model (decided 2026-08-05)
- €2.99/year, auto-renewing, everything included. No free/paid split of safety features.
- Framed as cost recovery, not profit.
- Plan values: "individual" (€2.99/yr) at launch. Schema keeps room for "b2b_hotel", "b2b_school", "b2b_care_home".

### iOS payment path — StoreKit + ASSN v2 (NOT Stripe)
- Apple App Store Review Guideline 3.1.1 requires In-App Purchase for any subscription unlocking in-app functionality. Stripe would be a rejection.
- Entitlement truth: App Store Server Notifications V2 (Apple pushes DID_RENEW, EXPIRED, GRACE_PERIOD_EXPIRED, DID_FAIL_TO_RENEW, REFUND, etc.) + receipt validation.
- `subscriptions.entitlement_ends_at` mirrors Apple's `expiresDate` — never our scheduler's guess.
- Apple's own billing grace period (configurable in App Store Connect up to 16 days) handles most card-retry cases automatically. Our 14-day grace period sits AFTER Apple's expires, not duplicating it. Composition: [Apple auto-renew] → [Apple billing grace, up to 16d] → [Apple expiresDate reached] → [our 14d grace period, aggressive banners] → [expired NOT PROTECTED state].
- Reactivation from NOT PROTECTED state is a StoreKit purchase / subscription-management sheet (`SKPaymentQueue.presentCodeRedemptionSheetIfEligible` / `showManageSubscriptions`), not a Stripe checkout URL.

### Execution order (confirmed 2026-08-05)
1. Finish task #9 — user Publishes and validates the Google sign-in end-to-end.
2. EMSC/USGS Phase 1 shadow mode — starts the mandatory 1-2 week soak clock ticking, inert to users. Priority-first because the soak can't be shortened.
3. Subscription lapse Phase A+B in parallel while EMSC soaks — state machine + audit trail + in-app UI + acknowledgement. Get in front of user for feedback.
4. Phase C (push warnings), D (email via Emergent Resend), E (StoreKit reactivation) as follow-ups.

### Copy shipping rules (per 2026-08-05 sign-off)
- Never "subscription", "payment", "billing". Always "cover" + explicit safety consequence.
- 1-day warning MUST state exact date + time in user's local timezone, not "tomorrow."
- "You will not be alerted to earthquakes near you" is accurate under €2.99-everything-included and is the strongest line — keep it.
- Consequences bolded at each tier.
- One-tap "Reactivate now" CTA on every warning.
- No cheerful marketing language. Only ⚠️ emoji, on the terminal NOT PROTECTED state.

### Apple HIG check
- Deferred to start of Phase A+B implementation (not now). Focus is on §4.2.2 (coercive UX) — the design already has the mitigation (3s-hold secondary action), plus specific check on subscription-lapse messaging rules. If HIG suggests softening further, prioritise "impossible to misunderstand" over "impossible to dismiss" — evidence comes from the recorded ack, not from trapping them.

## Subscription lapse — copy & UX rules locked 2026-08-05

- **Noun = "protection"** throughout. Never "cover" (insurance jargon, BrE-inflected — bad for Malta's bilingual/tourist market) or "monitoring" (cold, passive). Consistency with the NOT PROTECTED terminal state is a safety feature, not a style preference.
- **Responder accuracy rule** (locked): current phrasing is "you will not appear on the emergency response dashboard" — accurate today. NEVER "rescuers will not see you" until Civil Protection or equivalent responder org is contractually watching. Same principle applies to store listings and marketing copy.
- **Mirror rule** (locked): NEVER tell a user they're unprotected when they are. During Apple's billing grace period, protection is still active; the warning must reflect that (yellow banner, "your protection continues until [date]") — no red, no NOT PROTECTED state. False alarms train people to ignore the warnings that count.
- **Accessibility rule** (locked): hold-to-confirm is a trap for elderly / tremor / arthritis / motor-impaired users — precisely our highest-risk lapse cohort. Hold gesture is a shortcut for able users only. Primary path for anyone who can't hold (short-tap, VoiceOver, TalkBack, Switch Control, reduced-motion) is a two-step confirm dialog. Both paths record identical acknowledgement events. Hold duration: 2 seconds with visible progress ring (not 3s — long enough that users release early thinking it's broken).
- **Statutory language rule**: EU disclosure (auto-renewal, 14-day withdrawal, pre-contract info, easy cancel) sits with Paul as seller, not with Apple. Copy doc uses `[STATUTORY_TEXT_TBD]` placeholders; lawyer to draft. Never invent statutory wording.

## Backlog — scoped 2026-08-05, to build after EMSC soak + subscription A+B

### Session idle timeout (P2, before/alongside subscription A+B)
- **Why:** Paul's concern — an operator who walks away leaving the dashboard tab open lets the next person act under their identity, quietly undermining the entire per-user attribution we just built in Task #9.
- **Design:** 15 minutes of no activity → session expires → re-sign-in required. Configurable. Activity = mouse/keyboard/click. Calls `qaAuth.signOut()` internally so the same audit trail path fires.
- **Not per-action friction** — decided against re-prompting on Undo etc. Idle timeout is the right knob for the unattended-dashboard case; per-action friction just trains operators to avoid the action.
- Belongs in `dashboard-auth.snippet.html` so all dashboards inherit it once.

### Audit log export (P3, after subscription A+B)
- Admin-only export of `/api/audit` rows.
- Formats: CSV + PDF.
- Filters: date range.
- **Respects notes-behind-auth contract** — operator-role export omits note text (only `notes_present: true` boolean); admin-role export includes note text verbatim. Different endpoint per role, not a runtime flag.

### Printable casualty reports — TWO distinct documents (P3)
- **B1 — Operational report** (rescue teams, Civil Protection):
  - Full detail: per-person short code, first name, coordinates + accuracy, status, severity, mobility, last-updated, battery %.
  - Grouped by triage: Immediate / Serious-Stable / Minor / Not responding / Safe / Rescued.
  - Count summary top of page.
  - "Generated at [exact date + time, local]" prominently — snapshot of a moving situation.
  - **"CONFIDENTIAL — OPERATIONAL USE ONLY"** watermark on every page.
  - Admin/operator auth required.
- **B2 — Public/media summary**:
  - Aggregate counts ONLY. No names, no short codes, no coordinates, no per-row detail, no health/mobility data.
  - Format: "As at HH:MM on DATE: N people have reported themselves safe. N have reported being trapped. N have been confirmed rescued."
  - **GDPR rationale (locked):** injury and mobility data is arguably special-category health data under Article 9. Sending casualty detail to press is both a legal risk AND a human one — families should not learn a relative's condition from a news bulletin.
- **Structural separation** (locked): different endpoints, different buttons, different visual styling, explicit label on each. Impossible to generate B1 and hand it to press by accident.

### Dashboard category filter (P3)
- Sidebar filter — one triage category at a time (trapped / rescued / walking wounded / etc). Extends the map filter buttons pattern to the list view.
- **Rescued view specifically:** show `rescued_at` timestamp next to each entry.
- **Wire to exports:** "filter the view → export exactly what you're looking at" is the natural workflow. Both B1 report and CSV export should respect the active filter.
