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

### Operational standard (locked 2026-08-06)

**No terminal-required final steps.** If a feature's activation path requires the account owner to run a curl command after landing, the feature is not complete. Every ongoing admin operation must ship with a UI — dashboard panel, mobile-app control, or equivalent one-click surface. Applies to: adding operators, enabling/disabling features, kill switches, config toggles, expiry renewal, all similar operations.

Rationale: Paul is the operator, not a developer. Assuming there's a developer between him and the product produces stalled deliveries and a false sense of "done." Curl commands during initial setup (bootstrap admin, one-time production migration) are acceptable if I run them myself; asking the account owner to run one is not.

### EMSC preview mode dashboard UI (landed 2026-08-06)

`dashboard-preview-mode.snippet.html` — admin-only self-service panel:
- Enable/disable toggle
- Trigger tier dropdown (all_ingested / quiet_tier / critical_tier / neo_original)
- Rate-limit input (1-1440 minutes)
- "Recent iOS registrations" list with one-click "Add to preview" buttons
- Enrolled-devices list with per-row remove
- Recent notification activity log (delivered / skipped / failed, colour-coded)
- Big red PANIC KILL button (with confirm) — disables preview mode across ALL countries
- Auto-refreshes after every action
- Admin-role gated via `qaAuth.user().role === "admin"`; entirely hidden for operators

### Phase 2 (NOT in this landing)
- Cross-provider dedup / corroboration signal
- Circuit breaker + exponential backoff (Phase 1 uses simple retry)
- Move poller to a separate worker process (Phase 1 is in-process for
  inspection simplicity; safe because no pushes fire)
- Firing pushes based on `would_have_fired` — gated by manual flip of
  `country_configs.shadow_mode: false` after soak analysis

### Preview mode (landed 2026-08-06 — P2.5, between Phase 1 and Phase 2)
- Sends REAL non-critical notifications to an allowlisted device (Paul's phone) while `shadow_mode` stays true for every other device. The soak is completely undisturbed.
- **Why now, not at Phase 2 launch:** shadow mode validates detection but not DELIVERY. Zero evidence today that "EMSC reports an event → notification appears on real iPhone lock screen" works end to end. Finding that out on go-live day is the worst possible timing. Also turns threshold-tuning from guesswork into lived experience.
- **Non-negotiable constraints (locked):**
  1. NEVER the Critical Alerts path. Regular `interruption-level: "active"` only. Misusing the critical entitlement risks Apple revoking it.
  2. Notification body always prefixed `PREVIEW · ` and suffixed `. Test notification, no action needed.` — prevents alert fatigue on the channel we need Paul to trust.
  3. Audit-tagged in `emsc_preview_notifications` collection — never conflated with real `push_events` triggers.
  4. Rate-limited (default 10 min/device) — a swarm sequence can't produce 50 notifications in an hour.
  5. `POST /api/admin/emsc/preview/kill` = one-request panic stop, disables preview on ALL countries.
  6. Cross-device isolation via explicit allowlist — no device receives anything without being in `preview_mode.device_ids`.
- **Config:** `country_config.preview_mode = {enabled, device_ids, trigger_tier, rate_limit_minutes}`. `trigger_tier` accepts `all_ingested` (fire for every stored event) or a threshold_set name (`quiet_tier`, `critical_tier`, `neo_original`).
- **Admin endpoints:**
  - `GET/POST /api/admin/emsc/preview/config?country_code=MT`
  - `POST /api/admin/emsc/preview/add-device` — convenience to append a single device_id
  - `POST /api/admin/emsc/preview/kill` — panic stop
  - `GET /api/admin/emsc/preview/candidates` — list recent iOS device_ids to pick from
  - `GET /api/admin/emsc/preview/recent` — audit trail including rate-limited skips (honest volume signal)
- **APNs path:** `apns.send_preview_alerts()` — `apns-priority: 5` (power-efficient), `sound: default`, `interruption-level: active`, payload includes `preview: true` top-level for mobile app styling if desired.
- **iOS-only for v1.** Android via Emergent push relay is a future addition — Paul's device is iOS so this doesn't block v1.
- **TEST-ONLY radius override (landed 2026-08-07):** operators can widen the preview radius per-country (e.g., Malta 2000 km to catch Greek/Turkish quakes) without touching the real 600 km alert boundary that real users see. Field: `country_config.preview_mode.preview_radius_km_override` (100–5000 km, server-enforced). Server also stamps `preview_radius_km_override_expires_at = set_at + 7 days`; the evaluator ignores the override once it expires, so the setting **can never be silently left on when real users arrive**. Preview notifications generated only because of the override get the body prefix `⚠️ Beyond alert zone — ` so a 1800 km preview can never be visually mistaken for a 600 km-boundary alert. Every set/clear is logged in `emsc_audit_log` (`kind: preview_radius_override_change`, with from→to values and expiry). Real-alert path is architecturally isolated: `dispatch_preview_if_needed` is the ONLY code path that reads the override field. Locked-in invariants (5 unit tests in `tests/test_preview_radius_override.py`):
  1. Athens (830 km) skipped without override
  2. Athens fires with override + Beyond-alert-zone prefix
  3. Expired override → falls back to 600 km + skip reason annotated
  4. Sicily (200 km, inside 600 km) fires without prefix (event is inside real zone)
  5. Auckland (18000 km) never fires even at max 5000 km override

### Continuity tracking (landed 2026-08-06 after 18h gap incident)
- **What went wrong:** initial Phase 1 landing had no persistent continuity check. Credit exhaustion suspended the pod for ~18h; on resume the poller restarted but the `poller_started_at` field got overwritten with the current-process start time. `total_polls` counter also reset (Mongo persistence quirk under pod suspension). No way to distinguish "quiet period" from "was dead" — would have contaminated threshold-tuning on day 14.
- **What was added:**
  - `emsc_soak_meta` singleton with authoritative `soak_started_at`. Only reset by explicit admin action; survives all restarts.
  - `emsc_poller_gaps` collection. On poller startup, compare persisted `last_success_at` (per provider) to `now`. Any gap ≥3× poll interval writes a row: `{provider, gap_start, gap_end, gap_seconds, detected_at, detection_reason}`. Unique index on (provider, gap_start) prevents duplicates.
  - `GET /api/admin/emsc/continuity` — soak_started_at, wall_seconds, dead_seconds, **coverage_pct**, gap list, reset_history. THIS is the number to quote when claiming "we've soaked for N days".
  - `POST /api/admin/emsc/reset-soak-clock` — admin-only, `confirm: true` required, `reason` required (audit trail).
- **Contract:** any tuning decision on day 14 requires reading `/continuity` first and confirming `coverage_pct` is acceptable. "We polled for 14 days" is dishonest if coverage_pct is 60%.

## Bug fixes 2026-08-06 — preview-tap siren + hardcoded alert values + worldwide-preview

### BUG-2026-08-06-preview-worldwide (feature-sinking)

**Symptom:** A preview notification landed on Paul's phone for an event **10,834 km WSW of Malta** (M3.2, Pacific). Malta config specifies `poll_radius_km: 600` and the dashboard tier dropdown labels `all_ingested` as "All ingested events (M2.5+, ≤600km)". Neither was being honoured.

**Root cause:** `should_send_preview()` had a short-circuit for `all_ingested` that returned `True` unconditionally, bypassing the evaluator entirely. Since the poller ingests worldwide M2.5+ feeds, `all_ingested` literally meant "every event on Earth". The narrower tiers (`quiet_tier`, `critical_tier`) happened to be safe because their `threshold_sets` enforced `max_distance_km` internally.

**Fix:**
1. `should_send_preview()` now takes `distance_km` and `poll_radius_km` as arguments. A HARD radius gate applies to EVERY tier including `all_ingested` — an event beyond `poll_radius_km` never fires a preview, regardless of tier.
2. Radius gate is checked BEFORE the tier logic so no threshold_set can bypass it.
3. Distance is computed once at dispatch time (from evaluations, falling back to haversine on event coords) and passed to both the gate and the notification body formatter.
4. Every skipped dispatch now writes an audit row to `emsc_preview_notifications` with the specific `skipped_reason` (`beyond_country_radius (10834km > 600km)`, `tier_did_not_match (quiet_tier)`, etc.) — so Day-14 review can spot false negatives, not just false positives.
5. Return type changed to `(bool, Optional[str])` — reason string surfaced everywhere for audit clarity.

**Contract locked:** the tier controls sensitivity WITHIN the region. It never controls whether the region applies at all. Every future preview-related tier must pass through the radius gate.

**Also fixed same session — F-M 2010 anelastic attenuation term:**
Original coding of Faenza-Michelini 2010 IPE omitted the `-0.00189*R_hypo` anelastic-attenuation term. That made the equation systematically over-predict at long distances (M6@400km returning MMI 8.4 instead of ~6.8). Added the term. The formula still runs somewhat hot at close range (M6.5@20km returns 10.5 vs empirical 7-8) — this is a KNOWN limitation of all short-form IPEs, and it's DELIBERATELY OK given the asymmetric-cost bias. Day-14 comparison against EMSC testimonies will quantify the offset and we can calibrate down (never up).

### BUG-2026-08-06-preview-tap-siren (safety-critical)

**Symptom:** Tapping a preview notification (M2.7 event ~1,300km away, correctly delivered as quiet non-critical) opened the full EARTHQUAKE DETECTED screen with siren + "Drop. Cover. Hold on." — the exact alert-fatigue failure preview constraints existed to prevent.

**Root cause:** `_layout.tsx` tap handler defaulted to `/alert` for any notification without an explicit `action_url`. Preview payload set `action_url: "/"` but that wasn't strong enough as a signal — the whole architecture depended on the sender writing the right URL rather than the receiver knowing the intent.

**Fix:**
1. **Payload contract:** every APNs payload MUST carry a `kind` field (`critical_alert`, `emsc_preview`, or `quakeguard-reminder`). Fail-safe on the mobile side treats a missing/unknown kind as INFORMATIONAL, never critical.
2. **Backend:** `_build_critical_payload` and `_build_preview_payload` in `apns.py` both now embed `kind` at payload root. Extended signatures forward event details (magnitude, distance_km, intensity, depth_km, region, unid, provider).
3. **Frontend tap handler (`_layout.tsx`):** routes by `kind`:
   - `critical_alert` → `/alert?siren=1&...event details`
   - `quakeguard-reminder` → `/alert?siren=0&reminder=1` (check-in flow, no siren)
   - `emsc_preview` / unknown → `/quake/[unid]` (informational)
   - `/alert` from any non-critical kind is BLOCKED, prevents future regressions.
4. **`/alert` siren gate:** `shouldPlayRef` now initialised from `params.siren === "1"` (default false). Only the tap handler for `kind: "critical_alert"` sets `siren=1`. Direct navigation, dev browsing, or a malformed payload can never fire the siren.
5. **New screen `/quake/[unid]`:** calm informational detail — magnitude, distance, depth, coordinates, region, time-ago, EMSC attribution ("Data © EMSC"). Deliberately NO siren, NO "Drop. Cover. Hold on.", NO check-in prompt. `PREVIEW` badge when the tap was on a preview. "This is a record of what has already happened. Earthquake detection is not early warning." footer.

**Fail-safe philosophy (locked):** a missed siren on tap is recoverable (the notification itself carried siren + haptics if it was truly critical); a spurious siren on tap destroys trust permanently. Every ambiguous path routes to informational.

### BUG-2026-08-06-alert-hardcoded

**Symptom:** `/alert` screen displayed `MAGNITUDE 6.4 · DISTANCE 12km · INTENSITY VII` regardless of the actual triggering event — literal hardcoded strings, no connection to payload.

**Root cause:** placeholder mockup values shipped as literal JSX text and never replaced with dynamic data.

**Fix:**
1. `alert.tsx` reads via `useLocalSearchParams` — `magnitude`, `distance_km`, `intensity`, `depth_km`, `region`, `unid` all sourced from URL params set by the tap handler.
2. Missing fields render as literal `"—"`. Never a default. Never a stale value from a previous event.
3. Backend `send_critical_alerts` extended to accept event fields; `server.py:trigger_alert` forwards `body.magnitude`, `body.distance_km`, `body.intensity` into the payload.
4. Tap handler encodes fields as URL params before pushing to `/alert`.

**Contract:** during a real earthquake, showing wrong magnitude/distance would be worse than showing nothing — someone could conclude a distant event was on top of them, or the reverse. `"—"` communicates "we don't have this data" honestly, unlike a hardcoded default.

## Intensity-based alerting — the reframe (scoped 2026-08-06)

**Fundamental correction of what we alert on.** Magnitude describes energy released at the source; it says nothing about what a person in Valletta actually feels. Human experience = intensity (MMI/EMS-98) — a function of magnitude, distance, depth, and local soil conditions. Every mature system (ShakeAlert, JMA) alerts on predicted intensity, not raw magnitude. Magnitude thresholds are systematically miscalibrated — M4.5 at 20km is a far bigger event than M6.0 at 400km, and current rules treat them as comparable.

### Proposed intensity tiers (locked)
| Tier | Predicted MMI | Meaning | Delivery |
|---|---|---|---|
| Informational | III–IV | Felt indoors by some | Silent notification / map only |
| Standard alert | V | Felt by nearly everyone, small objects fall | Normal notification, prompts check-in |
| Critical | VI+ | Damage begins | **Siren** (critical-alert entitlement) |

### Deliberate asymmetric bias (locked)
Ground-motion-to-intensity conversion is uncertain (GMICE equations disagree by >1 MMI unit, worse near-source). **Alert on the optimistic edge of the uncertainty band.** If our estimate spans MMI V–VI, treat it as VI. Missed alert can kill someone; false alarm is irritating. Cost asymmetry is real, code must reflect it. Document explicitly in intensity code so nobody "optimises" it as noise reduction.

### Data sources — use existing before building physics (research done 2026-08-06)
1. **USGS `properties.mmi`** — ShakeMap-derived, present ~14% weekly M2.5+ events, ~100% significant events. Use directly when present.
2. **USGS `properties.cdi`** — DYFI community-reported, ~22% weekly. Secondary confirmation.
3. **EMSC Testimonies API** — `https://www.seismicportal.eu/testimonies-ws/api/search?unids=X&includeTestimonies=true`. Returns raw + corrected EMS-98 intensities per location once ≥~5 respondents. **Ground-truth validation channel, not primary trigger** (~1h latency). Same CC BY 4.0 licence. ~470k reports/year.
4. **Own GMPE + GMICE** — only when 1–3 give no signal. Published Mediterranean-region attenuation relation, cited in code. Never invent physics.

### Soak-phase enhancement (Part 1a — MUST land before Day 14 tuning)
- Add `intensity_estimates` to each `emsc_events` doc:
  ```
  intensity_estimates: {
    at_malta_center: {
      mmi_from_usgs: 4.2 | null,
      cdi_from_usgs: 3.8 | null,
      mmi_predicted: 4.8,
      mmi_predicted_upper_band: 5.7,   // alarming-edge value used for tier decision
      gmpe_used: "Akkar-Bommer-2010",
    },
    from_emsc_testimonies: {
      max_intensity: null,
      report_count: 0,
      last_updated: null,
    }
  }
  ```
- Add three intensity threshold_sets alongside existing magnitude ones (`intensity_informational`, `intensity_standard`, `intensity_critical`). Magnitude sets continue running in parallel — day-14 comparison against ground-truth is the whole point.

### Felt-report follow-up sweeper (new background task)
- Every 15 min, sweep `emsc_events` ingested in last 72h that don't yet have final EMS-98 data.
- Batch-fetch via EMSC testimonies API (`unids=A,B,C&includeTestimonies=true`).
- Update `intensity_estimates.from_emsc_testimonies` in place (not revision-tracked — this is a running best-known value).
- **This is the validation signal.** No other small safety app has this.

## Seismic map (Part 2, scoped 2026-08-06)

**Core, not decoration.** Malta's intensity ≥V return period is ~18 years, ≥VI ~40 years. A correctly-tuned critical alert fires roughly twice in a lifetime. Between those events the app appears to do nothing — and people cancel subscriptions to apps that appear to do nothing. **The seismic feed is what keeps Quake Angel installed, trusted, and paid for during decades of quiet.** Which means it's what ensures the safety function still exists when it's finally needed.

### Design (locked)
- Full Mediterranean map, not just Malta-region.
- Per event: epicentre pin, magnitude, depth, distance from user, time-ago. Tap for detail.
- "Show minor tremors" toggle.
- **Absolute rule: map filter ≠ notification filter.** Map is WIDE (informational); notifications are NARROW (intensity-based). If map breadth ever leaks into notification logic, trust in alerts dies.
- On real alert firing, show epicentre + distance so users understand what happened.
- **EMSC attribution required on this screen** — licence condition, committed in writing.
- **Never imply early warning.** No "incoming", no countdowns, no predictive framing. Physics doesn't allow it; claiming it would be misrepresentation.
- Plain language, first-time-reader assumption. No "epicentre" without a "where the earthquake started" annotation.

## User-configurable notification sensitivity (Part 3, scoped 2026-08-06)

"My Lightning Tracker" pattern (distance slider drawing approximate radius circle, plain-language statement), with two hard adaptations:

### **RULE (locked — cannot be overridden)**
**The user controls the QUIET tier only. Never the siren.** A critical-intensity event (MMI VI+) MUST fire the full alert regardless of user preference. Settings screen must state explicitly:

> These settings control informational notifications about nearby tremors. Alerts for dangerous earthquakes are always on and cannot be switched off.

### Presets (not raw distance)
Earthquake significance = magnitude × distance × depth. A pure distance slider gives false confidence ("60km, so I'm safe from further"). Use predicted intensity as underlying variable:

| Preset | Threshold | Label |
|---|---|---|
| Significant only | predicted MMI IV+ | "Only events I'd properly feel" |
| **Noticeable (default)** | predicted MMI III+ | "Anything I might notice" |
| Everything nearby | all detected in region | "Every tremor in the region" |

Draw approximate radius circle on the map (labelled approximate — real boundary is intensity-shaped, not circular). Circle is UX communication, not a real filter.

### Map behaves independently
Map always shows full Mediterranean regardless of notification preset. Preset governs only what interrupts.

### Location handling
"Last known location" occasional-not-continuous. Feeds GDPR minimum-collection.

## Priority (updated 2026-08-06 evening)

1. ✅ EMSC Phase 1 soak (continues — magnitude data still useful)
2. ✅ Intensity soak enhancement (Part 1a) — GMPE + testimonies landed 2026-08-06
3. ✅ User notification presets (Requirement 1 / Part 3) — landed 2026-08-06. Mobile screen at `/app/frontend/app/settings/notifications.tsx` with 4 peer presets (Off / Significant / Noticeable-default / Everything nearby), OS-Critical-Alerts-revoked banner, and the mandatory safety copy ("Alerts for dangerous earthquakes are always on and cannot be switched off"). Backend: `POST/GET /api/devices/notification-preset` on `user_presence` collection.
4. ✅ In-app Seismic Map (Part 2) — landed 2026-08-06. Mobile screen at `/app/frontend/app/map.tsx` (with `/app/frontend/src/components/MapCanvas.native.tsx` for the native map). Mediterranean-wide event map from EMSC+USGS via new public `GET /api/seismic-map/events` endpoint. Two-pass dedup (revisions + cross-provider merge). Indicative radius circle: **600 km SOLID** for "Everything nearby" (= real poll radius boundary), 300 km / 200 km **DASHED** for Noticeable / Significant (UX communication only). "Off" hides the circle. Non-early-warning disclaimer chip pinned at top. EMSC attribution in footer. Web fallback is a chronological event list.
5. ✅ Subscription lapse A+B (entitlement state machine + in-app banner) — landed 2026-08-06. Backend: `/app/backend/entitlements.py` state machine (never_subscribed / active / grace / lapsed) with three admin-gated endpoints (`/api/entitlement`, `/api/entitlement/test-override`, `/api/entitlement/test-override/clear`). Grace period = 7 days. Frontend: `/app/frontend/src/components/EntitlementBanner.tsx` on home. **INVARIANT: `critical_alerts_active=true` in every response, "Critical alerts still work." in every banner body — enforced backend-side, audited client-side (banner refuses to render if backend violates invariant).** Option A locked with Paul: critical alerts always free; premium tier gates convenience features only. Phase C (real Apple StoreKit + ASN2 webhook + purchase UI) still pending.
6. **In-progress: Production migration** (post Emergent Support reply) — 7 migration-prep artifacts landed 2026-08-06:
   - `/app/backend/Dockerfile` — production FastAPI container tuned for Fly.io
   - `/app/fly.toml` — Fly.io app config (region: fra, min_machines_running=1, auto_stop_machines=off — poller must never sleep)
   - `/app/scripts/migrate_mongo.py` — JSONL-based Mongo migration tool, smoke-tested end-to-end (13/13 collections round-trip with all custom indexes)
   - `/app/memory/migration-checklist.md` — 10-step cutover runbook + rollback plan
   - `/app/memory/env-inventory.md` — every env var + secret mapped from Emergent to Fly.io
   - `/app/memory/emergent-support-email.md` — drafted email covering the 5 blocking questions (reliability tier, DB export, integration continuity, App Store implications, GitHub export scope)
   - `/app/backend/tests/test_emsc_continuity_migration.py` — 6-test regression suite that runs pre/post migration to verify soak continuity is preserved
   - **GITHUB PUSHED 2026-08-06**: hand-pushed to `https://github.com/PaulVincentiSafequate/quake-angel-app` (private) from inside the pod. Emergent's built-in Save-to-GitHub UI was broken for this user. Sanitization commit `e90660e` scrubbed leaked admin tokens across 13 tracked test files + `test_result.md` before push. `backend/.env`, `frontend/.env`, `memory/test_credentials.md`, `test_reports/` all correctly excluded (404 on GitHub API). PAT `github_pat_11CJLEDEA...` used for the push was **REVOKED by Paul on 2026-08-06** immediately after verification — zero live credential exposure remains.
   - **STATUS: ON HOLD 2026-08-07.** Emergent replied to the support email. Migration paused because (a) moving the backend forces a full rebuild + fresh App Store / Google Play review per re-submission — a real cost given ~3-7 day review cycles that break EMSC soak continuity, and (b) Emergent cannot support us if we leave. Paul waiting on their pricing for a reliability/SLA tier before deciding. Migration artifacts (Dockerfile, fly.toml, migrate_mongo.py, checklist, env-inventory, continuity test) remain in the repo — inert but ready if we ever unfreeze. The 16-hour gap on 2026-08-06 remains the strongest argument for eventually migrating; support pricing must beat that risk.
   - GitHub off-platform backup was completed (private repo `PaulVincentiSafequake/quake-angel-app` at commit `e90660e`) — that value stands independent of the migration decision.
7. ✅ **User Management dashboard (Task 4)** — landed 2026-08-06. Backend: new PATCH /api/admin/users/{email} (role + expiry updates), POST /extend (one-click +90d), DELETE (with last-admin + last-usable-admin guards including expired-admin exclusion). Auth: `expires_at` enforcement wired into `resolve_principal`, expired accounts get distinct 403 message. Existing users (pmvincenti, karen) keep `expires_at=null` (non-disruptive rollout — no retroactive lockout). Frontend snippet: `/app/memory/dashboard-user-management.snippet.html` — admin-only collapsible panel, add/disable/re-enable/change-role/extend/set-custom-expiry/never-expires/delete, all destructive actions require typed-email confirmation. Safety rails: cannot self-demote, self-expire-to-past (including exactly-now), self-delete, or delete the last USABLE admin. Reserved email `legacy@dashboard` blocked to prevent sentinel collision. **22/22 pytest** in `/app/backend/tests/test_user_management_iteration_29.py`.
8. ✅ **Audit log CSV + PDF export (Task backlog a)** — landed 2026-08-07. Backend: `GET /api/admin/audit-log/export.csv` and `.pdf`, both admin+operator gated (JWT or legacy X-Admin-Token). Filters: since/until (ISO 8601, hard-capped 30-day window), kind (trigger|status|rescued|rescue_reverted), limit (silent-clamped to 500). CSV has 28 stable snake_case columns (locked contract for archival scripts). PDF is landscape A4 via ReportLab (5.0.0), monospaced table, no branding chrome, includes header + generation metadata + operator email. Notes visible in both exports (admin-gated so no leak). Dashboard: `/app/memory/dashboard-audit-log.snippet.html` gained a toolbar with "Last 24h / 7d / 30d" range picker + Download CSV / Download PDF buttons (blob → object-URL → anchor pattern for cross-origin downloads with Bearer auth). Toolbar auto-hides for unauthenticated users. **24/24 pytest** in `/app/backend/tests/test_audit_log_export.py`.
9. ✅ **Dual casualty reports (B1 Operational / B2 Public)** — landed 2026-08-07. Backend: `GET /api/admin/casualty-report/operational.pdf` (full detail with names, exact GPS, notes, timeline) and `GET /api/admin/casualty-report/public.pdf` (aggregate counts only — no identifiable data even for rescued people, per legal lock). Both share `_gather_devices_in_report_window` + `_bucket_by_status` so B1 and B2 counts are always internally consistent. B2 has an assertion-based safety guard that refuses to render if the aggregate structure grows unexpected keys (e.g. `rescued_names`). Dashboard: audit-log snippet's export toolbar gained two new buttons ("B1 Operational" / "B2 Public") with client-side `X-Report-Kind` header sanity check (defense-in-depth against a misrouted proxy accidentally serving B1 when B2 was requested). Also fixed a dead sort-expression bug in the B1 per-device sort (was `-1 * ("" and 1)` — always evaluated to 0 — testing_agent caught it). **23/23 pytest** in `/app/backend/tests/test_casualty_reports_iteration_31.py`, including full privacy invariant (pypdf text extraction) that seeded a `TEST_CASUALTY_PII_NAME` and a `TEST_CASUALTY_RESCUED_NAME` and verified B2 exposes neither.
10. Next backlog: dashboard category filter, Crockford Base32 rescue codes + QR + `/r/?code=` landing, `server.py` refactor into route modules.

---

## Legal / privacy locks (do not change without review)

### B2 Public Casualty Report — identifiability policy (locked 2026-08-07)

**Rule:** The B2 Public report exposes **aggregate counts only**. No names, no initials, no short codes, no per-person location, no per-person status — not even for rescued people.

**Rationale:** This is a GDPR and next-of-kin issue, not a design preference. Publishing any personally-identifiable field (even for someone confirmed rescued and safe) could preempt formal next-of-kin notification protocols run by Malta Civil Protection. Once information is public it cannot be taken back.

**Change process:** Any modification that would make B2 contain identifiable information about any individual — even a first initial, even for confirmed-safe/rescued people — requires **explicit legal review before code lands**. Not an agent decision, not an ops decision, not a "we'll add a flag" decision. Legal review, in writing, before merge.

**Owner:** Paul.
6. Production migration (post Emergent Support response)
7. Existing backlog (audit export, dual reports, dashboard category filter, QR)

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
- **Karen (operator, 2026-08-05):** added as `operator` role, NOT admin. Deliberate — keeps a single admin (Paul) and provides a real test of the two-tier permission model. Karen can mark rescued/reverted, cannot manage users or reach admin-only endpoints. Promotion to admin is a one-line change if the role turns out to be too restrictive in practice.
- **Bootstrap admin (pmvincenti@gmail.com) NEVER expires** when auto-expiry lands. Rationale (locked 2026-08-05): if every account lapses simultaneously, nobody can sign in to renew them — self-locking failure mode. The one non-expiring account exists specifically to un-brick that scenario.
- **Paired safeguard (planned):** email-on-sign-in notification to the bootstrap admin's email, so a quiet compromise of the non-expiring account cannot happen unnoticed. See the "Admin sign-in notification" backlog item below.
- Additional operators added via `POST /api/admin/users` + Google Cloud Console test-user allowlist (consent screen is in Testing status).

### Legacy token cutover plan (2026-08-05 → pending)
- Legacy `X-Admin-Token` shared-secret path remains enabled (`LEGACY_TOKEN_ENABLED=true`) as safety net during rollout.
- Cutover gate: (1) Karen signs in successfully on separate device/browser, (2) Karen appears by name in audit log, (3) Karen confirmed blocked from admin-only endpoints, (4) admin-sign-in email safeguard shipped OR explicitly deferred by Paul.
- On cutover: flip `LEGACY_TOKEN_ENABLED=false` in backend `.env` → redeploy. Shared password permanently retired.

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

### Session idle timeout (landed 2026-08-06)
- 15-minute inactivity timeout on the dashboard, configurable via `CONFIG.IDLE_TIMEOUT_MS`.
- 60-second warning modal (`CONFIG.IDLE_WARNING_MS`) with countdown + "Stay signed in" button before expiry.
- Activity events tracked: mousemove, mousedown, keydown, touchstart, scroll, click. Debounced to reset the timer at most once per 5 seconds.
- **Deliberate UX call:** if the warning modal is showing, silent mouse movements do NOT dismiss it. Requires explicit "Stay signed in" click. Prevents accidental drift extending a session.
- Expiry routes through `qaAuth.signOut()` — same server-side audit path as manual logout.
- Per-tab timers (sessionStorage is per-tab; each tab tracks its own activity). Cross-tab sync not solved in v1 — uncommon on dispatcher workstations.
- Test hooks: `qaAuth.idle.expireNow()`, `qaAuth.idle.reset()`.

### Session idle timeout (P2, before/alongside subscription A+B) — LANDED 2026-08-06, see above
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

## User management + auto-expiring accounts (P2, scoped 2026-08-05)

Together with Task #9 (per-user auth) and the planned session idle timeout,
this completes the realistic threat model:
  - Fired employee → instant disable (Task #9's `session_version` bump)
  - Walked-away unlocked desk → idle timeout (planned P2)
  - Forgotten volunteer → auto-expiry (this scope)

### Locked reasoning
- **No master password on top of per-user auth.** A shared secret is strictly a regression: can't attribute actions, leaks (as ours did 2026-08-04), one departure forces rotation for everyone, creates a false "second factor" impression without adding real independence. Per-user Google + idle timeout + auto-expiry is the complete answer.
- **No hard delete of users.** Removal creates audit-trail holes. Disable is the operational action. GDPR erasure = separate `/admin/users/{email}/anonymize` endpoint that replaces email with `deleted-user-<uuid>@quakeangel.internal` but preserves the row.

### Request 1 — User management screen

**Backend endpoint additions (small):**
- Enhance `GET /api/admin/users` to include `last_login_at`, `last_activity_at`, `disabled_at`, `disabled_by`, `expires_at`, `expires_in_days`.
- New `POST /api/admin/users/{email}/role` — role change with `session_version` bump so it takes effect immediately. Refuses last-admin demotion.
- New middleware: update `users.last_activity_at` on every authenticated request. Powers "currently signed in" indicator without server-side session storage.
- New `POST /api/admin/users/{email}/anonymize` — GDPR erasure preserving audit rows.

**Dashboard surface (new `dashboard-users.snippet.html`):**
- Admin-only. Table: email · role · last-active · status · expires · actions.
- "Currently signed in" green dot = `last_activity_at < 15 min ago` (aligns with idle-timeout window). Honest given the JWT model.
- Actions: Change role, Disable, Re-enable, Renew expiry, Anonymize (GDPR).
- Add-operator modal: email + display_name + role + initial expiry (default 90 days).
- Self-row banner: "You cannot disable your own account or remove your own admin role."

### Request 2 — Auto-expiring accounts

**Schema additions:**
- `users.expires_at: datetime | null` (null = never expires; bootstrap admin defaults to null).
- `users.expiry_warned_at: datetime | null` — dedup guard for warning emails.
- Default expiry on new user creation: **90 days**.

**Expiry sweeper:**
- Coroutine on the EMSC poller loop, but at 60-min cadence (not 60-sec).
- Also runs on startup so a long-down deploy catches up.
- Per sweep:
  1. Users where `expires_at < now AND NOT disabled AND NOT active_alert_defer` → disable + `session_version++` + audit row `kind: "user_expired"`.
  2. Users where `expires_at < now + 7d AND expiry_warned_at IS NULL` → send warning + set warned_at.

**Active-alert-window defer (decisions):**
- Trigger conditions: (`push_events.kind == "trigger"` in last 12h) OR (any device in `not_responding` OR `trapped`). Either is "active".
- Defer duration per sweep: 24h. Re-checked every sweep, only expires if conditions clear.
- Warnings ALSO deferred during active alert. Not the moment to prompt account maintenance.
- **7-day cap on defer.** After a week of continuous active-alert conditions, expire anyway with escalated warning to all admins.

**Warning delivery:**
- Email via Emergent Resend integration (inherits from subscription lapse Phase D landing).
- Dashboard banner in user-management screen: red badge < 7d, yellow < 30d.
- Cadence: T-14, T-7, T-1 days. No more.
- Copy inherits subscription-lapse rules: exact date + local time, no relative language, one-click renew CTA.

**Renewal:**
- One-click "Renew 90 days" from user table.
- Custom duration dropdown: 30/90/180/365/never. No free-form input — prevents "expires 2099" degeneracy.
- Bumps `expires_at`, clears `expiry_warned_at`, writes audit row.

### Priority
After EMSC Phase 1 and subscription lapse A+B. Ahead of QR feature and report exports — every added operator makes this matter more.

## Admin sign-in notification (paired with never-expires bootstrap admin)

**Requested 2026-08-05 by Paul.** The non-expiring bootstrap admin account is a legitimate un-brick safeguard but also the single most valuable target — if compromised, an attacker has permanent access. This feature closes the "quiet use" gap: every sign-in as an admin fires a notification email to the admin's own address, so any use they didn't perform themselves is visible within minutes.

**Design (locked):**
- Trigger point: successful `/api/auth/google` for a user with `role == "admin"`.
- Delivery: email via Emergent-managed Resend.
- Contents: signing-in user email, timestamp UTC + Malta local, IP address (from `request.client.host` / `X-Forwarded-For`), User-Agent string, source Google account sub prefix (last 4).
- Fire-and-forget: email delivery must never block the sign-in response. Failures logged, not surfaced to the user.
- Rate limit: **max one email per 15 minutes per admin** — prevents accidental email storm during a testing session or a legitimately-frequent-usage day. Additional sign-ins within the window still produce audit-log entries; email is a supplementary channel not the primary one.
- Recipients: initially just the signing-in admin's own email. When a second admin exists, discuss whether all admins get notified of each other's sign-ins (mutual watch) or only self-notification stays.

**Storage:**
- New collection `admin_login_notifications` — log of every notification attempt (fired/skipped-by-ratelimit/failed-to-send) for forensic use.

**Priority:** MUST land before flipping `LEGACY_TOKEN_ENABLED=false`, per the cutover gate above, UNLESS Paul explicitly defers.


---

## Known Issues (must-fix before the release they impact)

### KI-2026-08-06-01 — Seismic map circle dashing broken on Android
- **Severity:** Blocks Android launch (does NOT block iOS launch)
- **Symptom:** All three indicative-radius circles on the seismic map (`app/map.tsx` via `src/components/MapCanvas.native.tsx`) render as solid lines on Android. On iOS they render correctly: 600 km solid = real poll-radius boundary, 300 km / 200 km dashed = indicative-only.
- **Impact:** The solid-vs-dashed distinction is the whole honesty framing of the feature. Solid says "this line is real data", dashed says "this line is UX communication only, not a physical boundary". If Android renders all three solid, we're implicitly claiming the 200 km / 300 km circles are as authoritative as the 600 km one — the exact false-precision failure the design was built to avoid.
- **Root cause:** `react-native-maps` upstream limitation — `lineDashPattern` on `<Circle>` is honored on iOS (MapKit) and silently ignored on Android (Google Maps SDK). Tracked in the react-native-maps repo, unfixed as of 1.20.1.
- **Fix options (choose one before Android launch):**
  1. **SVG overlay approach** — render the indicative circles as `react-native-svg` `Circle`s in a transparent overlay above the map, with `strokeDasharray`. Requires converting metres → screen pixels using `getBoundingBox()` / camera state on region change.
  2. **Segmented Polyline approach** — build the dashed circle as a series of short `<Polyline>` segments in a ring. react-native-maps polylines do respect `lineDashPattern` on Android as of 1.18+, but performance suffers with hundreds of segments per circle.
  3. **Solid circle with visual differentiation** — keep solid on both platforms, differentiate by stroke width or opacity (600 km = full opacity, 2 px; smaller = 0.5 opacity, 1 px). Weakest option; erodes the honesty framing.
- **Preferred fix:** Option 1 (SVG overlay). Option 3 accepted only if Option 1 turns out to be slow on old Android devices.
- **Owner:** Assign at Android-launch planning.
- **Not for iOS-first release:** iOS renders correctly. This is filed here so it can't get lost.


---

## 2026-08-12 — Export hardening + dashboard map/UX batch (Paul's bf1354f verification list)

**Backend (needs "Publish" on Emergent to go live):**
- **2.1** All exports round coordinates to 5 decimal places (~1 m) — GDPR data-minimisation. Device accuracy is 5–19 m; 12-dp storage stays internal, never exported.
- **2.2** `?pseudonymise=true` on CSV / audit PDF / B1: operator emails become stable `operator-N` aliases. Real mapping kept server-side in `operator_pseudonyms` collection for accountability. Dashboard exposes it as a "Hide operator emails" checkbox.
- **2.3** Server-side credential guard on rescue notes (`_looks_like_credential`): rejects password/key/token-shaped notes with 422 + plain-language message BEFORE storage. Client-side live warning under the notes textarea mirrors it.
- **2.4** B1 + audit PDFs: large diagonal CONFIDENTIAL watermark on EVERY page (banner already on all pages), `CONFIDENTIAL-` filename prefix, and a confirmation dialog on the dashboard before downloading B1/audit exports. B2 exempt (aggregate-only, shareable).
- **3.1** B1 Name/code column now renders via Paragraph markup — no more literal `<br/><font>` text.
- **3.2/3.3** CSV: UTF-8 BOM (Excel em-dash fix), CRLF-only line endings, ALL rows padded to 29 columns (no ragged first row), new `at_simple` column (`YYYY-MM-DD HH:MM`, Excel-sortable), `delivered` as TRUE/FALSE, `display_name` backfilled from device records, metadata rows (window start/end, generated-at, generated-by, row_count) before the header.
- **P4** Response-over-time bar chart (hourly ≤48h window, else daily) on B1 AND B2 + plain-language progress lines. HARD LOCKS: (a) never a bare percentage — B1 states the base on the same line ("of app users who checked in, not of everyone affected"), B2 shows counts only, NO percentage ever; (b) short lines, one idea per line ("3 of the 5 people who told us they were trapped have now been found. / This only counts people using the app. / Others may be affected who we cannot see.").
- Logo (PNG only — reportlab can't rasterise SVG) now drawn on B1 (below banner) and B2 (top-right) page headers.
- Tests: `tests/test_export_hardening.py` (new, 16 tests) + `tests/test_audit_log_export.py` updated for the new CSV shape. 63 export/report tests pass.

**Dashboard (staged in /app/memory/dashboard_build/index.html, deployed via GitHub push):**
- **1.2** `dashboard-v3.js` folded inline into index.html (fetched from the live site — it was public). Its duplicate audit-log widget (double-polling #qg-audit-body) was dropped. The external file should be DELETED from the repo on next push.
- **1.3** `map.invalidateSize()` wired: on load, delayed retries, ResizeObserver on #map-wrap, visibilitychange.
- **1.4** `scrollWheelZoom: false`; Ctrl/Cmd+scroll zooms (with on-map hint overlay); "⌂ Recentre on Malta" control (top-right).
- **1.5** No more 4-second scroll jumps: sidebar render skips DOM writes when data unchanged (signature check), preserves page scroll + group open/closed state + focus when it does rebuild; audit widget only rewrites on content change; count pills updated in place.
- Map markers now accessible shape+colour+label divIcons matching qgSeverityChip (circle=IMMEDIATE+SOS tag, triangle=SERIOUS, square=MINOR, ✓-circle=rescued, diamond=not-responding) and **scale with zoom** (13px at world view → 26px at street view) (3.4).
- **1.1** caching: to be fixed at the repo during the next push (Cache-Control headers / server config — inspect `backend_dashboard` server when cloned).

**DB additions:** `operator_pseudonyms {identity, alias, created_at}`.

---

## 2026-08-13 — Fixes from Paul's verification of 88d4fb2 + backend Publish

**🔴 1. Narrative/table contradiction (worst finding — on the public report):**
- Root cause: narrative merged self-reported "safe" check-ins into "found", while the table counted only operator-confirmed rescues.
- Fix: `_progress_figures()` computes every narrative figure from the SAME source as the aggregate table. "Confirmed found by a rescue team" (== table People rescued) and "told us themselves that they are now safe" are now two separate, separately-worded lines — never merged. Singular/plural grammar handled (`_plural`). B1 overall percentage states its base inline ("counting app users who checked in only"); B2 still never shows a percentage.
- Process fix Paul asked for: `TestNarrativeTableConsistency` in tests/test_export_hardening.py compares the narrative numbers AGAINST the table figures on the generated PDFs (plus merged-wording and grammar regression tests).

**🔴 2. Signed-out privacy exposure:**
- GET /api/devices and GET /api/audit now require operator/admin (JWT or X-Admin-Token). New anonymous GET /api/public/summary returns aggregate counts only.
- Dashboard: signed-out visitors get count pills + "Sign in to see live triage detail" — no markers, no feed (client-gated too, covering the window between the Render deploy and the backend Publish).
- Confirmed for Paul: /api/trigger-alert rejects unauthenticated POSTs server-side (401) — the browser message is not the only gate.

**🟠 3. Preview mode relocated:**
- The #qa-preview-panel node is MOVED at runtime into a collapsed "🧪 Admin testing tools" <details> at the bottom of the page (below the footer). Triage + map render first, always. Wrapper visibility mirrors the panel's own admin gating via MutationObserver.
- Enter key inside preview-panel inputs is neutralised (the 1951 km incident).

**🟡 4. Smaller:**
- Watermark alpha 0.08→0.05 + opaque white backing strip behind the chart legend (was obscuring "Checked in safe"; also helps B&W print).
- B1/B2 narrative blocks wrapped in KeepTogether — caveat lines can't be stranded on the next page.
- accuracy_m rounded to 1 dp on exports.
- display_name plumbing CONFIRMED end-to-end (app sends it via checkin.ts; backfill test passes with a seeded name). Live rows are empty simply because no user has entered a name yet.

Tests: 141 passing across export/report/gating/CORS/display-name suites. test_cors_iteration_24 + test_display_name_iteration_25 updated to authenticate against the newly gated endpoints.

**DEPLOY STATE: backend changes awaiting Publish; dashboard changes staged in /app/memory/dashboard_build/index.html — LAST PUSH FAILED because the PAT expired. Ask Paul for a fresh PAT and push (single commit, message drafted).**

---

## 2026-08-13 (batch 2) — Paul's full findings list

**Backend (DONE, tests: 144 passing — NEEDS PUBLISH):**
- 1a: B1, B2 and audit PDFs all generate PORTRAIT A4 now (595×842). Tables re-fit: audit cols 186mm, B1 per-device cols 186mm @7pt, charts 182/170mm. Verified single-page renders.
- 1b: CONFIDENTIAL band enlarged: 30pt band, 13pt bold "CONFIDENTIAL — personal data" + 7.5pt second line.
- 3a: B2 issuer line "Issued by the Quake Angel emergency response system, in cooperation with {authority}." — system-level, never the operator.
- 3b: B2 heading+chart+narrative are ONE KeepTogether block; fits one page.
- 5b: B1 `?detail=summary` omits the per-device table ("Per-device detail omitted…" note, `-summary` filename suffix, CONFIDENTIAL treatment kept).
- Polish: percentage/"Overall:" line REMOVED from B1 narrative (restated the split lines); watermark now fully behind content — opaque white bases on all tables + chart drawing; logo drawn on PDFs now carries an "In partnership with" caption.
- Tests updated: portrait assertion, summary variant, issuer line, no-percentage-statistic; iteration_31 name test made wrap-tolerant.

**Dashboard (STAGED in /app/memory/dashboard_build/index.html — PUSH PENDING, PAT expired again):**
- 5a: export bar rebuilt as plain-language cards grouped by confidentiality: 🔒 "CONFIDENTIAL — for your team only" (Today's report — for your team (B1) / Full history — printable (audit PDF) / Full history — spreadsheet (audit CSV)) FIRST, then ✅ "Safe to share" (Today's report — safe to share (B2)). Icons+wording, never colour alone. Shared controls: time range, Hide operator emails, Team report size selector (full/summary → &detail=summary).
- P2: header org logo now a labelled "In partnership with" badge; QA mark is permanent product branding (Paul's decision — only a code change may touch it).
- P4: trigger button hidden signed-out (server 401 already enforced).
- Commit message drafted (see git history intent); ask for fresh PAT and push single commit.

**NEXT AFTER THIS BATCH: full dashboard restructure — spec in `dashboard-restructure-spec.md` in Paul's project folder (tabs Live/Reports/Admin/Testing, persistent status bar, search, pop-out map). Ask Paul to provide the spec file contents when starting.**

---

## 2026-08-13 (batch 3) — Time windows, short codes, triage ergonomics

Backend (server.py, tests green): `_covers_line`/`_duration_words`/`_fmt_dt_plain` (plain-words absolute coverage on B1/B2/audit PDFs + CSV `covers` metadata row); `_last_alert_start` (latest push_events row — NOTE: no end-of-incident marker exists; "active" = caller-defined, dashboard uses 72h); `_window_gap_warning` (window starts after alert → red warning on all 3 PDFs + CSV `warning` row); `/api/public/summary` exposes `last_alert_at`; `/api/devices` returns collision-safe `short_code` (`_short_codes_for`: last-5 alnum uppercase, colliders extended leftward +2 chars until unique) + `trapped_since` (`_trapped_since_map`: start of current trapped spell); B1 per-device Status cell carries "trapped for …" in words; B1 narrative appends `_low_battery_lines` (plain words, states total, no bare %).
Dashboard (index.html): 8 window options shortest-first incl. "Since the alert"; default = alert if last_alert_at <72h else 24h (never 7d); on-screen gap warning `#qg-window-warning`; feed + all exports use `qgWindowSinceISO()`; cards lead with big short code + small full id, "Trapped for X"/"Updated X ago" in words ticking every minute (minute-tick in sidebar signature); low-battery flag ⚠ + words <20%; triage search box (code/name/id); within-group sort longest-waiting-first with battery tiebreak + visible label; Rescued group default collapsed (user open/close preserved in-session); live counts 2-col grid + session-only Hide/Show toggle (NO persistence — deliberate); pins carry short code tags; `window.__qgDebugRender` test hook.
DEPLOY ORDER (Paul's rule): backend Publish FIRST, then GitHub push. Dashboard push PENDING — need fresh PAT after Paul publishes.
Outstanding backlog (Paul's list): #128 logo/branding duplicates, #130 B1 default summary, #131 watermark, #133 export card text + dialog/filename jargon, #134 signed-out banner, #135 idle timeout, #136 Cmd/trackpad zoom. Then dashboard-restructure-spec.md.

---

## 2026-08-13 (batch 4) — Dashboard polish: issues #128–#136

**Backend (server.py — tests green, NEEDS PUBLISH):**
- #131: `CONFIDENTIAL` watermark moved from centred diagonal (crossed Rescued rows / chart legend) to rotated margin bands — "CONFIDENTIAL" repeats up BOTH 12mm side margins (11pt bold, 30% alpha, stops 80pt below top to clear banner+logos). Content-free margins ⇒ can NEVER overlap data. Banner+footer unchanged (audit test asserts ≥2 "CONFIDENTIAL" per page — still holds).
- #128: `_QA_LOGO_B64` embedded (80×80 PNG, same mark as dashboard header); `_draw_header_logos` draws the Quake Angel mark top-right on EVERY B1/B2 page permanently; a configured partner logo renders to its LEFT under "In partnership with" caption. Replaced `_draw_logo`; `_make_confidential_onpage`/`_make_public_onpage` always draw the QA mark.
- #130: `detail` Query default flipped `full`→`summary` on operational.pdf — per-device table is opt-in. Tests updated to pass `detail=full` where per-device content is asserted; new `test_b1_defaults_to_summary`.
- #133: filenames de-jargoned — B1 → `CONFIDENTIAL-quakeangel-team-report{-summary}-TS.pdf`, B2 → `quakeangel-public-report-TS.pdf`. `X-Report-Kind` headers unchanged (b1-operational/b2-public — internal contract w/ dashboard). PDF body wording (locked confidentiality text, "END OF B1 OPERATIONAL REPORT") deliberately NOT touched. New `test_b1_filename_has_no_jargon`.

**Dashboard (index.html — STAGED, needs PAT push):**
- #128: partner badge is now ONE bordered chip (`.qg-partner-badge`) with the "In partnership with" caption inside it — label can't float. NEW dedup guard: uploaded org logo is pixel-compared (16×16 downsample composited on #0f0f0f, RGB mean-abs-diff <12; measured same-artwork≈1.4 vs different≈166) against the built-in QA mark — a duplicate upload (current prod state: 512×512 transparent re-export of the QA logo) is hidden from the header but kept in the admin preview so it can be removed.
- #130: "Team report size" select defaults to "Summary only"; `&detail=` now ALWAYS sent explicitly.
- #133: removed `(B1)`/`(B2)` code spans from export cards; confirm dialog / progress / success messages / settings-panel copy all say "team report"/"public report"/"printed reports".
- #134: signed-out state = ONE active brand-red banner at top ("You are signed out — sign in to use the dashboard" + Google button inside). Sidebar + activity-feed signed-out messages reduced to quiet one-liners pointing at the banner.
- #135: idle timeout 15→60 min (activity-reset + 60s warning modal unchanged).
- #136: OS detection via `userAgentData.platform` fallback chain; hint mentions trackpad pinch; ctrl/meta wheel (= trackpad pinch) now ACCUMULATES deltaY and steps zoom once per |40| (mouse notch ≈100 steps immediately; pinch no longer rockets across zoom levels). Verified +1 step for 5×(-10) synthetic pinch events.

Deferred (Paul's call): Android dashed-circle `lineDashPattern` workaround in MapCanvas.native.tsx.
NOTE: full pytest run shows 116 pre-existing failures ALL in legacy push/debug suites (schema drift, e.g. `/api/status` now requires `status` field — tests predate it). Unrelated to this batch; candidates for cleanup during the server.py refactor task.

---

## 2026-08-17 (batch 4 / Neo) — A1–A3 fixes, B1 investigation, B2 verification, B3 history

**A1 (server.py — PDF body jargon):** all literal B1/B2 removed from VISIBLE PDF text: `CONFIDENTIALITY_TEXT`, `_pdf_confidentiality_onpage` banner + footer, team title ("Team report — operational casualty report"), closing note ("END OF TEAM REPORT … use the 'safe to share' public report"), PDF metadata title. `X-Report-Kind: b1-operational/b2-public` response headers deliberately unchanged (dashboard contract, not visible text). Tests: `test_batch4_polish.py::TestA1JargonFreePdfs` (team full/summary, public, audit).

**A2 (server.py — PDF logo dedup):** `_logo_is_brand_duplicate()` mirrors dashboard's looksLikeBrandMark (16×16 on #0f0f0f, RGB diff <12); `_get_logo_image_reader()` returns None for duplicates. Audit PDF now uses `_make_confidential_onpage(logo)` too, so all three PDFs carry the QA mark + labelled partner rules. Caption only drawn when a real partner logo exists. Tests: `TestA2SingleQuakeAngelMark` (dup hidden / none = no caption / real = labelled, on all 3 PDFs).

**A3 (index.html — export cards):** real button affordance: 1.5px border, shadow, hover lift + colour, :active press, cursor pointer, circled ↓ download icon (::after, U+2193), head 12.5→14.5px, desc 11→12.5px. "A printable page." → "A printable doc." (2×).

**B1 (investigation only, PROD numbers 2026-08-17 ~09:10Z):** poller RUNNING (EMSC+USGS last success 09:08Z; task up since the 08-13 publish). Ingest ≈1,200 events/day (4,530 since 08-13 restart; listing endpoint caps at 500). Preview mode ACTIVE, allowlist = Paul's current device (re-registered 08:56Z today). Radius: base MT poll 600 km; preview override 3,000 km (set by Paul 08-15, expires 08-22 14:57Z). Last 15h: 500 decisions = 488 beyond-radius + 12 below-threshold, 0 sent; closest event 4,829 km. Last DELIVERED previews: 2026-08-15 10:26Z & 10:47Z (EMSC 20260815_0000344, M3.4/3.6). Verdict: silence is genuine seismic quiet, filters healthy. Flag: Paul should confirm he RECEIVED the two 08-15 previews.

**B2 (no code change + version bump):** mobility-skip (#51) verified correct in current code via web preview: RED submits directly (mobility defaulted to trapped/pinned), GREEN submits directly, YELLOW asks. Paul's build 1022 predates 2026-07-31 (his repro used the OLD label "I can walk, I'm not badly hurt" + old post-red flow). app.json version bumped 1.0.22 → **1.0.23**; ALL app-side fixes land in the first build generated after next deploy. **Build-audit finding: expo version had been stuck at 1.0.22 since 07-22, so builds were indistinguishable.** In-code-but-not-in-build-1022: #51 labels+skip (07-31), Apple Watch note (07-31), check-in reminders rework (08-04), push registration changes (08-05), quake detail screen + notification settings screen (08-06), seismic map #107 (08-06), EntitlementBanner (08-06).

**B3 (server.py + index.html):** ANSWER: reconfirmations were ALREADY logged — `status_events` is an append-only ledger; every POST /api/status inserts a row (same-status re-reports included) with per-event battery + location. Gap closed: per-person surface. NEW `GET /api/admin/device-history/{device_id}` (admin/operator): alerts sent (push_events) + every status event, `reconfirmation` flag (same status+severity+mobility as previous), last_known block with `is_stale` (>30 min silent). Dashboard: "📜 Full history" button per triage card (event delegation on #userlist) → modal `openHistoryModal()` with stale warning, reconfirmed badges, per-event battery/map links. Exports: audit CSV/PDF already contain every event (pre-existing). GDPR flag for #75: legal-record use argues for LONGER retention of status_events — flagged, not decided. Tests: `TestB3PerPersonHistory` (5, incl. auth gate; cleans up after itself).

**B4:** design written to `/app/memory/zones-design.md` — DESIGN ONLY, build with #116. Open auth question for Paul (field logins vs coordinator-only) asked in report.
**B5:** plan written to `/app/memory/load-test-plan.md` — PLAN ONLY, awaiting approval. Safety: synthetic devices never enter token collections (structurally unpushable), all rows flagged synthetic+run_id (ties into #146).

Tests: 115 passing (105 existing export/report/user suites + 10 new batch-4). Legacy push/debug suites still stale (pre-existing, see 08-13 note).

---

## 2026-06 (batch 5 kickoff) — B4 auth decision, B5 stage 1 run, 1.0.23 build

**B4 zones — auth question ANSWERED by Paul: coordinator-only.** No field logins,
no new role, zero auth work in B4; operator sees all zones and radios teams.
Field-team scoped logins belong to the separate **responder field app (#98)**.
`zones-design.md` updated (open question section replaced with the decision).
Still DESIGN ONLY — builds with the #116 dashboard restructure.

**B5 load test — stage 1 EXECUTED (approved by Paul).** New harness:
- `/app/backend/scripts/load_test_seed.py` — `seed --count N --hours H`,
  `count`, `clear [--dry-run]`. Direct DB inserts to `device_status` /
  `status_events` only; never touches `push_devices` or any send path, so
  synthetic devices are structurally unpushable. Every row carries
  `synthetic: true`, `load_test_run_id`, device_id prefix `qg-loadtest-`
  (same flag proposed for #146).
- `/app/backend/scripts/load_test_measure.py` — read-only median-of-3 timing of
  /api/devices, /api/public/summary, both casualty PDFs, audit CSV + PDF.
Stage 1 = 100 devices / 240 status events on PREVIEW. **Nothing degraded**
(slowest surface: audit PDF 0.21 s; /api/devices 0.01 s / 52.8 KB). Key scaling
read: /api/devices ≈ 0.46 KB per device ⇒ 30k ≈ 14 MB payload, which breaches
the 5 MB worry line long before latency does — pagination/field-trimming will be
the first required change. Synthetic rows cleared after measuring (dry-run
counted 100/240, delete removed exactly 100/240, 14 real rows untouched).
Results table: `/app/memory/load-test-results.md`. Dashboard render / Leaflet FPS
/ Mongo CPU deferred to stage 3 (only meaningful at 10k+, and needs the
dashboard pointed at preview).

**App build:** no new app-side code this session. app.json remains
**version 1.0.23**; iOS build number / Android versionCode are assigned by the
Emergent build pipeline at publish time (auto-increment). The 1.0.23 build is the
first to contain the #51 mobility-skip fix plus everything listed as
"in-code-but-not-in-build-1022" above.

---

## 2026-08-17 (batch 5) — Part A verified live, Part B built, C1 designed

**PART A — all four already live in PRODUCTION; Paul's evidence was pre-publish.**
Verified by fetching a fresh PDF from the production backend and the live Render
dashboard, not from preview:
- A1: prod `operational.pdf?detail=summary` → 0 occurrences of "B1", 0 of "B2".
  Same for public.pdf and the audit PDF (checked with pdfplumber text extract).
- A2: prod team report → exactly 1 image per page, no "In partnership with"
  caption when no org logo is set.
- A3: prod index.html carries the bordered/hover/pointer export cards,
  14.5px/12.5px text and "printable doc" ×2.
- A4: prod index.html contains `openHistoryModal` ×3 and the "📜 Full history"
  button — it lives on each person's row in the trapped/status list.
Paul's stale file was timestamped 20260817T072324Z, before the publish landed.

**PART B — app version 1.0.24 (build number assigned by the pipeline).**
- B1 `app/index.tsx` no longer schedules reminders on the TEST trigger;
  `app/alert.tsx` arms them in a `useEffect` gated on `shouldPlaySiren`, so only
  a genuine critical alert (siren=1) does. `_layout.tsx` also arms them when a
  `kind=critical_alert` push arrives while the app is running.
  Kill switch: `POST /api/admin/reminders/cancel` (server.py) →
  `apns.send_silent_cancel_reminders()` sends `{"aps":{"content-available":1},
  "kind":"cancel_reminders"}` with `apns-push-type: background`, priority 5,
  10-minute expiration. App handles it in a registered background task
  (`CANCEL_REMINDERS_TASK` in `_layout.tsx`) + the foreground received listener.
  Dashboard button "Stop reminder notifications" added to
  memory/dashboard_build/index.html — NEEDS A PAT PUSH TO GO LIVE.
  Answer-cancels-reminders: demonstrated with a Node harness stubbing
  expo-notifications (8 pending at 60/150/…/690s → 0 pending + 2 lock-screen
  ones dismissed, synchronously in `submitCheckIn` before GPS/network).
- B2 `alert.tsx`: metrics strip moved OUT of the shrinkable `center` block;
  `center` is `flex:1 + minHeight:0 + overflow:hidden`, strip and buttons are
  `flexShrink:0`; `compact` (window height < 760) shrinks graphic/headline only.
  Verified no overlap at 320x568 / 375x667 / 390x844 / 430x932. "Dismiss alert"
  removed (#14) — only a "Back to home" after a confirmed trapped report.
- B3 `quake/[unid].tsx` computes distance from the user's own position when
  permission is ALREADY granted (never prompts here), else falls back to Malta
  and says so; the explainer sentence is either fully present or fully absent.
  ALSO FIXED THE REAL CAUSE: `_layout.tsx`'s tap handler short-circuited on
  `action_url` and navigated to /quake/<unid> with NO params — every field
  showed "—" when opened from a notification. It now forwards all payload
  fields. Preview notification body already carries distance ("210km SE of
  Malta").
- B4 three options; "significant" retired and auto-migrated to "noticeable"
  (same MMI III floor) on first open, so nobody lands on Off.
- B5 "woken" → "alerted"; onboarding's "must wake your phone" reworded. Repo
  searched — those were the only two; a pytest guard now fails on any new
  waking claim in app/ or src/.
- B6 post-update card carries Paul's why-it-matters copy + a permanent
  "I don't use an Apple Watch" opt-out (`WATCH_NOT_APPLICABLE_KEY`). LIMITATION:
  iOS exposes no Watch-pairing signal to JS, so the gate is self-declared.
- B7 re-verified on current code: red skips mobility, green skips, yellow asks.
  Never was a bug in 1.0.23 — Paul's repro was against the 3 Aug build.
- B8 "places I care about": `user_places` collection, 4 endpoints under
  /api/devices/{id}/places (cap 5, whole-feature switch), notices dispatched by
  `emsc/preview.py::dispatch_place_notices` using the SAME intensity logic
  (`mmi_from_faenza_michelini_2010` + `preset_would_fire`) plus a
  poll_radius_km cap so "everything nearby" can't become a firehose. App screen
  `app/settings/places.tsx` uses the OS geocoder (`Location.geocodeAsync`) — no
  API key, no map. SAFETY: the critical path never reads `user_places`;
  asserted structurally in tests. PRIVACY: saved places are location data about
  third parties — flagged for the GDPR work (#75).
- B9 `TREMOR_INFO` category with "See location on map" / "Close" on
  informational notices only; `_build_critical_payload` asserted to carry NO
  category. Tapping the body behaves like "See location on map"; "Close" does
  nothing at all.
- Housekeeping in app.json: `ITSAppUsesNonExemptEncryption: false` (stops the
  export-compliance question on every build), explicit
  `CFBundleDisplayName: "Quake Angel"`, and `UIBackgroundModes:
  ["remote-notification"]` (required for the silent kill switch).

**C1** designed in `/app/memory/recheckin-design.md` — nothing built. Flags one
uncomfortable truth: the widening ladder is reliable for ~24h and best-effort
after that, because iOS local-notification top-ups need the app to run and
silent pushes are throttled.

**Docs:** `/app/memory/app-build-history.md` answers Paul's outstanding request
for app changes since 3 Aug with the build each landed in.

**Tests:** `tests/test_batch5.py` (20) + testing agent's independent
`test_batch5_independent_iteration_32.py` (33) — all green. Pre-existing legacy
failures unchanged (test_critical_alerts, test_admin_gate: predate the #9 JWT
cutover and assert version 1.0.8).

**Batch 5 follow-ups (same day):**
- B6 refined per Paul: the opt-out now means "I don't OWN an Apple Watch", never
  "stop telling me". `dismissWatchForever` (app/index.tsx) shows a confirm
  dialog whose default action is "I own one — keep reminding me"; only "I don't
  own one" sets `WATCH_NOT_APPLICABLE_KEY`. Watch owners keep getting the notice
  after every version change, which is the point — iOS resets the toggle every
  update.
- **#146 old test entries in the trapped list — FIXED.** Root cause: the
  existing `/admin/purge-test-devices` only ever touched `push_devices`, so the
  trapped list (which reads `device_status`) was untouched by it. Now:
  `_is_test_device()` in server.py returns a server-computed `is_test` on every
  /api/devices row (+ `test_count`). Detection is two-pronged: recognised
  harness markers (test/e2e/loadtest/diag/snippet/playwright/demo/-mob-) AND an
  explicit `synthetic` flag — needed because our own test check-ins come from a
  real phone. Ids matching the app's own format `qg-<epoch>-<rand>` are
  ALWAYS treated as real unless explicitly flagged, so a random suffix can never
  hide a genuine casualty. New endpoints: POST
  /api/admin/devices/{id}/mark-test (operator, reversible, audited both ways),
  GET /api/admin/test-entries (preview across all 3 collections), POST
  /api/admin/purge-test-entries (ADMIN ONLY, audited with the full id list —
  operators may hide, only admins may destroy). Dashboard hides test rows from
  the list, the map and the count pills by default with a "Show test entries
  (N)" toggle + per-row "🧪 Mark as test" / "↩︎ Not a test". Verified end-to-end
  in a browser with a stubbed API: pill went 2→1 when hidden, TEST tag and both
  buttons render, POST fires with the right body, banner confirms.
- Dashboard pushed to GitHub twice (kill switch, then #146); clone wiped each
  time. BOTH features need the batch-5 backend published to work.

**C1 design finalised 2026-08-17 (still NOT built).** `recheckin-design.md`
rewritten after Paul challenged two things:
1. **Server-driven, not device-scheduled.** He was right: the throttling/Low
   Power Mode limits apply to SILENT pushes, which only my local-schedule top-up
   needed. Visible alert pushes at priority 10 have none of them. Now: backend
   due-check sweeper is primary; ONE local notification armed a step ahead as an
   offline safety net; answers queue on device and flush on reconnect.
2. **iOS shows ZERO notification actions until expanded** (verified, not
   assumed — cannot be overridden). So ordering the actions can't fix it. The
   primary answer path is now: tap the notification BODY (whole banner is the
   target) → full-screen re-check with four ~64pt buttons = two large taps, no
   long-press. Actions kept as a secondary path. Android does show 3 inline, so
   there Paul's rule applies: WORSE, MUCH WORSE, SAME inline; BETTER in-app.
   Live Activity (ActivityKit + App Intents) is the real fix for true
   lock-screen buttons — native work, needs a build, deliberately deferred.
Other decisions: WORSE escalates one band, **MUCH WORSE jumps straight to red**
(no forced stepwise); no auto-downgrade on BETTER; stop asking when the phone is
DARK, keep asking indefinitely while it's ALIVE, operator can restart (state,
not clock); non-responders included at the widest interval; re-checks are
critical-level with a SHORT sound, with the Apple entitlement justification now
written out verbatim in the doc plus an enforcement rule (refuse to send to any
device not currently `trapped`, with a test).
**Tap time is authoritative:** `answered_at` (device tap) is what the history
modal, audit CSV and audit PDF must show and sort by; `received_at` kept
alongside, never substituted; gaps > ~2 min rendered explicitly ("answered
14:20, reached us 15:05 — queued offline"); implausible device clocks flagged,
never silently corrected.

---

## 2026-08-17 (evening) — #169 SIREN SILENT ON TEST ALERT — FIXED (app 1.0.25)

**Severity: highest to date.** Paul pressed Trigger Test Alert on 1.0.23, got the
red EARTHQUAKE DETECTED screen in total silence. He had already ruled out assets
(both siren files "loaded" + play from Diagnostics), phone audio, the
critical-alert entitlement (a reminder arrived loud on a locked phone) and the
Apple Watch stealing it.

**ROOT CAUSE — a one-line entry-param bug, not an audio bug.**
`app/index.tsx` navigated with `router.push("/alert")`. `app/alert.tsx` gates
playback on `params.siren === "1"`. That gate was introduced 2026-08-06 in commit
d3e8d81 to fix BUG-2026-08-06-preview-tap-siren (informational preview taps were
detonating the siren) — the test trigger never passed the param and was silenced
as collateral. Broken 2026-08-06 → 2026-08-17. Paul's earlier 3 Aug build DID
siren on test, which predates the gate — that's why it looked like a regression.

**Answers to Paul's three questions:**
1. Same playback code, DIFFERENT entry params — and the params were the bug. Now
   identical: test → `/alert?siren=1&test=1`, real → `/alert?siren=1&…`.
2. No, the real path was NOT exercised by any test. Every test covered playback
   internals (which were fine); nothing covered the entry params. Fixed:
   `TestIssue169SirenPlaysOnTest` pins the params for BOTH paths.
3. Not foreground/background related, and not tied to notification arrival —
   tied to the alert screen's params. Any entry without `siren=1` was silent
   regardless of app state.

**Fix:** `router.push("/alert?siren=1&test=1")`; new `isTestRun` flag in
alert.tsx so the siren and the check-in reminders are two INDEPENDENT decisions
(conflating them is what let this hide — B1's reminder gate was keyed on
`shouldPlaySiren`). Added a `console.log("[QuakeAngel] SIREN play() requested")`
trace so "screen appeared but silent" can be distinguished from "sound path never
ran" from a device log.

**Proven, not asserted:** browser console capture shows `SIREN play() requested`
with `?siren=1`, absent without the param, and **fires when the actual Home
button is clicked**. 6 new tests incl. #13 (playsInSilentMode) and #31/#50
(kill-switch) regression guards, plus a guard that the critical push payload
still names the bundled `siren.caf` (the locked-phone siren is the PUSH sound —
if the user never opens the app, that file is the only siren they get).

**App version 1.0.25.** C1 design work paused as instructed.

**#169 — answer to "would a REAL alert have sounded?" + two further defects
found while tracing it (app 1.0.25, backend change):**

iOS real alerts WOULD have sounded in every app state 6–17 Aug. The siren is
the push sound itself (`siren.caf`, `critical: 1`, `volume: 1.0`) and the
foreground handler sets `shouldPlaySound: true`, so only the in-app TEST button
(which sends no push at all — it is purely local) was silent. The looping in-app
siren also worked on a real alert, because the tap handler sets `siren=1`.

TWO REAL DEFECTS FOUND ANYWAY, both fixed now:
1. **Android real alerts were silently downgraded.** The SuprSend payload in
   /api/trigger-alert carried NO `kind` field. The app's tap handler routes by
   `kind` and treats a missing kind as INFORMATIONAL (deliberate fail-safe from
   BUG-2026-08-06-preview-tap-siren) — so an Android user tapping a REAL
   earthquake alert landed on the informational event screen, never on the
   check-in screen, and the in-app siren never armed. Fixed: payload now carries
   `kind: "critical_alert"` + magnitude/distance/intensity. STILL OUTSTANDING:
   the Android notification sound is the channel default, not the siren — a
   custom channel sound needs the audio file in `res/raw` via a config plugin
   (native work, needs a build).
2. **Foreground gap on iOS.** A real alert arriving with the app already open
   played the push sound once and showed a banner, but left the user wherever
   they were — no looping siren, no check-in screen — unless they tapped it.
   Fixed: the received-listener now routes straight to `/alert?siren=1` via the
   same handleTap path as a tap.
3 new tests pin all three (Android `kind`, foreground routing,
`shouldPlaySound: true`). 35 tests in test_batch5.py.

**Aftershock edge case (Paul, 2026-08-17) — FIXED before it shipped.** The
foreground auto-routing added minutes earlier called `router.push("/alert")`,
which mounts a SECOND alert screen: an in-progress answer (open triage sheet,
chosen severity, mobility answer) was discarded and the first screen's siren
kept looping underneath the new one. New `src/utils/alertBus.ts`: alert.tsx
registers mounted state, `_layout.tsx` PUBLISHES the new event to the open
screen instead of navigating. The screen shows an amber "Another alert just
arrived — M5.1. Your answer below still applies." notice and touches no answer
state; if the user had already answered it says the report already reached the
team and offers an explicit "Update" button (never an automatic reset).
**Also removed a re-arm I had just added:** the first cut restarted the siren
for the new event, and the browser demo showed it restarting while the user was
mid-triage seconds after they'd silenced it — the exact #31/#50 shape. Dropped:
audibility of the new event is already covered by the push sound itself
(siren.caf plays on arrival regardless), so the user's control over the in-app
siren stays absolute. Demonstrated in-browser via a __DEV__-only bus handle:
sheet survives, notice appears, no second route stacked, mobility question still
reachable, answer completes, no "re-armed" log. 5 new tests (40 total in
test_batch5.py) incl. one that fails if the aftershock subscriber ever touches
setStatus/setTriageOpen/setChosenSeverity/submitCheckIn.

---

## 2026-08-18 (batch 6) — A0 + A1 fixes, event-detail navigation, C3 identity

**A0 — blank screen on tremor tap: ALREADY FIXED in 1.0.25.** Cause was
`_layout.tsx` handleTap short-circuiting on `action_url` and navigating to
/quake/<unid> with NO params; it now forwards every payload field. Paul was on
1.0.24/125 which predates it.
**A0 measured timing (real preview data, 400 events):** quake time → our ingest
median **10.5 min**, p10 5.0, p90 42.7, max 59.4. That is EMSC publication lag
plus our 60 s poll; our own send adds seconds. Paul's ~2h53m gap is NOT pipeline
latency — it is the radius/tier widening at ~11:00 making already-ingested
events newly eligible, and the backlog going out. Which exposed a real defect:
**no freshness gate existed.** Added `max_event_age_minutes` (default 90) to
both `dispatch_preview_if_needed` and `dispatch_place_notices`; older events log
`event_too_old` and send nothing.
**A0 revisions (confirmed, real data):** 33 of the last 400 ingested events were
revisions of an event already stored; e.g. 20260805_0000212 revisions [0,1] with
magnitudes [3.7, 4.4]. Every revision dispatched a fresh notice, so Paul's
M3.3/M3.7 pair was ONE quake. Now: magnitude moved < 0.3 → suppressed
(`revision_no_material_change`); >= 0.3 → sent as "PREVIEW · Updated seismic
reading" / "Updated: now measured at M3.7 (first reported M3.3). Same
earthquake, not a new one." Delivered rows now record magnitude so the
comparison has a baseline.

**A1 — chart counted events, table counted people (#124 family).**
`_bucket_timeline` in server.py now counts each device ONCE per bucket at its
MOST SEVERE status (trapped > rescued > safe), so chart, table and narrative all
measure people. Y-axis labelled "People" in `_timeline_chart`. Verified on all
three PDFs with a seeded device that toggles 5 times — chart peak 1, not 3.
6 tests incl. the structural invariant (bucket total can never exceed distinct
device count) — that is the check Paul asked for after #124, expressed as an
assertion rather than a review step.

**B2/#173 — event detail returned to Home.** `quake/[unid].tsx` used
`router.replace("/")`, tearing down the stack. Now `router.back()` when a stack
exists (map keeps its pan/zoom/time window), `replace("/")` only as the
cold-start fallback. Back control is a chevron labelled with its origin
("Map"), an X only on cold start. `map.tsx` tags its pushes with `from: "map"`.

**B5 — "See location on map"** on the detail screen → pushes /map with
`focus_lat/focus_lon/focus_unid`; MapCanvas gained `focus` (initial region) and
`highlightExternalId` (larger dot + white ring). Pushed, not replaced, so
notification → detail → map → detail always unwinds.

**C3 — identity everywhere.** Verified: team PDF per-device table shows the name;
public PDF has NO names (aggregate only); audit CSV has display_name +
short_code columns; history modal renders name + code; dashboard search already
matched names. FIXED: map pin labels and popups showed only the code — both now
carry the name (pin labels truncate at 12 chars). Pushed as 7809dc8.

**C2 — answered with evidence.** `status_events` is append-only and
/admin/device-history returns EVERY row: a repeated identical status is its own
entry flagged `reconfirmation: true` (demonstrated: 3 identical trapped/red
events → false, true, true), each with per-event battery and lat/lon, plus
`last_known` carrying `silent_seconds` and `is_stale`. So "reconfirmed hourly
for three days" and "silent for three days" are already distinguishable.

**App version 1.0.26.** Diagnostics marker updated to "#169 siren, aftershock
guard, map nav (#173)". Testing agent verified 16/16 backend + all frontend
flows (report iteration_33.json); its one non-blocking finding (places path
missing the freshness gate) is fixed above.

---

## 2026-06-18 — GDPR map-link fix, UTC timestamps, server.py split, C1 phase 1

### 🔴 GDPR: coordinates no longer leave to a third party
Audit rows, the per-person history and the server-rendered audit page all built
`https://www.google.com/maps/place/<lat>,<lon>` links. Every operator click
disclosed a casualty's exact position to Google inside a URL. Replaced with an
internal recentre of our own Leaflet map (`window.qgShowOnMap`, delegated
`[data-qg-map-lat]` handler); the backend HTML page now prints coordinates as
text rounded to 5 dp. Exports and PDFs swept — clean. Regression guard:
`backend/tests/test_no_external_map_links.py`. **If an external routing link is
ever added it must be a separate, explicitly-labelled action AND written to the
audit trail, because it is a disclosure (Paul, 2026-06-18).**

### 🔴 Two-hour timestamp error (found while measuring A0 delivery latency)
Motor returns naive datetimes; a naive `isoformat()` has no offset and
JavaScript parses an offset-less date-time as LOCAL time. An 08:07 UTC quake
rendered as "08:07" on a Malta (UTC+2) phone — two hours early, on the exact
timestamp a user compares a notification's arrival against. It made an 11-minute
delivery look like a three-hour one. Fixed at source (`deps.iso_utc`,
`emsc/preview.iso_utc`, seismic-map serializer) and defensively on the phone
(`src/utils/time.ts parseUtc`).

Measured latency for the record (production, the two events Paul tapped):
origin 08:07:10.75Z → ingest 08:18:03.62Z → send 08:18:03.628Z (10 min 53 s,
of which our send path is 8 ms); second event 30 min 33 s, send path 8 ms. The
lag is upstream EMSC publication, not ours. Poll cadence ≤60 s.

`emsc_preview_notifications` now records `distance_km` + `observed_at` on
DELIVERED rows too — previously only skipped rows carried distance, so any
"closest event we've seen" query could only ever return the closest event we
did NOT notify about (that is where the bogus 4,829 km figure came from).

### 🔵 server.py split (behaviour-preserving)
6,057 lines → 2,307, with the route surface asserted byte-identical (71 routes,
same methods) before and after:
- `deps.py` — env, single Mongo client, admin secret, CORS allowlist,
  `short_code`, `iso_utc`, `is_test_device`, and the background workers
  (EMSC poller, testimonies sweeper, re-check sweeper). Imports nothing from
  server.py, so route modules can import it without a cycle.
- `push_relay.py` — Emergent/SuprSend relay client + `send_push`.
- `reports_export.py` — audit CSV/PDF, B1/B2 casualty reports, PDF chrome,
  chart, plain-language narrative.
- `routes_auth_users.py` — Google sign-in + user management.
- `routes_emsc_admin.py` — soak health/recent/config/continuity + preview mode.
- `routes_diagnostics.py` — maintenance + operator HTML diagnostic pages.
- `routes_recheck.py` — C1 answer endpoint, sweeper status, kill switch.
Test files that read server.py source were repointed; four tests using
`asyncio.get_event_loop()` were changed to `asyncio.run()` (they failed only in
combination with other modules, which reads as a regression and wasn't one).

### 🔴 C1 phase 1 — automatic re-check ladder (landed)
Per `memory/recheckin-design.md`. `backend/recheckin.py`:
- Ladder 15 / 30 / 60 / 180 min by time-since-trapped, tunable; ×2 below 20%
  battery, ×3 below 10%, and the prompt SAYS so.
- `RecheckSweeper` (60 s cadence, own asyncio task) sends due prompts.
- **Invariant: never prompts a device whose CURRENT status is not `trapped`** —
  this is what keeps the Critical Alerts entitlement justification true, and it
  has a test.
- Critical interruption level with a SHORT sound (`recheck.wav`, ~1 s, bundled
  via the expo-notifications `sounds` array), never the 30-second siren. Own
  category `RECHECK_V1`; the critical alert still carries no category at all.
- Lock-screen actions WORSE / MUCH WORSE / SAME submit with
  `opensAppToForeground: false` — no Face ID, no passcode. That is the PRIMARY
  answer path (Paul, 2026-08-18); tapping the body opens `/recheck` (four
  ~64 pt buttons) as the secondary path. BETTER is in-app only.
- Escalation one-way: WORSE moves one band, MUCH WORSE reaches red from any
  band, BETTER is recorded as "reports improving" and NEVER auto-downgrades.
- Tap time authoritative: `answered_at` (device) is what every human surface
  renders and sorts by; `received_at` kept alongside; >2 min gap flagged
  "queued offline"; an implausible device clock is flagged, never corrected.
- Non-responses written as `recheck_missed` rows — "we asked and heard nothing"
  is a positive fact in the record.
- Two kinds of silence on `/api/devices`: `silent_alive` (missing answers, phone
  still reporting) vs `dark` (>45 min of nothing). Neither reduces priority; a
  dark phone is not prompted, because asking a dead phone achieves nothing. An
  operator restart brings it back.
- Kill switch with a UI path: `POST /api/admin/recheck/enabled`,
  `GET /api/admin/recheck/status`.
- Dashboard: re-check rows in the history modal (answer, ↑ deteriorating,
  offline-queue lag, device-clock warning), plus card badges for deteriorating,
  silent_alive and dark.
- 35 unit tests in `backend/tests/test_recheckin_c1.py`.

Deferred to C1 phase 2: operator-initiated `POST /api/admin/recheck` with target
selector + battery-cost confirm dialog, and the "help is on the way" line (needs
real zone assignment, never a free-text morale message).

### Spec written, not built
`memory/swarm-grouping-design.md` — one updating notification per swarm, a
much-larger event always stands alone, never applied to the critical alert.
§6 answered by Paul 2026-06-18: use the EMSC region string verbatim when every
member shares it, our own broader label only when they differ; first notice
sounds, updates silent, a stand-alone larger event always sounds; stand-alone
magnitude gap **0.5**, not 0.8 (magnitude is logarithmic — burying a
significant quake costs more than a needless notification).

---

## 2026-06-18 (later) — A1 reopened, singular/plural fixed as a class, C1 proven

### 🔴 A1 reopened: chart still contradicted the sentence beneath it
The August fix de-duplicated within a bucket. That is not enough: someone still
trapped an hour later reports again — and the C1 re-check ladder makes that
routine — so one person produced a red bar of 1 in each of three consecutive
hours. A reader adds the bars up and reads three trapped people while the
sentence underneath says one. On the public report that misreading reaches the
press and cannot be corrected afterwards.

`_bucket_timeline` now counts each device **once per status for the whole
window**, in the period it FIRST reported that status. Consequences:
- Invariant, asserted as a test: `sum(red bars) == "N people told us they were
  trapped"` (`test_red_bars_sum_equals_narrative_trapped_figure`).
- Legend reads "First told us they were trapped / First marked found /
  First checked in safe".
- New caption under the chart on BOTH B1 and B2: "Each person is counted once,
  in the period they first reported that status. Adding the red bars together
  gives the number of people who told us they were trapped." Without it a
  reader has no way to know whether adding the bars is meaningful.
- A row with no `device_id` can no longer become a bar (the narrative counts
  distinct device_ids, so an unattributable row would break the invariant).

### 🔴 Singular/plural fixed as a class, not as a third instance
Three of these have now reached a rendered PDF (#124, A1, egress). Every count
of humans in a narrative sentence goes through `_n_people(n)`, and every
"n of the t people still trapped" sentence through
`_subject_of_still_trapped(n, t)` — which returns "The only person still
trapped"/"has", "All 3 people still trapped"/"have", "1 of the 3 …"/"has". So
agreement is written once, not once per feature. Fixed en route: "The 1 person
still trapped", and the egress line saying "Some of them report only minor
injuries" about a single person.
`backend/tests/test_narrative_grammar.py` generates every narrative sentence
over every count that changes the wording (0, 1, 2, many, n == t) and runs one
shared bank of grammar rules over all of them — 95 cases, no DB, milliseconds.
The bank is digit-guarded: the failing `test_singular_plural_grammar` was a
false positive, because "31 people" contains "1 people".

### 🔴 C1 verified end-to-end (was built but unproven)
213 backend tests green: ladder timing, `much_worse` → red from any band,
BETTER never downgrades, authoritative tap time (answer stamped 40 min in the
past is recorded at tap time), dark phones not prompted, kill switch reflected
in `GET /api/admin/recheck/status`, 400 on a bad answer / 404 on an unknown
device. Frontend `/recheck` verified in the web preview: four buttons, deep
link with `check_id`, confirmation screen, offline-queue notice. Lock-screen
notification ACTIONS remain untestable outside a real build — unchanged.

No mobile app change in this pass, so the version stays **1.0.27**.

### App version
1.0.27 (parseUtc timestamp fix, /recheck screen + lock-screen answers,
recheck.wav bundled, Diagnostics "Reset Apple Watch reminder", diag fix marker).

---

## 2026-06-18 (later still) — batch 6: B3, B6, C1 phase 2

Paul's order for the remaining batch 6 items: one app-side batch first
(B1 + B3 + B4 + the triage reword) because every build costs him a publish, an
App Store Connect step and a test cycle; then B6 (dashboard only, no build);
then C1 phase 2; then swarm grouping. Android alert sound stays deferred with
the Android launch work.

### Already shipped, only unverified (Paul is on 1.0.25 / 126)
B1 (metrics above the buttons, "Dismiss alert" gone), B4 ("alerted", not
"woken" — zero matches left in the app) and the triage reword + egress question
are all in 1.0.26/1.0.27. Re-verified this session at 390×844 and 320×568.

### 🟠 B3 — the third tremor option states what it costs, in its title
Was: title "Everything nearby", subtitle "Including tremors too small to feel".
Now the clause is IN the title — "Everything nearby — including tremors too
small to feel" — because the title is the only line a scanning reader reads,
and the option that generates the most notifications must not look like the
quiet one. Three options and the always-on green panel are unchanged.

### 🟠 B6 — the activity feed is grouped by person, in plain sentences
It was a raw event log: one row per update, so a person who re-reported four
times filled the panel four times and the operator reconstructed "who is where
and how are they now" by reading backwards. It also printed wire values
(`not_responding`) at the operator.
- Default view is **By person**: one row per person, most urgent first, with
  their current state, a plain sentence for the latest thing that happened,
  the update count in the window, battery, a map recentre link and 📜 Full
  history (the SAME modal as the triage cards — `window.qgOpenHistory`).
- **Every update** (the old log) is one click away and the choice is
  remembered in localStorage; an operator reconstructing a sequence needs it.
- Colour is never the only channel: each state carries a **shape** (circle =
  needs help now, triangle = needs help, square = minor, tick = found, dot =
  safe, diamond = not responding) and a **word**, matching qgSeverityChip and
  the map markers, so it survives greyscale printing and colour-blindness.
- Wire values are gone from both views: "Recorded as not responding.",
  "Told us they need help — minor injuries. They cannot get out on their own.",
  "Answered a re-check: much worse — urgent." Severity badges read
  IMMEDIATE / SERIOUS / MINOR even in the pre-qgSeverityChip fallback.
- Row timestamps read "14 minutes ago · 13:05 UTC" — elapsed for staleness,
  absolute for the radio and the record.
- **Backend:** `/api/audit` now labels C1 rows `recheck_sent` /
  `recheck_answered` / `recheck_missed` with `answer`, `answered_at`,
  `deteriorating` and `queued_offline`. They previously arrived labelled
  "status", so an automatic re-check answer was indistinguishable from a
  person opening the app and reporting themselves.
- 10 tests: `backend/tests/test_b6_activity_feed.py`.

### 🔴 C1 phase 2 — the operator can ask now, and sees what it costs first
`POST /api/admin/recheck` (operator or admin):
- `confirm: false` (the default) is a **preview that sends nothing** — it
  returns who would be woken, their battery, and the cost in plain words.
  Pressing "ask now" wakes injured people's phones, so the cost goes in front
  of the operator before it happens, not after.
- `confirm: true` sends, via the SAME `_dispatch_rechecks` path as the
  automatic sweep — same invariants, same ledger rows, same rescheduling — so
  a manual ask can never drift from an automatic one. Ledger rows carry
  `initiated_by` + `manual: true`, and a `recheck_audit` row records who asked,
  why, and whom.
- Optional `device_ids` and `severity` targeting (e.g. red only).
- Two refusals with no override, because the Critical Alerts entitlement rests
  on them: not currently `trapped` → never asked; phone dark → never asked
  (it cannot answer). Both come back in `skipped` with a plain reason. A
  broadcast preview does NOT list every safe phone in the database — a 400-row
  skip list buries the two entries that matter.
- The cost text states facts, never an invented percentage: "This will wake 4
  phones belonging to people who told us they need help. 1 of them is on less
  than 10% battery. … Their next automatic check is rescheduled from now, so
  asking does not add an extra one on top."
- Dashboard panel above the activity feed (live operations, not the admin test
  drawer): ladder state in plain words, the two-step ask, and pause/resume with
  an explicit "Only an admin can pause or resume automatic checks."
- 11 tests: `backend/tests/test_c1_phase2_manual_recheck.py`.

### Deploy order for this landing
Publish the backend FIRST (the feed depends on the new `/api/audit` kinds and
the panel on `POST /api/admin/recheck`), then push the dashboard to GitHub.
App version stays **1.0.27** — the B3 wording lands in it; no build exists yet.

### D1 — ANSWERED by Paul, 2026-06-18: name the region in tremor notices
"(a) — name the region in tremor notices, e.g. '3 tremors near Sicily,' instead
of only showing a distance." So D1 is a WORDING requirement on the informational
tremor notice, not a new subscription model and not a dashboard concept —
"Places I care about" (named place + coordinates + trigger tier, `user_places`)
already covers the subscription side and is unchanged.

D1 therefore folds into swarm grouping, which already has the region rule
Paul set: use the **EMSC region string verbatim** when every member of a group
shares it, and fall back to our own broader label ONLY when they differ. Single
(ungrouped) notices should carry the EMSC region verbatim too — EMSC is
authoritative and we must not introduce errors into it. Build the two together;
the distance stays, the region is added, not substituted.

### Dashboard repo — RECORDED at last (2026-06-18)
Pushed by the agent with a one-day fine-grained PAT and verified by reading the
file back byte for byte. **Revoke that PAT.**
- Repo `PaulVincentiSafequake/SafeQuake`, branch `main`
- Dashboard file `backend_dashboard/public/index.html`
- Repo contains only `backend_dashboard/`: `package.json`, `server.js`,
  `usgs_poller.js`, `public/index.html`. `dashboard-v3.js` is already deleted —
  that carried-over cleanup item is closed. Cache-Control (item 1.1) still open;
  it belongs in `server.js`, which IS in this repo and now reachable.
- The app repo (`quake-angel-app`) is separate and does not deploy the dashboard.

Commit `c871710073` — B6 person-grouped feed + C1 phase 2 panel, PLUS two live
bugs found only because the live file was diffed against the staged copy instead
of being overwritten by it (the live file was AHEAD on C3, so a wholesale
overwrite would have reverted names on pins):
1. The map popup printed the name twice — "ABC12 Anna — Anna". C3 was hand-made
   in the repo and added the styled name without removing the older
   `" — " + u.displayName` line.
2. The popup never showed "Cannot get out — extraction needed", so the egress
   answer was invisible on the map.

**Lesson for next time: never push the staged copy over the live file without
diffing first.** The staged copy in `memory/dashboard_build/` is only as current
as the last hand-edit made directly in the repo.

## v1.0.38 — Neo round (2026-08-20)

### #266 / #260 — Truthful "on the alert list" registration status
Root cause proven live on preview backend:
1. Old `/register-push` wrote to Mongo BEFORE calling relay, and on relay 401
   raised `HTTPException(500)`. Row survived, dashboard counted it, but the
   phone had no way to receive pushes — the false promise Paul flagged.
2. Old "Is this working?" screen had two rows:
     - "signed up" → local `token_length > 0` (no server check)
     - "server confirmed" → last-response 2xx
   Both were local-only. They could disagree with the server (green tick
   while dashboard shows 0).

Fix (v1.0.38, build 38):
- **Backend `/register-push` reorder**: call relay first. 2xx → upsert + 201.
  4xx (except 429) → NO upsert, return 502 with plain-English detail.
  5xx or network → best-effort upsert + 502 with retry message. Always
  log to `push_registrations_log`.
- **New `GET /api/register-push/status/{user_id}`**: phone asks server
  "do you actually hold my registration?". No auth (phone asks about its
  own uid). Returns registered/platform/last_seen/dead_token/last_attempt/
  relay_healthy.
- **New `GET /api/admin/relay-health`**: admin+operator. Returns healthy
  + plain-English reason ("EMERGENT_PUSH_KEY appears missing…" etc.) so
  the dashboard can show "any 0 count below is misleading".
- **Mobile Diag**: replaced the two rows with ONE row driven by the
  server round-trip. Red states have plain-English server-side detail.
- **Dashboard**: amber banner above Registered devices count when relay
  is refusing (or grey info banner when no attempts yet).

### #265 — Dashboard sign-in unresponsive until browser restart
Same class as em-dash statusText: one silently broken call kills sign-in.
- Root cause: `renderSignInButton()` was called from ~5 code paths. Each
  did `container.innerHTML = ""` + re-ran `google.accounts.id.initialize()`.
  Google's docs say initialize is one-shot; repeated calls leave the
  rendered button click a no-op. Rebuilding the DOM orphans Google's
  internal handlers.
- Fix (three parts): idempotency (return early if banner + iframe are
  already visible); `_googleInitialized` guard so initialize runs exactly
  once per page load; stuck-button watchdog that shows a plain-English
  "sign-in didn't respond, reload the page" hint 8s after a click if no
  credential callback fires. Also `use_fedcm_for_prompt: false` to bypass
  Google's `g_state` FedCM cooldown cookie (the exact mechanism that made
  "restart the browser" the only workaround).

### What Paul needs to do to ship v1.0.38
1. Publish (Emergent button) to redeploy backend so the new endpoints and
   ordered `/register-push` are live in prod.
2. Push dashboard `index.html` to `PaulVincentiSafequake/SafeQuake`
   (agent needs a fresh short-lived PAT).
3. Rebuild native iOS app (1.0.38 build 38) so the Diag screen change
   ships.

Verified live on preview backend per rule 4a: I registered a device,
read back what the server actually holds, and confirmed:
- register-push returned 502 with plain-English detail (not "HTTP 500").
- push_devices had 0 rows (no phantom persist).
- status endpoint returned registered:false with truthful last_attempt.
- admin/relay-health returned healthy:false with the credentials reason.

## v1.0.39 — Neo round (2026-08-20)

### #208 — critical-alert routing on locked phones (from ANY screen)
Root cause: the `shouldRedirectToAlert()` watcher lived only in
`app/index.tsx` (Home). Every other screen was a dead end for an
unanswered alert. The tap handler's `router.push("/alert")` can race
the router-ready state on resume-from-lock (iOS suspends JS while
locked) and silently no-op; Home compensated because it had its own
mount check, no other screen did.

Fix: moved the watcher into `app/_layout.tsx` (which wraps every
route). Runs on:
  - Every layout mount (covers cold start).
  - Every `AppState` transition to "active" (covers background→
    foreground, lock→unlock, phone-call return, Control Center dismiss).
  - Every pathname change to anything except /alert or /recheck.
Uses `router.replace` (not push) so back-stack doesn't accumulate a
Diagnostics screen underneath /alert. 750ms redirect debounce so
concurrent triggers (AppState + pathname) don't stack a redirect on
top of a redirect. Removed the duplicate watcher from `index.tsx`.

### #267 — typed-confirmation dialogs (all 5 sites)
Three safety-critical (SIREN / STANDDOWN / WIPE) + two operator-mgmt
(delete-user / role-change, still email-typed). All go through the
same `confirmTyping()` helper.

Changes in `confirmTyping()`:
  - Case-insensitive + trim() (matches backend `.strip().upper()`).
  - Enter to submit; Escape to cancel.
  - On mismatch: plain-English named message inside the modal
    ("That did not match. Type SIREN to send the alert.") — replaces
    the silent red-border flash that Paul reported as "the button was
    broken".
  - Input auto-selects on error so the operator can retype in place.
  - Error clears the moment they resume typing (never a stale red
    warning while they're clearly correcting).

Backend constants:
  - TRIGGER_ALERT_CONFIRMATION: "SEND EARTHQUAKE ALERT TO ALL PHONES"
    → "SIREN"
  - STAND_DOWN_CONFIRMATION: "STAND DOWN THIS ALERT" → "STANDDOWN"
    (letter-distinct from SIREN — 3-char no-overlap invariant tested)
  - DEVICE_PURGE_CONFIRMATION: "CLEAR ALL DEVICES" → "WIPE"

### #135 — idle timeout (2h, alert-aware, protects in-flight work)
  - IDLE_TIMEOUT_MS: 15min → 2h.
  - New backend endpoint /api/admin/incident-status: returns
    `active=true` if the most recent trigger has no stand-down after
    it AND is within a 72h window (matches ALERT_ACTIVE_MS in the
    dashboard). Plain-English `reason` field.
  - Dashboard polls it every 30s. While `active=true`:
      - Idle timer is FROZEN (no expiry, no warning modal).
      - Red banner across the top of the dashboard:
        "⚠ Alert live — idle sign-out suspended. [reason]"
  - Post-alert (stand-down sent), the timer resumes with a fresh
    full-length 2h — never sign an operator out the instant they
    close an incident.
  - Warning modal now lists in-progress work by name (open typed-
    confirm modals, focused non-empty inputs) so the 60s countdown
    is not blind.
  - After a sign-out that discarded work, a persistent amber banner
    names what was NOT saved. Dismissable.
  - #240 already handled personal-data wipe on sign-out — this build
    just plugs the "silent discard of in-flight input" gap Paul
    called out.
  - Trigger endpoint now stamps `kind: "trigger"` on push_events
    inserts (used to be untagged, only stand-down was tagged). Tolerant
    of historical rows via `{"kind": {"$exists": false}}` fallback.

### Preview-mode enrolment — last-seen + dead-marker + all-dead banner
Paul's report: "the enrolled devices list held qg-1786015151886-2zbf6xjy,
an old install of mine that no longer exists. My live phone was never
enrolled, so preview notices were being sent to a dead device."

Preview panel now cross-references each enrolled `device_id` against
/api/admin/device-registry:
  - Green "live" + last-seen timestamp when the device is in the
    registry and has an alive token.
  - Red "unreachable (dead token)" when APNs marked it dead.
  - Red "no longer exists in the registry" when the enrolment refers
    to a device that has been purged / never existed. Preview notices
    to this id go nowhere.
  - If preview mode is ENABLED and every enrolled device is dead,
    the panel shows an amber banner right above the list. No more
    "0 enrolled" or "1 stale enrolled" silently looking fine.

### #57 / #265 — dashboard cache-busting (belt-and-braces)
  - `<meta http-equiv="Cache-Control" content="no-cache, must-
    revalidate, max-age=0">` etc. in the head. Browser must
    conditional-GET every load; a stale copy from a caching proxy
    can no longer stick around across a deploy.
  - New DASHBOARD_BUILD_STAMP baked in near the top of body,
    format `YYYY-MM-DD-hhmmZ-feature-slug`. Bumped by hand every push.
  - StaleCopyWatch (self-check): every 5min the tab re-fetches its
    own URL with `cache: 'no-store'` and extracts the fresh
    DASHBOARD_BUILD_STAMP via regex. If different from the running
    tab's stamp, an amber "A newer dashboard has been deployed"
    banner appears at the bottom with a Reload button.
  - Still to do (needs push access): add explicit
    `Cache-Control: no-cache, no-store, must-revalidate` headers in
    `backend_dashboard/server.js` — the ultimate belt. Meta tags are
    the braces; both together make the caching problem structurally
    impossible.

### What Paul needs to do to ship v1.0.39
1. Publish (Emergent button) to redeploy backend so the new
   /api/admin/incident-status endpoint + phrase changes + trigger
   kind stamp are live in prod.
2. Provide a fresh short-lived GitHub PAT so I can push the dashboard
   changes (already staged in /app/memory/dashboard_build/index.html —
   contents include #267 modal, #135 idle timer + banner + poster,
   preview-enrolment cross-reference, cache-busting meta + watchdog).
3. Rebuild the native iOS app as 1.0.39 build 39 so the #208
   global unanswered-alert watcher lands on the phone.

### Verified live per rule 4a
  - /api/admin/incident-status: no-trigger state → active:false; after
    a trigger with no stand-down → active:true with named reason;
    after stand-down → active:false again.
  - /api/trigger-alert 400 on "wrong" now returns
    "That did not match. Type SIREN to send the alert."
  - /api/admin/alert/stand-down 400 on "wrong" returns
    "That did not match. Type STANDDOWN to recall this alert."
  - /api/admin/device-registry/purge-all 400 on "wrong" returns
    "That did not match. Type WIPE to erase every registered device."
  - "siren" / "  SIREN " / "SirEn" all accepted (case + trim).
  - 23 pytest cases pass across the three test files edited/added.

---

## v1.0.40 (build 40) — 2026-08-20 — #208 diagnostic probe

Strict directive from Paul (Neo round): stop feature work; add ONLY
the diagnostic probe requested. Do NOT alter routing logic, payload
fallbacks, or the /quake/[unid] UI. Root cause of the wrong-screen
lock-tap is currently unknown — probe existence is what makes the
next fix evidence-based instead of another guess.

### What ships in v1.0.40
1. `src/utils/tapProbe.ts` — AsyncStorage-backed ring buffer, last
   5 entries under key `diag.tapLog`. Fields per entry: ts,
   source (`response` | `lastResponse`), actionIdentifier, rawPayload
   (verbatim from `notification.request.content.data`), extracted
   discriminators (kind, action_url, hasCheckId, hasMagnitude,
   hasUnid), and the exact `chosenRoute` string handed to
   router.push / router.replace.
2. `app/_layout.tsx` — `handleTap()` gained an optional `tapCtx`
   arg; each of its five router navigation branches (recheck /
   critical / reminder / stand-down / external / informational
   fallback) now calls a local `logChoice(route)` immediately
   BEFORE the router call. Wiring points: the
   `addNotificationResponseReceivedListener` callback tags entries
   with source="response"; the cold-start
   `getLastNotificationResponseAsync().then(...)` tags entries with
   source="lastResponse". The foreground-receive path
   (`addNotificationReceivedListener` for critical_alert while app
   is open) intentionally does NOT log — it isn't a tap.
3. `app/diag.tsx` — new visible section "For support — last 5
   notification taps". Renders each entry as: `#idx · source · ts`
   header, then `action:`, `kind + action_url`, `magnitude/unid/
   check_id ✓/✗`, `chosenRoute:`, then the raw payload as
   pretty-printed JSON. Two buttons: **Copy tap log** (uses
   `expo-clipboard`, falls back to Share on error; prefixes the
   text with a header identifying app version + build + entry
   count + capture timestamp) and **Clear log** (disabled when
   empty). Loaded via `getTapLog()` in the same `load()` promise
   that already drives the screen, so pull-to-refresh updates it.

### What v1.0.40 does NOT do
- No changes to routing decisions in `_layout.tsx`.
- No changes to the payload builder in `backend/apns.py`.
- No changes to the /quake/[unid] UI (Magnitude/Location "—" issue
  is intentionally deferred until the probe tells us which layer
  is at fault).

### How Paul verifies
1. Build v1.0.40 build 40 to the phone.
2. Trigger a critical alert from the dashboard. Lock the phone.
3. Tap the notification from the lock screen (reproduces the #208
   failure).
4. Open the app → Diagnostics → scroll to "For support — last 5
   notification taps" → Copy tap log → paste back to support.
5. Log fields tell us definitively whether the payload delivered
   to the device carried `kind: "critical_alert"` /
   `action_url: "/alert"` / `magnitude`, and which of the five
   router branches actually fired. That is the evidence needed to
   fix the correct layer in v1.0.41.

### Lint / type-check
- ESLint clean on _layout.tsx, diag.tsx, tapProbe.ts.
- `tsc --noEmit` produced only three pre-existing errors
  (alert.tsx AudioPlayer.stop, index.tsx `Alert` name,
  eventReadings.ts LocalSearchParams) — none introduced by this
  probe.

### Package added
- `expo-clipboard@8.0.8` via `yarn expo install expo-clipboard`
  (needed for the Copy button; the existing Share-based flow is
  kept as fallback).

---

## v1.0.40 backend fix — 2026-08-20 evening — #208 root cause closed

Paul's v1.0.40 build 40 probe returned `rawPayload: {}` on a real
lock-screen tap. The empty payload proved the routing code was never
at fault. Investigation of the four possible layers isolated the
cause to the shape of the APNs userInfo dict this backend builds.

### Root cause (with citation)
`node_modules/expo-notifications/ios/EXNotifications/Notifications/
EXNotificationSerializer.m` lines 80–84:

```objc
+ (NSDictionary<NSString *, NSObject *> *)serializedNotificationData:
                                          (UNNotificationRequest *)request
{
  BOOL isRemote = [request.trigger isKindOfClass:
                    [UNPushNotificationTrigger class]];
  return isRemote ? request.content.userInfo[@"body"]
                  : request.content.userInfo;
}
```

For remote (APNs) pushes, expo-notifications populates `content.data`
from `userInfo["body"]` only — never from siblings of `aps`.
Every APNs push this backend built put routing keys as siblings of
`aps` (the standard APNs shape), so `content.data` arrived as `{}` on
every iOS device the app has ever shipped to.

### Symptoms this closes
- #208: lock-screen tap on a critical earthquake alert lands on
        /quake/unknown instead of /alert (Paul's probe log, v1.0.40).
- #174: tremor tap opens a blank screen (missing preview payload).
- #205: notification carries magnitude but detail screen renders "—".

Same wrong-nest. One fix closes all three.

### What changed
`/app/backend/apns.py` — three builders now nest routing keys under a
top-level `body` dict at the APNs userInfo layer:
- `_build_critical_payload`
- `_build_preview_payload`
- `_build_recheck_payload`

`aps` (Apple's reserved namespace) stays at the top of userInfo — the
title/body/sound the phone displays on the lock screen were never
broken and still travel that path.

### What is NOT changed
- Mobile app: zero code change. Paul stays on v1.0.40 build 40. The
  tap handler already reads `data.kind`, `data.action_url`,
  `data.magnitude` etc.; those keys will now actually be present.
- Android SuprSend path in `server.py`: untouched. FCM's Android
  serializer (`NotificationSerializer.java:63-74`) copies data-as-is
  when `body` isn't a JSON string, so leaving Android's dict shape
  alone preserves current Android delivery.
- Probe (`tapProbe.ts` + diag UI + `_layout.tsx` logging): unchanged.
  It's the verification path.

### Tests
- Old tests that pinned `p["kind"]`, `p["action_url"]`, `p["check_id"]`,
  `p["escalated_to_critical"]`, `p["consecutive_missed"]` updated to
  assert on `p["body"][...]` to reflect the new nest.
- New file `tests/test_apns_body_nesting_208.py`: 5 pinning tests that
  fail loudly if any future refactor "flattens" custom keys back to
  the top level, with a docstring citing the expo-notifications iOS
  serializer contract so nobody has to re-discover this bug.
- All 16 originally-affected tests + 5 new tests pass locally against
  the preview backend.

### Deployment
Backend only. Paul hits Publish (top-right) to redeploy production.
No new mobile build required.

### Live verification protocol (Paul's phone)
1. Publish backend to production.
2. From deployed dashboard, tap Trigger Earthquake Alert.
3. Lock iPhone (v1.0.40 build 40 already installed).
4. Tap the critical notification from the lock screen.
5. Expect: land on /alert with siren looping (the bug fix effect).
6. Open app → Diagnostics → "For support — last 5 notification taps".
7. Entry #1 should read: `kind: critical_alert`,
   `chosenRoute: /alert?siren=1&…`, `rawPayload: { kind, action_url,
   magnitude, distance_km, intensity }` — no more `{}`.

If entry #1 is still `{}`, diagnosis was wrong; do NOT ship further,
probe deeper.

---

## 2026-08-20 late evening — #208 confirmed live + regression sweep

### #208 verified live by Paul on his own iPhone
Real earthquake alert triggered from production dashboard → lock
screen tap → landed on /alert with siren looping and safe/injured
buttons showing. Tap log entry #1: kind=critical_alert,
action_url=/alert, magnitude ✓, chosenRoute=/alert?siren=1&
magnitude=6.4&distance_km=12&intensity=VII, fully-populated
rawPayload. Previous empty entry preserved in the log for
comparison.

### #174 and #205 — closed by the same v1.0.40 backend fix
Both are downstream symptoms of the same wrong-nest bug the #208
probe exposed. No separate work needed. Test path documented in
Paul's message reply.

### Regression sweep — findings

Backend (all tests exercising touched surfaces):
- 183 tests pass on the payload-adjacent files
  (test_apns_body_nesting_208, test_batch5, test_recheck_escalation,
  test_recheckin_c1, test_batch5_independent_iteration_32,
  test_c1_phase2_manual_recheck, test_neo_batch_2026_08_20,
  test_neo_255_place_notice, test_preview_radius_override,
  test_seismic_map, test_trigger_alert_confirmation).
- 5 failures on this slice — verified by git-stash comparison to
  be PRE-EXISTING (identical failures on the pre-fix commit). Not
  caused by the payload nest change:
    * test_neo_255_place_notice.py: 3 preview-place-notice pipeline
      tests failing on "expected 1 row, got 0" — unrelated to
      payload shape, live for backlog cleanup.
    * test_batch5.py::TestBuildIdentification: fixed in this sweep
      (updated the hard-coded IPA marker from "#208 R4 primary
      alert" to "#208 mobile probe" to match the current v1.0.40
      diag.tsx wording — same test intent, current build's marker).
    * test_batch5.py::TestIssue169SirenPlaysOnTest:
      test_home_test_trigger_passes_siren_and_test_params flags a
      substring inside a source comment, not real code. Pre-existing.
- Broader suite (all tests): 631 pass, ~140 fail on 401 auth
  (fixtures missing ADMIN_TRIGGER_PASSWORD context) — pre-existing
  environmental issue, verified identical on pre-fix commit.

Backend API surface:
- GET /api/ → 200
- GET /api/admin/incident-status → 200
- GET /api/admin/relay-health → 200
- Backend restarted cleanly on new code; EMSC poller, testimonies
  sweeper, and re-check sweeper all started.

Dashboard renderer (routes_diagnostics.py):
- Reads only payload["aps"].sound.critical/name and
  ["aps"].interruption-level for the badge summary. Everything
  else renders as JSON in <pre>. The nested body dict simply
  appears in the pre block. No renderer changes needed.

Mobile:
- No mobile code changed since Paul verified v1.0.40 build 40 live.
- Lint on /app/frontend/app: 4 problems in alert.tsx and
  quake/[unid].tsx, all pre-existing and unrelated to the probe.
- Preview frontend serving 200 on / and /diag.

### Not started
Home-screen redesign (§8) — held per Paul's instruction until the
regression sweep is signed off.

---

## #268 — the phantom casualty (Neo, 2026-08-21)
App version unchanged: **v1.0.40, build 40** (no mobile code touched).
This round is **backend + dashboard only**.

### The defect
The rescue board showed two entries that were both Paul: `CW7EF` (his
live phone) and `F6XJY` (an old install that no longer exists on any
phone). `F6XJY` read "Not responding · Phone dark since 21:08 · Battery
?%" — to an operator, a missing person with a last known position. In a
real incident a team is dispatched to find nobody while a real missing
person waits.

### Doctrine encoded (Paul's words, kept verbatim in the code)
1. "Silence is information, but only if we know what kind of silence it
   is." → four states, never one bucket.
2. "Never infer that anyone is safe from an absence of data." → nothing
   in this work marks anyone safe or reduces concern.
3. "Never delete a person from a rescue board." → nothing deletes. The
   strongest software action is `on_working_board = False`, which moves a
   record to a labelled area an operator can open.
4. "Status always outranks device state." → any record that has EVER
   reported needing help (trapped / needs-extraction / rescued in the
   append-only `status_events` ledger, or `trapped_since` on the row)
   cannot leave the working board, whatever the token says. Guaranteed by
   the first branch of `record_state.classify` and pinned by tests.

### The four states (`/app/backend/record_state.py`)
| wire value | label read aloud | on the working board |
|---|---|---|
| `waiting_for_answer` | "Waiting for an answer" | yes |
| `phone_went_dark` | "Phone went dark" | yes (never downgraded) |
| `app_removed` | "App removed from this phone" | no (unless an alert is live) |
| `never_used` | "Never used the app" | no |
| `resolved_by_operator` | "Resolved by an operator" | no |

### What the phone networks actually tell us — and the limits
- **Only APNs `Unregistered` (410)** may claim the app was removed. It is
  a positive fact reported by Apple, not an absence.
- **`BadDeviceToken` no longer counts.** Before #268 both reasons were
  treated identically, so a prod/sandbox mismatch on our side could
  manufacture a phantom "removed". It now reads "Phone went dark" plus a
  technical note.
- **A destroyed / flat / out-of-signal phone produces no `Unregistered`
  at all** → lands on "Phone went dark". That is the safe default and it
  is deliberate.
- **`Unregistered` cannot separate "user deleted the app" from "phone
  wiped/restored"**. Reported as what it is, never as intent.
- **Android can never be classified "app removed"** — the relay reports
  chunk-level HTTP status, not per-token invalidity. Android silence is
  always "Phone went dark". Told to Paul explicitly.

### Where the doctrine collided, and how it was resolved
Paul's rule 1 ("a token dying mid-incident for someone who has not
answered — do not move them") and rule 4 ("dead and removed devices must
not be counted in 'not responding' anywhere") both had to hold at once.
So a removed-app record during a live alert stays **on** the board with a
held-reason line, and is counted in its own named bucket
(`app_removed_held_on_board`) instead of inside `not_responding`. Anyone
with help history is exempt and is counted as the person they are.

### Thresholds, and how they were chosen
- Dark after **45 minutes** normally, **15 minutes** for anyone who has
  reported needing help (Paul: "for them, silence is clinically
  meaningful"). The card always shows the real elapsed time as well.
- Mass-dark notice: a cluster of **≥5** records going quiet inside a
  **10-minute** window that is **≥40%** of everyone in contact. 10 min
  because our own check-in ladder is 15 min coarse, so phones that died in
  the same second can be ~15 min apart in last-contact stamps; ≥5 as an
  absolute floor so a 3-tester board does not cry wolf; ≥40% so a large
  deployment does not fire on 5 unrelated dropouts in 500. Both tests
  must pass. The notice never moves or reclassifies anyone.
- Duplicate suggestion: handover within **30 minutes** (required) AND
  same first name OR last positions within **150 m**. Different names,
  both recorded, is a **veto**. Test entries are excluded.

### Files
- NEW `/app/backend/record_state.py` — the classifier, thresholds,
  mass-dark detection, help/location history helpers, live-alert check.
- NEW `/app/backend/duplicates.py` — suggestions with evidence, no writes.
- `people_counts.py` — `load_board()` is now the single source of truth
  for who is on the working board; new counts + `counts_notes()` /
  `counts_notes_short()` / `moved_by_words()`.
- `server.py` — `/api/devices` returns `devices` (working board only),
  `off_board`, `notices`, `counts`, `count_notes`; new endpoints
  `POST /api/admin/records/{id}/resolve|unresolve|duplicate-decision`;
  `/api/public/summary` carries the new counts + notes; a check-in now
  un-resolves a record (software may only ever move a record TOWARDS the
  board); purge-all refuses during a live alert and keeps back every
  help-history record, reporting what was kept.
- `apns.py` — `APP_READ_REMOVED` contract: `APP_REMOVED_REASON =
  "Unregistered"` named next to `DEAD_TOKEN_REASONS`.
- `reports_export.py` — B1 gains the indented breakdown, the exclusion
  notes and a "Records not on the working board" appendix; B2 (strict
  one-pager) gains a one-sentence exclusion note, paid for by tightening
  two spacers; the audit CSV gains counts, notes and one row per
  off-board record.
- `/app/memory/dashboard_build/index.html` — state line + held reason +
  token note on every card, duplicate banner with Confirm/Reject, count
  provenance under the numbers, mass-dark notice above the list, and a
  collapsible "Not on the working board (N)" area with Put-back.
- NEW `/app/backend/scripts/seed_268_scenario.py` — PREVIEW-ONLY seeder
  that reproduces all four states plus a duplicate pair.

### Tests
- NEW `tests/test_record_state_268.py` — 31 pure unit tests pinning every
  sentence of the doctrine (status outranks device state, live-incident
  hold, Unregistered-only, both thresholds, never-demote-a-located-person,
  radio-safe labels, mass-dark maths, duplicate evidence and vetoes,
  counts excluding set-aside records).
- NEW `tests/test_record_decisions_268.py` (8) — endpoint refusals:
  unauthenticated, unexplained, unknown record, invented verdict.
- NEW `tests/test_devices_payload_268.py` (1) — payload shape pinning
  (own module: this suite tolerates one DB-touching request per module).
- UPDATED `tests/test_export_hardening.py` (public summary shape),
  `tests/test_count_consistency.py` (fake DB gained the collections the
  board reads), `tests/test_device_purge_all_262.py` (keeps-back doctrine).
- Regression sweep, file by file: no new failures. All report/export/
  count/recheck/audit/user-management files green. The pre-existing
  failures (schema-drift push/debug suites, a short_code that happens to
  contain "B1") are unchanged.

### Verified where
- Verified live on the PREVIEW backend + a local copy of the deployed
  dashboard: seeded all four states, saw them classified through
  `GET /api/devices`, clicked "Yes — same person" in the browser and saw
  the real endpoint resolve the older record with attribution, checked
  the B1 appendix, the B2 one-pager and the audit CSV.
- NOT yet verified on production (`quakeangel.app` +
  `quake-alert-18.emergent.host`): needs Paul's Publish for the backend
  and a `git push` for the dashboard.

### #268 follow-up — two holes closed the same day
An independent backend sweep (35 fresh HTTP tests,
`tests/test_268_end_to_end.py`) tried to break the doctrine and found two
real paths:
1. **Answering "same person" could drop a casualty.** The endpoint resolved
   the OLDER record by date alone, so confirming a duplicate moved a
   TRAPPED person off the working board — software choosing to remove a
   casualty. Fixed: if exactly one of the pair has help history, that one
   is KEPT whatever the dates say; if the record that would be resolved
   has help history anyway, the endpoint refuses with 409 and tells the
   operator to resolve it explicitly. The operator's answer is now written
   down only AFTER the guards pass, so a refusal cannot silence the
   suggestion for ever.
2. **A record could be marked a duplicate of itself**, self-resolving with
   a self-contradictory message. Now a 400.
Also hardened while there: `/resolve` on a record that has ever reported
needing help now requires `acknowledge_help_history: true`. It is still
allowed — a human may know they are accounted for — but never by accident,
and it is recorded against their name.
Five new tests pin all of it. Full #268 suite: 40 HTTP + 40 unit, all green.

### #268 follow-up 2 — the app-removed fact is now durable
Found while re-checking the work on live preview data: the "app removed"
fact lived ONLY on the `push_devices` registration row. That row is
transient — the admin registry wipe deletes it and a re-register replaces
it — so after an unrelated cleanup a known-deleted app silently reverted
to "Phone went dark" and walked back onto the working board as a missing
person. The exact defect #268 exists to kill, resurrected by a wipe.

Fix: `_prune_dead_devices` now also stamps `app_removed_at` /
`app_removed_source` on the `device_status` record itself (only for
`Unregistered`, never for `BadDeviceToken`), and `classify` reads either
source. Cleared in exactly two places, both of which are positive evidence
that the app is back and both of which move the record TOWARDS the board:
a check-in (`POST /api/status`) and a successful re-register
(`POST /api/register-push`). A check-in also clears a stale `dead_token`
mark on the registration, because a phone that is answering demonstrably
has the app installed; if the token really is dead the next push re-marks
it. Pinned by one unit test and two HTTP tests.

Also in this pass: B2's one-page guard was tightened (10/12 leading, 7.5pt
note) so the exclusions line cannot tip the page on a data-heavy window —
verified stable across repeated runs.

### #268 — plain-language pass (Paul: "I am dyslexic")
Paul, 2026-08-21: *"remember that whenever you create buttons, text and
descriptions and manage layouts, that I am dyslexic and I want both
dashboard and the app to be simple to understand, intuitive to use always
following the most intuitive approach."*

Standing rule written down in **`/app/memory/writing-and-layout-rules.md`**
— read it before adding any text or control to any surface. Summary: one
idea per sentence, ~12 words maximum, everyday words only, lead with what
matters, same thing = same words everywhere, verbs first on buttons, short
lines instead of paragraphs, 12.5px minimum body text, line height 1.5+,
never colour alone, read it aloud before shipping.

Applied to everything #268 added, on every surface (dashboard, both PDFs,
the CSV, API messages and confirmation dialogs):
- "No word from this phone for 3 hours. Last heard 05:36. We cannot reach
  them. The status and place shown are the last we knew." — was one
  clause-heavy sentence.
- Count provenance is now a scannable list: "Not responding: 1 person on
  the working board." / "Set aside: 1 record where the phone said the app
  was removed. A removed app is not a missing person." / "Nothing is ever
  deleted. You can put any record back."
- Every API refusal an operator can see was rewritten the same way, e.g.
  "O268C asked for help at some point. Taking that record off the working
  board needs a second yes. Nothing is deleted, and you can put it back."
- Dashboard type up to 12.5–13px with 1.6 line height, bold lead words on
  scannable lines, 36px reason buttons.
- Tests updated to pin the new wording (they assert the exact operator
  sentences, so a future reword cannot silently reintroduce jargon).

### #268 — "Take off the board", the human path (new control)
Verifying on production exposed a real gap. `F6XJY`'s push registration had
already been deleted by the #262 pre-pilot wipe, and Apple can only report
a token we still hold — so that record can NEVER be reported as removed. It
would have sat on the working board as a missing person for ever with no
control to deal with it. A safety surface with no way out is a defect.

So every card now carries **"Take off the board"**: six plain reasons
(same person listed twice / app removed from this phone / never used the
app / found another way / one of our test entries / something else — say
why), a note field, and a Cancel. "Something else" without a note is
refused in words, client-side, before any request. Anyone who has ever
asked for help needs a second yes, and the dialog is the server's own
sentence, not a paraphrase. Nothing is deleted; "Put back on the working
board" sits on every off-board card.

### Live verification (rule 4a)
- **Verified live on production**, 2026-08-21: loaded
  `https://safequake.onrender.com` (the deployed dashboard, pushed to
  `PaulVincentiSafequake/SafeQuake`, Render redeployed), signed-out view,
  and read the new provenance lines rendered from the live backend at
  `quake-alert-18.emergent.host`: "Waiting for an answer: 0." / "Phone went
  dark: 5." / "Both are counted in the numbers above." plus the
  exclusions line. Production `/api/devices` confirmed serving
  `record_state` for all 43 records, and `/api/admin/device-registry`
  confirmed only ONE registration exists (CW7EF) — which is why F6XJY needs
  the human path.
- **Production backend still carries the pre-plain-language wording** of
  the count notes. The short-line version needs Paul to redeploy.

## 2026-08-21 (evening) — #273 verified, #274, #271, #272, #270

### Build unblocked
Three TypeScript errors left the app failing to compile: `sirenPlayer.stop()`
(expo-audio has no `stop` — now `loop=false; volume=0; pause()`), a missing
`Alert` import in `app/index.tsx`, and a removed `LocalSearchParams` type in
`src/utils/eventReadings.ts` (now `UnknownOutputParams`). `npx tsc --noEmit`
is clean, eslint clean.

### #273 — the siren must obey what the notification SAYS it is
Verified in code and by test: the tap router in `app/_layout.tsx` decides on
`kind` only. `kind === "critical_alert"` is the ONLY thing that can start the
siren. A payload with no `kind` but `action_url === "/alert"` still routes to
the check-in screen, with the siren withheld. `magnitude` is consulted
nowhere in routing. Preview notices carry `kind: "emsc_preview"`
(`apns.py _build_preview_payload`), so a preview tap lands on the
informational screen. Paul still to confirm on TestFlight after redeploying.

### #274 — a stand-down never clears someone still asking for help
`_stand_down_split()` in server.py is the one place that decides. Anyone on
the working board whose effective status is `trapped` (or who needs
extraction) is held back: their phone is NOT told the alert is over, and
they stay on the board. Everyone else is cleared.
`GET /api/admin/alert/stand-down/preview` now returns `clearing_count`,
`staying_count` and `staying_people[]` — name, rescue code, how bad in
words, when last heard (Malta time) and battery — and the dashboard dialog
lists every one of them BEFORE the operator types STANDDOWN. The same split
(`cleared_count`, `kept_on_board_count`, `kept_on_board[]`) is written to the
`alert_stood_down` audit row and returned to the dashboard banner.

### #271 — "Ask them to check in"
One button per card, one person at a time. No bulk ask anywhere — that is a
separate control with its own confirmation (#47), deliberately not built here.
- Gap: **60 minutes** per person. **180 minutes** if battery <= 20% —
  "their phone is their lifeline, and every wake-up spends it" (#189).
- Cap: 2 unanswered asks, then the button refuses in words and points at the
  radio. A fresh answer resets the counter.
- History is printed under the button, always: "Asked twice. Last asked 40
  minutes ago, no answer." When we may not ask yet, the button greys out AND
  says why. One decision function, `server._ask_state`, feeds both the button
  and the endpoint, so the button can never offer what the server refuses.
- The push is an ORDINARY notification: `sound: "default"`,
  `interruption-level: "active"`, priority 5, `kind: "check_in_request"`.
  Never the critical path (#207), never `time-sensitive`.
- Wording Paul chose: title **"Are you all right?"**, body **"No new
  earthquake. Please tap to tell us how you are."** Reassurance first, always.
  Explicitly rejected: anything implying a rescue team is watching the
  dashboard — an operator pressed a button, which is not the same thing, and
  we do not say it until it is contractually true.
- Tapping it opens `/alert?siren=0&checkin=1` — the SAME check-in screen and
  the SAME submit path as a real alert, in calm form: blue not red, "No new
  earthquake." as the first line, no EARTHQUAKE DETECTED, no Drop-Cover-Hold
  on, no readings strip. "I need help" from here is a real report and reaches
  the working board exactly as one made during an alert.
- Someone already trapped gets the re-check prompt instead ("Are you still
  OK?"), because "has anything changed?" is the right question for them.

### #272 — one clock, named
- Screens: Malta time everywhere. Backend renders through `timefmt.py`; the
  dashboard renders through `window.qgTime` / `window.qgWhen`, which pin
  `Europe/Malta` explicitly so a laptop set to another country still shows the
  operations clock. "All times on this page are Malta time." sits with the
  counts.
- Legal records (audit feed, a person's full history, both PDFs): the offset
  is printed beside the local time — "21 Aug 2026, 21:08 (Malta time,
  UTC+02:00)". The dashboard maps the browser's "GMT+02:00" to "UTC+02:00" so
  two records of one instant read identically.
- Machine-readable: export filenames keep the UTC `...Z` stamp; the CSV `at`
  column keeps the full ISO timestamp with its offset; `at_simple` is the same
  instant in Malta time, kept sortable (`YYYY-MM-DD HH:MM`); a `times_note`
  row in the CSV header says which column is which.
- App: `maltaTime()` in `src/utils/time.ts`; the event detail row is labelled
  "Time (Malta)".

### #270 — no developer boxes, no developer words
Every yes/no question in the dashboard now draws OUR dialog through
`window.qgAsk` (backed by `confirmPlain`), so no operator sees a grey browser
box with the site address above our words. Rewritten in short lines with a
verb on the button: preview distance (3 dialogs), both confidential
downloads, add/remove test people, remove logo, mark as test, duplicate
"same person", stop reminders, take off the board, and the two typed
confirmations (SIREN, WIPE) plus the user-management ones. Dialog text now
renders line breaks (`white-space: pre-line`) — before this every dialog was
folded into one block of prose, which is the hardest thing to read at 4am.
The only remaining browser boxes are unreachable fallbacks behind a
`typeof window.confirmPlain === "function"` guard.

### Tests
- New: `backend/tests/test_stand_down_split_274.py` (12) and
  `backend/tests/test_review_274_271_272_270.py` (22, written by the testing
  agent). `test_273_regression.py` rewritten to be order-independent.
- Updated to the #271 doctrine ("went dark" REQUIRES an unanswered ask):
  `test_record_state_268.py`, `test_268_end_to_end.py`.
- `test_export_hardening.py` now pins "Malta time" in the Covers line
  instead of "(UTC)".
- Testing agent round: 45/45 backend cases pass; both frontend flows
  verified (calm check-in screen, and the red alert screen unchanged).

### Still to do
- Paul to redeploy (Publish) for the backend/app changes, and push
  `memory/dashboard_build/index.html` to GitHub for the dashboard.
- #190 (an alert after a stand-down re-opening an incident), #189 (ask the
  lowest batteries least), #47 (a deliberate "ask everyone" control),
  #25 (cluster map markers), #188 (group triage by place), #258 (home screen
  redesign — only on request).

## 2026-08-21 (night) — #275, #276, #277, after Paul's production round

Verified closed by Paul on production, v1.0.41: #273 (preview tap opens the
calm screen, no siren), #174/#205 (every reading present on it), #272 (one
clock, Malta time, offset on the full history). The real alert punched
through Focus, which is the critical entitlement working as intended.

### #275 — the stand-down dialog said 13 casualties who did not exist
Paul: "My working board showed 0 immediate, 0 serious and 1 minor. I believe
the same 13 test people are being counted twice."

He was right, and the count was the dashboard's fault, not the backend's.
The headline line used `staying_count`, which INCLUDES test rows, and the
next line then counted the same rows again as test entries. Checked against
production: `staying_real_count: 0, staying_test_count: 13` — there were no
real casualties at all, all thirteen were the seeded TEST people. The
headline now reads `staying_real_count`, and the two numbers can never
describe the same row twice.

Restructured to Paul's three sections, with headings, the part that matters
in the middle, and names behind one tap ("A count is easy to click past;
names are not."): What happens / Who stays on the board (+ "Show me who",
listing rescue code, name, how badly hurt, how long they have been waiting,
battery) / Also staying. "Nothing is deleted" dropped from this dialog —
it reassured about something nobody was worried about. Sentence case, no
shouted NOT. Two contrast bugs found while looking at it in a browser: the
dialog title and the typed-confirmation box inherited dark-mode colours and
looked disabled. Fixed.

### #276 — the check-in request never arrived, and re-checks never did either
Paul pressed "Ask them to check in" on his own phone. The dashboard said
sent. Nothing arrived, not even in Notification Centre.

ROOT CAUSE, in the HEADERS, not the payload:
  * `apns-expiration: 0` — "attempt delivery once, never store it". A phone
    that is not instantly reachable loses the notification entirely and we
    still get a 200.
  * `apns-priority: 5` — invites Apple to delay it. Delay plus "do not
    store" is how a push disappears silently.
Both fixed: priority 10, and Apple keeps the question for 30 minutes.

Paul asked whether the same had been happening to re-checks all along. It
had. Every re-check went out with `apns-expiration: 0`, which means historic
"no answer to the previous re-check" rows may be false negatives about
trapped people. Re-checks now get 10 minutes of store-and-forward — long
enough to survive a tunnel, short enough that it can never arrive after the
next re-check in a 15-minute ladder.

Interruption level raised from `active` to `time-sensitive`. Not critical —
Paul's rule stands and the physical silent switch still wins — but an
`active` notification is swallowed whole by any Focus mode, and a swallowed
question manufactures false silence: the operator sees "asked, no answer"
and sends help towards someone who is fine, while the genuinely unreachable
look identical. Same entitlement the re-check ladder already relies on.

TWO DIFFERENT FACTS, NOW SAID DIFFERENTLY. A 200 from Apple means Apple
accepted the push; it says nothing about what the phone showed. So:
  * The app now posts POST /api/push/receipt when it sees one of our
    questions — on arrival, on tap, on a quiet `content-available` wake, and
    for anything still sitting in Notification Centre at next launch.
  * New record state `no_answer`, label "Got our question, no answer" — the
    worrying one. The phone confirmed our question arrived and nobody
    answered.
  * `phone_went_dark` now says in words: "Their phone never confirmed our
    question arrived, so we cannot tell whether they saw it."
  * `ask_state.history_words` says which, and `ask_state.delivery` carries
    what Apple actually returned (status, reason, apns-id, accepted_at,
    confirmed_at) — Paul: "Tell me the real delivery response, not that our
    code called the send function."
  * Counted separately everywhere: /api/devices counts, the count notes, the
    CSV and both PDFs. A confirmed-but-unanswered silence is excluded from
    the mass-dark test — that phone plainly had a network.

### #277 — the alert-live banner
Was: "Alert live — idle sign-out suspended … 72h window closes."
Now: "An alert is running. You will stay signed in while it is. This lasts
until you call the alert off, or 3 days pass." with "Running for 2 hours 14
minutes." on its own line. Written by the backend, so it took effect on
production without a dashboard redeploy. The warning triangle is gone — it
put an alarm on good news.

### Tests
- New `tests/test_delivery_truth_276.py` (13) and the testing agent's
  `tests/test_review_276_277.py` (17). 42/42 green in that round.
- `test_export_hardening.py` public-summary key set updated for `no_answer`.

## 2026-08-21 (late) — #278, #279, after Paul's v1.0.42 production round

Verified closed by Paul on production: the check-in request arrives (the
delivery-header fix worked), the full loop works end to end (asked from the
dashboard, arrived on the phone, answered "I need help", landed on the board
as immediate with the SOS badge and a running clock — the specific guarantee
he asked for), the card tells the two facts apart correctly, and "Mark as
rescued" records the operator, the note and Malta time with its offset.

### #279 — Focus can still silence a check-in question. Recorded, not "fixed".
Paul's phone put the check-in request in a silent collapsed group under
"While in Personal Focus". He will NOT trade the critical entitlement for
this, and neither will we. Two consequences, both now shipped:
  1. People are TOLD at setup. A new bullet on the onboarding permission
     screen ("Check-in questions can be silenced"), the footnote points at
     Time Sensitive Notifications, and Settings › Notifications carries a
     panel that says it in three short paragraphs with an "Open my phone
     settings" button. Paul: "Better they decide knowingly than discover it
     in an earthquake."
  2. The operator wording stays exactly as it is. "Their phone has not
     confirmed our question arrived" is the only truthful thing we can say
     and it is what stops a coordinator drawing the wrong conclusion. It is
     not to be "improved" into something more confident.

### #278 — two wording faults in the history
  * "Answered: SAME — no change" → "Answered — no change since last time".
    Also worse / much worse / better, all sentence case, and any unmapped
    answer now reads as words rather than being upper-cased.
  * "TRAPPED — IMMEDIATE" → "Trapped — immediate"; the yellow and green
    labels are "Trapped — serious" and "Trapped — minor".
  Left alone deliberately: the triage GROUP headers and severity badges
  (IMMEDIATE / SERIOUS / MINOR). Those are triage category names, not
  emphasis, and they are what a rescuer reads across a room. Flagged to Paul
  in case he wants them changed too.

### Capitals: the standing exception (agreed with Paul, 2026-08-21)

The rule is still "no capitals for emphasis". These are NOT emphasis and are
NOT to be tidied away by a future wording sweep:

  * The triage category names — IMMEDIATE, SERIOUS, MINOR — on the group
    headings, the severity badges and the map key. They are recognised
    triage signals a rescuer reads across a room, not a raised voice.
  * DROP. COVER. HOLD ON. on the alert screen, for the same reason.

Paul: "Standard triage category names, the same exception as DROP COVER HOLD
ON: a recognised signal, not emphasis."

Everything else stays sentence case. If a sweep is tempted to change one of
the above, the answer is no — read this line instead.

## 2026-08-22 — #280 to #287, after Paul's setup/settings/home review (v1.0.43)

### #280 — the app contradicted itself about whether alerts were on (CAUSE)
Red banner: "Critical Alerts turned OFF". Directly beneath, green panel:
"Alerts for dangerous earthquakes are always on and cannot be switched off."
Paul: "Both cannot be true, and the green one is the dangerous falsehood."

THREE places each decided this for themselves: the banner read the live iOS
permission, the green panel was a hard-coded sentence, and the tremor-preset
helper text was a third hard-coded sentence. The duplication WAS the bug.
`src/utils/readiness.ts` now owns it: one `sirenWillSound` boolean and one
`sirenSentence`, and every screen prints those. Count of places that had
asserted the false promise: **3** — settings panel, preset helper, and (a
fourth, related) the Focus panel line "An earthquake alert is different. It
always comes through."  All four now read the one source.
A first attempt at this fix still left TWO conditions (the banner asked
`!notifications || !critical`, the sentence asked the same but only
`if (Platform.OS === "ios")`), and the live check caught the reassurance
still printing under the warning. That is why `sirenWillSound` exists as a
single computed boolean rather than a condition repeated per consumer.

### #281 — a warning nobody would ever find
"Someone who declined the permission at setup will never go to the
Notifications screen. They will believe they are protected and they are not."
`src/components/ReadinessBanner.tsx` is now the first thing on the home
screen, above the rescue code, with NO dismiss control. It covers every
state where the app cannot do its job, which is the wider rule Paul asked
for: siren off, notifications refused entirely, location refused, and no
successful contact with us for 12 hours. It never prints a reassurance — no
problems found renders nothing, because "we found no problem" is not "you
are protected".

### #282 — the home layout, third recurrence (#209, #253, now this) — CAUSE
The footer was `position: absolute` OVER the scroll area and the scroll area
reserved room for it: first a magic number (#209), then a measured height
(#253). Both are guesses about a box whose height depends on the system text
size, and a guess one line short puts a red button on top of "Hold on".
THE RULE NOW: a footer or header over scrollable text must be a flex
sibling, never absolutely positioned. Absolute positioning is for decoration
(glows, rings, gradients) and for overlays on a map canvas only.
Screens checked: **10** (index, alert, onboarding, map, recheck, diag,
quake/[unid], settings/notifications, settings/places, +html).
Screens affected: **2** — index.tsx and onboarding.tsx. Both fixed by
deleting the reserve, not by tuning it. The hero also had `height: 340`
which clipped the rescue code under the status bar at large text; now
`minHeight`. Verified live at 390x844 and 320x568, top/middle/bottom.

### #284 — early-warning wording sweep
"Hear it before it happens" → "Hear what a real alert sounds like".
Places found and changed: **3** — that title, and the two tremor-preset
lines that read as future tense ("Only what I'd likely feel" → "Only shakes
I would have felt"; "tremors you will not feel" → "tremors nobody felt…
already happened"). The dashboard, both PDFs and the map copy were swept and
carried none.

### #285 — setup split, one screen one decision
Step 1 "Let the siren sound": how rare it is (Paul's words, verbatim on the
rarity of Malta earthquakes), the siren and tremor notices are separate
things and the notices can be switched off without touching the siren, and a
heads-up that iPhone will ask twice and why. Step 2: the Apple Watch check,
on its own. Step 3: the rehearsal.

### #286 — the Apple Watch question. TECHNICAL ANSWER FIRST.
Paul asked whether a paired Watch is detectable. It is not, from this app:
`WCSession.isPaired` exists, but WCSession requires a companion watchOS
target in the same bundle, and Quake Angel has no Watch app — every RN/Expo
bridge (react-native-watch-connectivity, expo-watch-connectivity) depends on
that target existing. There is no other public iOS API and no entitlement.
Detection would mean building and shipping an actual Apple Watch app.
So ROUTE B, as he specified: "I don't have one" is a 90-DAY SNOOZE, never
permanent; we re-ask after any major iOS version change (that is when the
mirroring toggle resets anyway); the reminder stays findable; and after the
practice siren we ask "Where did the siren come from? My phone / My watch" —
if they answer My watch, the app has discovered the problem itself, says so,
and puts the reminder back whatever they answered before.
All of it lives in `src/utils/watchReminder.ts`; home, settings and
onboarding read it rather than each keeping their own copy of the rule.

### #287 — "no tremor notices since yesterday" — WHAT THE LOGS ACTUALLY SAY
Not the world being quiet, and not the feed. Production poller healthy, both
providers, last poll seconds before I looked. Preview mode is on for his two
devices with the 5,000 km test radius valid to 25 Aug. In the last 17 hours
**8 tremor notices were sent to his phone**, most recently 11:13 Malta the
same morning (M2.9, 2,805 km). The other 190 candidates were skipped for one
honest reason: `beyond_country_radius`, 9,800–12,000 km away (Washington,
Sulawesi, Nicaragua, Flores). So they were sent and Apple accepted them.
Two reasons he may not have seen them, one of which was ours: they went out
with `apns-expiration: 0` — attempt once, never store — the same fault as
#276, now 20 minutes; and at `interruption-level: active` a Focus mode
silences them into a collapsed group (#279, accepted).

### #283 — plain language and capitals
Removed: **40** `letterSpacing` declarations and every
`textTransform: "uppercase"` in the app (0 remain in frontend/app and
frontend/src). Sentence case on every button Paul listed plus the ones the
sweep found: Marked safe, Sending…, Save, Skip, Remove my name, Practise the
alert, Allow the siren, Play the practice siren, I have checked this, and the
lock-screen re-check buttons (No change / Worse / Much worse / Better — "SAME"
was code-speak in the same way he flagged in the history).
Jargon replaced: "bypass the ringer switch and Focus/DND" → "The siren sounds
even if your phone is on silent or set to Do Not Disturb"; "Uses Apple's push
infrastructure for lowest latency" → "Arrives straight away, by the fastest
route Apple provides"; "Same sound file used for a genuine earthquake alert"
→ "The same siren you would hear in a real earthquake"; "DASHBOARD SHOWS" →
"If you ask for help, this is what appears".
Also caught and fixed while in there: the trapped confirmation said
"Rescuers alerted", which breaks the standing rule about implying anyone is
watching the board. It now says "Your report has been sent."
Kept in capitals, per the agreed exception: DROP. COVER. HOLD ON. and the
triage category names.

### #7 — what happens when someone declines (answered plainly)
Before: `registerForPushNotifications()` wrote a "permission_denied" note to
storage and returned. Nothing on the home screen. The only warning was
inside a settings screen they had no reason to open, and it only covered
Critical Alerts being revoked later — not a plain refusal at setup.
Now: the home banner says "Your phone will not sound the siren. Tap to fix
this." permanently, tapping asks again if iOS still allows it and otherwise
opens Settings, and the panel states in one line what to tap when it gets
there — "Tap Notifications, then turn on Allow Notifications and Critical
Alerts. iPhone does not let an app open that page directly." That last
sentence is there because it is true: iOS has no public deep link to an
app's notification settings page, so we say so rather than leaving someone
hunting.

### Verified live (rule 4a), not from source
Home at 390x844 and 320x568 at top/middle/bottom; the three setup steps;
the settings screen with BOTH boxes agreeing (checked with a temporary probe
that forced the siren-off state on web, then reverted); the practice run
stopping on the wrist question with the watch warning and a working Back to
home; the red alert screen with readings and DROP. COVER. HOLD ON.; the calm
check-in screen; and a real "I need help" report reaching /api/devices.
Testing agent rounds 43 and 44: two regressions found in 43 (both mine —
the caps exception and an auto-redirect that made the wrist question
unreachable), both fixed and confirmed in 44. 32/32 backend tests pass.

---

## The board batch — #295, #194, #296, #297, #298 (23 Aug 2026)

Paul's testing report on v1.0.44, fresh install, production. Part C came
with the strongest wording in the project so far: "This is the most
important item in the batch." Numbers assigned in that report:
#288–#302, plus #303/#304 logged and deliberately not built, and #305
added later the same day.

### #295 — the board did not update until manually refreshed

**Diagnosed before anything was changed, and the first answer was "not
what you expected".** The live dashboard was fetched and compared byte for
byte with the copy in this repo: identical. That file *does* poll every 4
seconds, and its change-detector *does* include status and injury
severity. So "the refresh broke" was false.

What was actually wrong: the poll had no way of telling anyone it had
stopped. Five drawing jobs — the map among them — ran ahead of the
casualty list inside one try/catch. Any one of them throwing (a person who
declined location, for instance) aborted the cycle into a console nobody
has open, and the list froze while the page looked perfectly healthy. A
browser tab frozen in the background produced the identical silence.

Fixed at the cause: the casualty list is drawn first and alone, every job
is isolated with its own plain-English failure line, and the board now
carries a clock that only a real successful read can move.

### #194 — a stale board must admit it

Sticky strip: "Live — updated 3 seconds ago, at 20:14:32 Malta time."
Nothing but a successful read of `/api/devices` moves that clock. After 15
seconds without one: a flashing red bar across the top saying what it is
showing instead and how old it is, with a sound. Returning to a tab the
operating system froze prints the length of the gap rather than quietly
catching up.

### #296 — the annunciator, to ISA-18.1

Paul: "Do not invent this. There is a defined sequence used in control
rooms for decades." Built to it, server-side (`backend/board_alarms.py`)
so two operators see one picture and an acknowledgement survives a reload.

- Alarms: a new person asking for help · an existing person getting worse
  · a person who asked for help going quiet.
- Information, which never flashes or sounds: safe reports, battery
  changes, list housekeeping, registrations, tremor notices.
- Every alarm names the action, not the state change — and for a walking
  wounded it says "Not a rescue task", because a team must never be sent.
- Grouping: same kind, same minute, one line and one sound. Past ten
  alarms in ten minutes the board says out loud that it is summarising.
- Shape + word + colour, so it reads with the sound off and in black and
  white. Mute is five minutes with a visible countdown, and it cannot
  touch the flashing or the count.
- Acknowledging stops the noise, records who and when (readable in
  `/api/audit` as `alarm_acknowledged`), and clears nothing. Only a
  rescue, a deliberate move off the board, or a quiet phone speaking
  again resolves an alarm. Nothing auto-clears or times out.
- One guard added deliberately: a record that has been quiet for more than
  24 hours does not sound. It stays on the board with its card saying it is
  quiet. A wall of week-old silence on the first load of the day would
  train an operator to ignore the strip, which is the failure the flood
  rules exist to prevent.

### #297 — walking wounded off the working board

MINOR who can move themselves have their own labelled list; their count
stays on the board whether that list is open or shut. A MINOR who cannot
get out, or cannot move, stays on the working board — severity is medical,
being stuck is structural, and the structural fact wins. Getting worse
puts them straight back with an alarm; going quiet does the same. Rescued
pins are off the map by default and small, grey and unlabelled when shown.
A permanent chip names everything currently out of sight, with one button
that shows all of it, and filters reset when a new alert starts.

Note for the record: there is no team-assignment feature in the product
yet, so "what happens to someone already assigned to a team" cannot arise
today. The rule agreed with Paul is written into the code comments for
when it does: they do not move on their own, because moving them would
leave a team walking towards something no longer on the board.

### #298 — the false promise

"It does come through Do Not Disturb, so they will see it" is gone. The
dialog now states only facts: who it goes to by name *and* code, the exact
words, that it will not siren, that a Focus mode can hide it completely so
no answer proves nothing either way, what it costs their battery, and that
the operator's name and the time go on the record.

Sweep for the same fault: 8 places found. 2 on the dashboard, fixed here.
6 in the phone app (setup's "Delivered instantly", "Arrives straight
away", "so you don't miss the alert", "Alerts always come through", and
two "always comes through" lines in settings) — fixed in the phone batch,
because they need a build either way.

### Deferred on purpose

- **#303** (Paul called it #279): alert only phones last known inside an
  area the operator draws. Needs the drawing tool first. After the pilot.
- **#304** (Paul called it #278): ask everyone in a damaged area to check
  in, and surface clustered silence — as a question, never a conclusion,
  because it is also what a mast failure looks like. After the pilot.
- **#305**: tremor notices on by default for new installs, with a
  one-time "want fewer?" question after the first week. In the phone batch.

### #307 — the test suite tells the truth again

143 of 1,001 backend tests were failing. None of them were failing because
the product was broken; every one was a test still asserting a world that
had been deliberately changed. Left alone, that is worse than having no
tests, because a real regression hides in the noise.

What was wrong, and what was done:

- **Seven whole files tested endpoints that no longer exist.** The
  `/api/debug/*` family (devices, test-push, probe-push, last-push-events,
  recipients-sample, full-recipient-list, register-push-capture) was removed
  in an earlier cleanup. ~100 failures. Deleted; the "these must stay 404"
  guards live on in `test_critical_alerts.py` and `test_purge_browser.py`.
- **Tests seeded phones through `/api/register-push`.** Since #266 that
  endpoint only files a device row when the push provider ACCEPTS the
  registration, and this environment's key is a placeholder, so every
  registration is refused *by design*. Those tests now insert rows straight
  into Mongo and separately assert the #266 contract (502, no row).
- **Tests sent alerts without the confirmation phrase** (#245) and without
  calling the alert off afterwards. A live incident legitimately holds
  records on the working board, so a stray trigger made unrelated board
  tests fail — including on the NEXT run, since the state is in Mongo.
  Every test that sends a real alert now stands it down (`stand_down_after`).
- **Motor pins itself to one event loop for the life of the process.**
  Starlette's TestClient makes a new loop per request and `asyncio.run()`
  makes its own, so tests passed alone and failed together. `conftest.py`
  now gives the whole session ONE loop (and a `run_async` fixture for tests
  that call server internals directly).
- **The per-IP registration rate limit is real** and 20/hour is generous for
  a phone but not for a test suite. `clear_register_rate_limit` empties the
  bucket for the handful of tests that register over HTTP.
- **Stale assertions**: app version pinned to 1.0.8; the critical-alert
  payload and `send_push` moved to `apns.py` / `push_relay.py`; the legacy
  `client_name` POST /api/status is now the device check-in payload; #277
  reworded incident status away from "stand-down"; two in-memory fake Mongo
  matchers didn't understand `$ne`, so their devices silently disappeared.
- **Two genuinely brittle tests**, fixed at the cause: the audit CSV returns
  the newest 500 rows in the window, and a suite run writes several hundred
  events, so seeded rows fell off the end of a six-hour window (window
  narrowed to ten minutes); and the B1/B2 table-vs-narrative comparison
  compares CURRENT state against WINDOW state, which is only meaningful over
  a window wide enough to contain the events behind that state (29 days).
- **The "no B1/B2 jargon" guard** was matching the "B1" inside randomly
  generated rescue short codes like FB1FC. Now a word-boundary match.
- Nobody keeps a copy of the admin password any more: `conftest.py` loads
  `backend/.env` once and eight files read it from the environment.

Result: 910 passed, 7 skipped (all env-gated), 0 failed — stable across
three consecutive full runs. No product code was changed.

## 2026-08-24 — Paul's live test day. Numbering note first.

Paul's numbers are authoritative from here. The board/phone batch of
2026-08-23 used #288–#306 for different items; those are now suffixed
`-agent` in this document (#296-agent = the ISA-18.1 alarms, #297-agent =
walking wounded off the working board, #291-agent = name prompt on Home,
#289-agent = location permission step, #290-agent = Back to Home,
#293-agent = watch wording, #294-agent = no iOS badge, #295-agent =
stale-board warning). Paul's #281–#297 refer to today's list.

Closed today on Paul's own live evidence: #277 (board updates without a
refresh), #194 (board says out loud when it loses the backend), #129
(portrait exports), #131 (solid watermark).

### #296 — the alert was called off and the phone kept asking "Are you safe?"

Paul triggered test alerts, called them off, and then received at least
four CRITICAL "Are you safe?" notifications about a minute apart on one
phone.

Cause, found and confirmed in the code: those reminders are LOCAL
notifications, scheduled on the device by `scheduleCheckInReminders()` —
eight of them, 90 seconds apart, over 11½ minutes. Nothing server-side
could reach them except one specific silent push, which was wired only to
a separate operator button ("stop the repeating reminders"). Calling the
alert off sent a different push, which cleared the check-in SCREEN and
left the ladder running. An operator standing down a false alarm has no
reason to know they must also press a second button — so the product was
relying on knowledge nobody had.

Fixed in three places, deliberately overlapping:

1. **The stand-down cancels them itself** (`server.py`). Same action, same
   set of phones as the stand-down (#274 — a person still asking for help
   keeps their screen and their reminders), and it now reports
   `reminders_cancelled` in the response and in the `push_events` record.
   This lands on phones ALREADY IN THE FIELD, including v1.0.44 — the app
   has handled the cancel push since batch 5.
2. **The phone stops itself** (`_layout.tsx`). A stand-down arriving by any
   of the three paths — background task, foreground receipt, or the user
   tapping it — now cancels the ladder too. A dropped cancel push must not
   mean eleven more minutes of sirens.
3. **The operator is told** (dashboard call-off toast): "The repeating
   'Are you safe?' reminders on N phones were stopped too."

### #207 — a reminder must not look identical to the alert

Same ladder, and the real cause of what Paul saw. The backend re-checks
were already fixed in batch 7 (time-sensitive, escalating to critical
exactly once), but the phone's own reminder ladder was hardcoded
`interruptionLevel: "critical"` for all eight.

Now: the FIRST reminder is still a Critical Alert — someone may have slept
through the alert itself, and that is exactly the case the entitlement is
for. Reminders two to eight go out `time-sensitive`, which still breaks
through Focus and Do Not Disturb but respects the silent switch and the
volume the user chose. Eight full-volume Critical Alerts in a row drains
the battery, invites Apple to look at the entitlement, and teaches people
that a Critical Alert from this app can be ignored — which would cost us
the one alarm that must never be ignored.

### #283 — three screens, three different sums, all of them wrong

Paul found the same defect in three places on one day: the call-off toast
("13 phones", then "14", when it was one real person and thirteen test
entries), the sentence under the stat boxes (never matching the box next
to it), and the team PDF ("Not responding: 1" above a breakdown reading
0 + 1 + 6).

One cause: four surfaces each doing their own arithmetic.

- `people_counts._tally()` is now the ONLY thing that produces counts,
  and it is called with the population you want rather than re-derived.
  `/api/devices` returns BOTH sets — with test entries and without — so
  the board picks one instead of recalculating.
- The dashboard's `computeCounts()` is deleted. `pillsFromServerCounts()`
  maps names to boxes and does no arithmetic; a test fails if `+= 1` or
  `forEach` reappears in it. Walking wounded is counted server-side too.
- The three silence sentences moved into `counts_notes()` (one wording,
  used by the board, the PDF and the CSV) and now say what they are:
  "Gone quiet: 7 of the 44 people on the board... already counted above,
  spread across Safe, Trapped and Not responding. They are not extra
  people." The old line claimed all three were "counted in the numbers
  above", which read as a breakdown of the box beside it.
- The B1 heading names the total and denies being extra, so 0 + 1 + 6 can
  no longer look like a contradiction of the line above it.
- The call-off reports `cleared_real_count` / `cleared_test_count` and
  `kept_on_board_real_count` / `kept_on_board_test_count` — the same
  split the confirm dialog was already making correctly.

### #291 — never print a fact we do not have

"Phone went dark" appeared on two RESCUED people's map cards, in a live
alarm, in the team PDF and in the CSV. The state was honest (we asked,
nobody answered, the phone never confirmed our question arrived); the
LABEL asserted the phone had died, which is the exact claim #271 said we
must not make.

- `LABELS[DARK]` is now "We asked, no answer".
- A BROADCAST alert is not the same act as asking one person. When the
  only thing that has asked is a broadcast, the label reads "Not asked
  since the alert at 12:04" and the detail ends "Ask them to check in to
  find out". This is why rescued people were reading as dark: a test
  alert counted as asking them.
- The STATE is unchanged in every case, so no count moved and no alarm
  went quiet. This was about the words.
- CSV row renamed to `asked_no_answer_delivery_not_confirmed`; the PDF
  row and the spoken note match.

### #289 / #290 — the report that never reached the board

A real MINOR report did not appear anywhere. Cause: NOTHING was sent
until the follow-up question was answered, and both follow-up sheets can
be left — Back, a system gesture, or just putting the phone down. Both
sheets even said "This does not delay your report" while being the thing
delaying it.

- The report now goes on the SEVERITY tap. The follow-up answer updates
  it (`isFollowUp`, which is also what lets someone escalate to IMMEDIATE
  after a first send). The subtitles now say "Your report is already sent
  — this adds one detail to it", which is true.
- `egress: "not_answered"` is a real answer, and it is NOT walking
  wounded. Being filed as the lowest rescue priority on an assumption is
  how somebody gets left. They stay on the working board, in a group
  whose heading says "cannot get out or we have not been told". The board
  list and the count use the same rule, and a test fails if they drift.
- On #51: the mobility question IS skipped for green. What Paul saw was
  the EGRESS question, which is deliberate — mobility describes the body,
  egress describes the building, and only egress decides whether a team
  with cutting gear is needed. It stays, but it can no longer cost
  somebody their whole report.
- Alarms now carry `since_report`: "Since this alarm: reported MINOR, can
  get out at 14:32. This alarm still needs your decision." The alarm does
  NOT clear — a self-reported improvement must never quietly close a
  report of a serious injury — but the board no longer contradicts
  itself.

### #297 — "Authority: Emergency test name" on a real public report

Not an unfilled template. Somebody typed it into the dashboard's
Authority name field during a test and every export since repeated it.
Two layers: the settings endpoint now REFUSES a test-looking name in
plain words, and the report renderer FALLS BACK to "the responsible
authorities" if such a name is already saved — which is the only thing
that protects a value already sitting in a live database. Matched on word
boundaries, so "Attest Rescue" is accepted: refusing a real agency would
be its own kind of wrong.

---

## 2026-08-24 (second half) — live test batch: #285, #286, #135, #207

### #285 — "The alarm is too quiet, and if it is muted nothing on screen is unmistakable"

Two separate failures, fixed separately:
- **Sound.** The old alarm was one sine wave at 0.14 gain — a polite
  notification chime. It is now a square wave (far more energy in the band
  small laptop and tablet speakers actually reproduce) plus a harmonic,
  routed through a limiter at 0.95 output so stacked oscillators get loud
  rather than distorted. Rising two-tone, repeated three times, ~1.1 s.
- **Cadence.** Repeats every 3 s (was 10 s) **until somebody
  acknowledges**, and it now runs on its own 1-second timer rather than on
  the 4-second network poll: a slow or failed poll used to stop the alarm
  silently. The alarm must be the last thing to stop working, not the first.
- **Visual.** A new full-window overlay (`.qg-alarm-visual`): a flashing
  12 px red frame around the entire window plus a flashing word at the
  bottom centre — "47 alarms — nobody has acknowledged them". Driven ONLY
  by the unacknowledged count, so silencing the sound cannot hide it.
  `pointer-events: none`, so clicks pass straight through to the board (the
  #265 lesson: never cover a control). Bottom-centre, not top, so it cannot
  cover the sign-in banner. `prefers-reduced-motion` keeps the frame and the
  word, solid instead of flashing.

### #286 — bulk acknowledge

`POST /api/admin/alarms/ack {"all": true}` acknowledges every open,
unacknowledged alarm. The annunciator shows a row above the individual
alarms: the breakdown by word ("45 IMMEDIATE · 2 GOT WORSE") and one
button, "Acknowledge all 47". Individual rows and their own buttons stay
exactly as they were.
- Deliberately NOT behind a confirmation dialog. The operator is looking at
  a flood; a second click is friction at the worst possible moment. It is
  safe because acknowledging removes nothing: every alarm stays on the
  board and every row records who acknowledged it and when, so a bulk
  acknowledgement reads back in an inquiry exactly like 47 individual ones.
- The confirmation message says so out loud: "…acknowledging stops the
  sound, it does not mean anyone has been helped."
- An empty payload is still a 400. `all` must be explicit — an accidental
  whole-board acknowledgement would be an expensive typo.
- 7 tests: `tests/test_bulk_ack_286.py`.

### #207 — automated re-checks are never Critical Alerts (closed)

The previous cut escalated a re-check to `interruption-level: critical`
once per person per incident after three unanswered asks. Paul's ruling on
2026-08-24: remove it entirely, `critical` belongs to the first real alert
and nothing else. Every automated re-check now goes out at
`time-sensitive` — which still breaches Focus and Do Not Disturb, what a
trapped person needs — and no combination of arguments can make
`_build_recheck_payload` emit `aps.sound.critical`.
- `escalate` survives as a RECORD, not a loudness switch: the sweeper still
  computes it and the payload still carries `escalated: true` for the
  diagnostics panel and the audit trail. `escalated_to_critical` is kept
  and is always `false` — an explicit answer beats a missing key.
- Rewritten `tests/test_recheck_escalation.py` (7 tests) locks it, including
  a test that the earthquake alert itself KEEPS `critical`.

### #135 — signed out in the middle of a task

Root cause was never the idle timeout (that is 2 hours, and it already
suspends itself during an active incident). It was the token: a 15-minute
life with **no renewal path**. The first admin call made after 15 minutes
came back unauthenticated, the dashboard dropped the session, and an
operator mid-task was signed out by nothing more than a clock.
- **Longer life:** `JWT_TTL_MINUTES` 15 → 60 (env-overridable), so a laptop
  that slept through a renewal window still wakes to a working session.
- **Renewal:** new `POST /api/auth/refresh`. The dashboard renews 5 minutes
  before expiry, on tab focus/visibility if the token is close to expiring,
  and once automatically when any call comes back 401 (then retries the
  call). Single-flight, so a dozen polling panels cannot cause a renewal
  storm.
- **Renewal is not a way around revocation.** It performs every check a
  normal admin request performs: allowlist, disabled, session_version,
  account expiry. A disabled account, a bumped session_version or an
  expired account is refused. An already-expired token cannot be renewed —
  renewal keeps an active session alive, it does not resurrect a dead one.
  The legacy shared token has no session and is refused.
- **Absolute cap:** new `auth_iat` claim records when the human actually
  signed in with Google and is carried unchanged through every renewal.
  Renewal can never push expiry past `auth_iat + JWT_ABSOLUTE_SESSION_HOURS`
  (12 h — one long incident shift). A tab left open on a shared workstation
  still needs a fresh Google sign-in once per shift.
- **Loud sign-out:** when a session really does end, the existing
  signed-out banner gains `.qa-so-loud` — flashing red, `role="alert"`,
  scrolls itself into view, and plays the fault sound once (never on every
  re-render). It stays in normal document flow: it must never cover the
  Google button, which is exactly what #265 was.
- 9 tests: `tests/test_session_refresh_135.py`.

Full suite after this batch: **1002 passed, 7 skipped, 0 failed.**

---

## 2026-08-25 — live test batch #2: #285, #286, #301, #290, #283, #297, #299, #298, #300, #51

### #285 — the alarm sound never played (the loudness was never the problem)

Measured through a browser audio probe, the new alarm comes out at 0.81 of
maximum — near full output. What was broken was ARMING:
- A browser refuses to make any sound until the person has interacted with
  the page, and Safari (and a backgrounded tab) SUSPENDS the audio channel
  again afterwards.
- The old code created the channel on the first alarm — with no
  interaction behind it, so it started suspended — asked it to resume
  without waiting for an answer, DROPPED that alarm, and left a one-line
  note at the bottom of a panel as the only sign the board had gone deaf.
  Paul saw exactly that note, which is what confirmed the diagnosis.

Four fixes, one per hole:
1. **Arm on any interaction** — pointerdown, click, keydown, touchstart —
   not clicks alone (an operator on a tablet or working from the keyboard
   never armed it at all), and wait for the resume to actually succeed.
2. **Keep the channel alive** — an inaudible 1 Hz oscillator at 0.0001 gain
   runs for as long as the board is open, so the browser cannot suspend it
   again the moment the operator looks at their phone.
3. **Never drop the first alarm** — a sound asked for while the channel is
   waking up is remembered and played the instant it wakes.
4. **Say so, loudly** — a permanent control in the alarm panel: "🔇 ALARM
   SOUND IS OFF … Switch the sound on", or "🔔 Alarm sound is ON" with a
   **Test the sound** button, so an operator proves the alarm works at the
   start of a shift instead of discovering it during an incident.
   `qgSound.armed()`, `.arm()` and `.test()` are the API.

### #301 — test people now behave like real reports (only when asked)

Paul: "the 33 test people appeared as markers but didn't change any stat
box, didn't appear in the alarm panel, aren't even clickable." Alarms
returned early for anything test-flagged, and the seeder wrote rows
straight into the board so nothing ever went through the alarm decision.

His ruling: identical everywhere — counts, list, map, alarms — but only
while "Show test entries" is ticked, always labelled.
- `board_alarms` no longer skips test devices; every alarm row carries
  `is_test`, and the silence sweep includes them.
- `GET /api/admin/alarms?include_test=1` mirrors the tick. The dashboard
  passes it and re-reads the alarms the moment the tick changes, so the
  board and the alarm panel can never disagree about which population is
  on show, even for four seconds.
- Seeding raises alarms (12 of the 33 are trapped); clearing RESOLVES them
  (never deletes — the ledger still reads back honestly).
- Acknowledge-all only touches what the operator can see (`include_test`
  in the payload), so hidden test alarms are never silenced behind their
  back and a visible rehearsal is not left half-acknowledged.
- Every test row is labelled `TEST` in the panel, and the panel carries a
  banner: "TEST PEOPLE ARE INCLUDED in these alarms and in the numbers…".

### #290 — a worse report re-opens an acknowledged alarm

An acknowledgement means "I have seen THIS fact and I am dealing with it".
Getting worse is a new fact, so the acknowledgement no longer covers it.
`raise_alarm(..., re_raise=True)` UN-acknowledges the existing alarm rather
than creating a second one (no duplicate rows on the strip), records
`re_raised_at`, `re_raise_count`, `previous_ack_by/at`, and puts the row
back in the unacknowledged count with sound and flashing.

Paul's boundary (his choice of three options): **only worse re-alarms.** A
same-or-better report updates the yellow note and nothing else — otherwise
every routine check-in from somebody already being helped would sound the
alarm again, which is how a room learns to ignore it. Needing help again
after reporting safe counts as worse.

### #298 — every alarm explains itself

Paul: "if I was an operator, I have no idea what all that was about." Each
alarm now carries a `story`: what was reported when it was raised, what the
person has said since, who acknowledged it, and whether a worse report
re-opened it — rendered as a "What happened" expander. Built from fields
already on the alarm row, so fifty alarms cost exactly what they did
before (no extra queries).

### #283 — the breakdown now names the categories it counts

"…spread across Safe, Trapped and Not responding" was written from memory
and left out Rescued, so it added to 2 of 7. `Counts.quiet_by_status` is
now computed from the same rows as every other figure, and the sentence
lists what is actually there: "spread across 5 Rescued, 1 Needing help and
1 Safe."

Paul's separate question — should rescued people be in "gone quiet" at
all? They stay: removing them is how a total stops adding up, which is the
original complaint. Instead the page says out loud: "— 5 of them have
already been rescued, so their silence is not a worry. Those are listed
here only so the numbers add up."

### #299 — a stand-down is no longer reported as a failed trigger

`/api/audit` read EVERY row in `push_events` and stamped `TRIGGER` on all
of them. A stand-down has no magnitude and no recipients, so it rendered as
"⚠️ TRIGGER FAILED · M? · 0 people" — the feed was inventing a failure that
never happened. Trigger rows are now selected explicitly (`kind: trigger`,
or missing for rows written before the field existed) and stand-downs are
returned as their own `stand_down` kind, rendered "🔇 ALERT CALLED OFF ·
false alarm · N phones told · by <who>".

### #297 — grammar

"before {authority} has completed formal notification" → "before
{authority} completes formal notification", which is right whether
`authority` is one body or several.

### #51 — the egress question no longer reads as the mobility question

Green does not get the mobility question — it gets the egress question, and
its title was "Can you get out on your own?", word-for-word what the
mobility question sounds like. Retitled "Is your way out blocked?", answers
"No — I can get out" / "Yes — I'm blocked in".

### #300 — the radius override box

Could not be reproduced here (it renders 5000 for me), so the fix removes
every plausible cause rather than guessing: the value is written as a DOM
property after the box exists (a `value` attribute a browser considers
invalid is silently not displayed), `step` relaxed from 50 to 1, a
placeholder added, and Save falls back to the known active value if the box
is empty rather than telling the operator off.

### #291 — not reproducible from here

Those four rescued records live in production data this environment cannot
see. Most likely cause: the map hides rescued people by default ("Showing
less than everything: rescued people are off the map" with a "Show me
everything" button). Paul to confirm with that filter on.

Full suite after this batch: **1021 passed, 7 skipped, 0 failed.**

---

## 2026-08-25 (production) — #302: dead alarm-panel buttons, and a flashing leak

Paul, testing production:
> None of the alarm-panel buttons respond to clicks — not Acknowledge, Test
> the sound, What happened, or Silence the sound. Pointer cursor on hover,
> clicking does nothing, no console error. "Call alert off" does work.
> New finding: the flashing red border is still visible even after signing
> out completely, to an unauthorised visitor.

Both faults were mine, both from the batch he was testing. Both reproduced
in a real browser before anything was changed.

### Fault 1 — the swallowed click (root cause, reproduced)

The audio-arming listeners added for #285 run on `pointerdown`, and their
callback re-drew the alarm panel. The button under the operator's finger
was therefore destroyed and rebuilt BETWEEN pointerdown and pointerup —
and a browser only fires `click` when both landed on the same element. So
no click was ever generated. Proof, from a real browser at the button's own
coordinates: `pointerdown: qg-annun-ack-all` … and no `click` after it.

Everything Paul saw follows from that one line:
- pointer cursor, no action, no console error (nothing threw — nothing ran);
- every panel control affected, including `<details>` "What happened",
  which needs no JavaScript at all;
- "Call alert off" unaffected — it lives in the fixed top bar, which is
  never re-rendered.

Fixed twice over, because either alone suffices and this must not return:
1. Arming notifies ONCE, and always on a later tick (`setTimeout`), never
   inside the pointer sequence.
2. The panel writes to the DOM only when the content has actually changed —
   the same rule the activity feed has followed since 2026-08-12, where the
   symptom was a lost scroll position. Here the cost was a dead Acknowledge
   button during an incident.
Plus two belts: every panel button now carries its own direct listener as
well as the delegated one, and `forgetHtml()` clears the cache before a
label is changed by hand so "Acknowledging…" can never stick.

### Fault 2 — the flashing leak (root cause, proven)

`refresh()` returns early when signed out, before it ever reaches the
alarms. The panel and the whole-window flashing were left exactly as they
were at the moment the session ended: a stale casualty list and a red alarm
flashing at somebody with no right to see it.

Paul's question — should a signed-out visitor see a flashing alarm at all?
**No.** An alarm is an instruction to act; somebody who cannot act on it
should only ever see the calm aggregate numbers. Alarms are cleared on the
auth change itself, and again on a one-second tick regardless of what the
polling does.

### The overlay, rebuilt so it cannot ever block a click (#302)

Paul asked for this to be checked first, and he was right to. The overlay
WAS `pointer-events: none` and measured innocent here — but "should be
fine" is not good enough for a control room. It is now four 12 px strips
pinned to the edges of the window plus one tag at the bottom: there is no
element of any kind over the middle of the screen, so this class of bug is
impossible whatever a browser does with `pointer-events` (still set, with
`!important`, on the container and every child). Its z-index also dropped
below the fixed top bars, so a flashing alarm can never sit over the
trigger, stand-down or sign-in controls.

### The board now checks its own buttons

On every render it hit-tests its own Acknowledge button. If anything is
covering it, the panel says so — in plain words, with the name of the
offending element. Paul had to diagnose a dead control by hand; the board
should have told him.

### And it reports what it did

The sound panel now reads "🔔 Alarm sound is ON. Last sounded at 12:04." If
that time keeps moving and the room hears nothing, the fault is the volume
or the output device; if it never moves, it is the board. A question we can
now answer instead of argue about.

Verified live, in a real browser, at real coordinates: "What happened"
opens and STAYS open, Silence the sound toggles twice in a row, Test the
sound plays and the label returns to normal, an individual Acknowledge and
Acknowledge-all both reach the API, the count falls to 0 and the flashing
stops. Signing out clears the panel and the flashing within a second.

Regression tests: `tests/test_alarm_panel_clicks_302.py` (8). Full suite:
**1029 passed, 7 skipped, 0 failed.**


## 2026-08-27 — Paul's #307 (third repeat): buttons covered again, and the warning was in code

Third live report of "Show me who and Acknowledge stop responding because
another element is on top of them" — blamed in successive rounds on
`qg-banner-text`, `qg-trigger-wrap`, and `qg-tremor-strip`. Paul, rightly:
"the buttons should never get covered in the first place, and any warning
must read in plain English, no code names."

Root cause was two-layer, and fixed on both layers.

### 1. The buttons cannot be covered any more

The alarm panel lives inside `.qg-topstrip` (sticky, top:0, inside the
sidebar). Its previous z-index was 40. Every other top-of-page overlay
had a bigger number:

- `#qg-banner` (the transient "Ready / success / error" toast) — z-index 99998.
- `#qg-stale-bar` (the "board not updating" red bar) — z-index 12000.

So on any given render, the two of them could paint on top of the panel
buttons. Two changes make that impossible now:

- **`.qg-topstrip` z-index bumped to 100000** and given `isolation:
  isolate`. It now owns its own stacking context above every top
  overlay, and its own children can't be reached over from outside it.
- **`.qg-topstrip` `top` is now `var(--qg-fixed-top-offset, 0px)`.** A
  new JS `updateFixedTopOffset()` sums the heights of the currently
  visible fixed top banners (`#qg-banner.show` and `#qg-stale-bar`) and
  writes the total to the CSS variable. `showBanner`/`hideBanner` in
  the trigger IIFE call it, and `renderHeartbeat()` calls it every time
  the stale bar toggles. So even if paint order ever failed, the
  geometry can't overlap either — the sticky strip physically slides
  down by the banner's height while the banner is on screen.

Belt and braces on purpose.

### 2. The warning reads as a sentence, not as a bug report

If the check ever DOES fire in the field, it now says:

> The alarm-panel buttons are sitting behind the tremor-notifications
> status strip, so tapping them may do nothing. Reload the page. If it
> happens again, tell the developer that the tremor-notifications
> status strip is covering the alarm buttons.

Never a class name, never an ID, never "qg-…". A new `FRIENDLY_COVER_NAMES`
dictionary maps every element the check has ever caught in a bug report
(`qg-banner`, `qg-banner-text`, `qg-trigger-wrap`, `qg-trigger-btn`,
`qg-stop-reminders-btn`, `qg-stand-down-btn`, `qg-tremor-strip`, its
inner spans, `qg-stale-bar` and its parts, modal backdrops, map
filters, and `header`) to a plain-English phrase. `friendlyDescribeCover`
walks up to three ancestors, so a click landing on an inner `<span>`
still resolves to the parent's friendly name. Anything unrecognised
becomes "something else on the page" — the raw element still goes to
`console.error` for the developer, but never to the operator's screen.

### Verified

- **Backend static-source contracts** — `tests/test_alarm_buttons_never_covered_307.py`
  (8/8 pass); pins z-index=100000, `isolation: isolate`, `top:
  var(--qg-fixed-top-offset)`, both `showBanner`/`hideBanner` call
  `updateFixedTopOffset`, `renderHeartbeat` calls it on stale-bar
  toggle, the message never interpolates raw IDs or class names, the
  translation dictionary covers every element from Paul's three
  reports, and `friendlyDescribeCover` walks ancestors.
- **Regression** — `tests/test_alarm_panel_clicks_302.py` (8/8 still
  pass), `tests/test_batch_2026_08_26.py` still green after the change.
  Total: **21 passed, 1 skipped, 0 failed** in the touched suites.

### How Paul should test cleanly (per Standing Rule B)

1. Hard refresh (Cmd/Ctrl+Shift+R) the dashboard once GitHub Pages picks
   up the push.
2. Sign in as usual.
3. Trigger a test alert against a `qgtest-<random>` device (never a real
   one — Standing Rule A). Confirm the flashing red border, the count,
   and the alarm rows appear inside the topstrip.
4. Watch a full success cycle of the banner (Ready → success → auto
   dismiss). While the banner is on screen, the sticky topstrip slides
   down by the banner's height — no overlap.
5. Click the Acknowledge button. It responds. Click "Show me who" on any
   alarm card. It expands.
6. If (for any residual reason) the on-screen "buttons covered" line
   ever appears, confirm it names the offending element in plain
   English — never a code name.

### Deploy state

`memory/dashboard_build/index.html` is staged. Push to
`PaulVincentiSafequake/SafeQuake` main (path
`backend_dashboard/public/index.html`) is pending a fresh GitHub PAT
from Paul; the prior PAT was revoked after last push.

## 2026-08-28 — Paul's #307 (fourth repeat): the fix goes generic

### What Paul reported

> "You patched three elements (`qg-banner-text`, `qg-trigger-wrap`,
> `qg-tremor-strip`) but I found a fourth one live: the top
> status/update message bar also covers the alarm buttons. Please find
> every fixed or sticky element on the page that could ever sit above
> the alarm panel and give the whole family the same fix at once, not
> one at a time."

The fourth bar was `#qg-rescue-toast` (the rescue-flow confirmation
strip that slides in from the top when a bulk-rescue action fires). It
is `position: fixed; top: 0; z-index: 99998` — same shape as
`#qg-banner`, so the same class of coverage bug came back a fourth
time.

### What changed

Stopped naming top bars one at a time. `updateFixedTopOffset` now
scans the whole page for every visible `position: fixed` element
pinned to the top of the viewport and offsets the sticky alarm panel
below the union of them. Full-viewport modal backdrops and narrow
corner toasts are excluded so they don't slam the offset. A
body-wide `MutationObserver` (watching `class`, `hidden`, `style`) plus
`transitionend` / `animationend` listeners re-run the scan on every
DOM change that could show or hide a top bar — so any future
top-of-page bar added to the dashboard is handled automatically the
moment it renders. `showToast` / `hideToast` also call
`qgUpdateFixedTopOffset` directly (belt-and-braces, same frame).

`FRIENDLY_COVER_NAMES` gains `qg-rescue-toast` so the fallback warning
(if it ever fires) reads as "the status message bar at the top of the
page".

### Verified

- **Backend static-source contracts** — `tests/test_alarm_buttons_never_covered_307.py`
  (11/11 pass); pins the generic DOM-scan approach
  (`getComputedStyle`, `position: "fixed"` filter, full-screen backdrop
  exclusion), the `MutationObserver` with the right attribute filter,
  the `transitionend` / `animationend` re-scan hooks, `showToast` /
  `hideToast` calling `qgUpdateFixedTopOffset`, and the `qg-rescue-toast`
  entry in the translation dictionary. Asserts the OLD hard-coded
  `getElementById("qg-banner")` / `getElementById("qg-stale-bar")`
  form is gone.
- **Regression** — 175 passed, 1 skipped, 0 failed across every test
  file that touches `dashboard_build/index.html`.

### How Paul should test cleanly (per Standing Rule B)

1. Hard refresh (Cmd/Ctrl+Shift+R) the dashboard once GitHub Pages picks
   up the push.
2. Sign in as usual.
3. Trigger a test alert against a `qgtest-<random>` device (never a real
   one — Standing Rule A).
4. Fire a rescue-flow action so the top status/update message bar
   slides in. Confirm the sticky alarm panel slides down by the toast's
   height — no overlap with Acknowledge / Show me who / Silence.
5. Repeat with the regular success banner and the "board not updating"
   red bar.
6. If (for any residual reason) the on-screen "buttons covered" line
   ever appears, confirm it names the offending element in plain
   English — never a code name.

### Deploy state

`memory/dashboard_build/index.html` is staged. Push to
`PaulVincentiSafequake/SafeQuake` main (path
`backend_dashboard/public/index.html`) is pending a fresh GitHub PAT
from Paul.

## 2026-08-29 — Paul's #320: "Remove all" leaves survivors from other surfaces

While verifying #307/#308 live, Paul added test people twice and removed
them. The "33 test people" count read (5) before and still (5) after.
One survivor: **`qg-1785757225898-jy34olbg`, tagged TEST, "trapped for
22 days"**. That id shape — `qg-<13-digit epoch>-<8 lowercase>` — is
the *real* device-id shape, so it doesn't match the `_test_seed`
predicate the previous /clear was keyed on.

### Every way a test entry can be created

1. `POST /api/admin/test-people/seed` — writes 33 rows with
   `_test_seed=SEED_TAG`, `is_test=True`, `synthetic=True`,
   `device_id=qg-{SEED_TAG}-…`.
2. `POST /api/admin/devices/{id}/mark-test` — sets `synthetic=True` on
   a **real-shaped** device_id. **This is Paul's survivor's origin.**
3. `scripts/load_test_seed.py` — sets `synthetic=True`,
   `load_test_run_id=<uuid>`, `device_id=qg-loadtest-*`.
4. Diagnostics/e2e/snippet/demo/playwright device_ids from harness
   runs (recognised by `deps.is_test_device()` marker substrings).
5. Any row with `is_test=True` set by any other code path (defensive).

### What the old /clear missed

Only category 1. Everything else survived indefinitely, and its open
alarms with it — that's Paul's stuck 22-day "trapped" ghost.

### Fix (generic, one-shot)

`POST /api/admin/test-people/clear` now delegates to a new sweeper
that unions **all five predicates** and returns a breakdown so
"count=5 → 0" is visible in the response:

```json
{
  "removed": 5,
  "alarms_cleared": 3,
  "seed_tag": "seeded-33",
  "matched_by": {
    "seed_tag": 0, "synthetic_flag": 1, "is_test_flag": 0,
    "load_test_run": 0, "marker_id": 4
  }
}
```

The alarm sweeper now also accepts the exact list of ids we just
deleted, so a mark-test-flagged real-shaped device (which matches no
regex) still gets its open alarm resolved. Response shape is
backward-compatible: `removed`/`alarms_cleared`/`seed_tag` are
unchanged, `matched_by` is additive.

### Same fix applies to the /seed idempotency branch

Pressing "Add 33 test people" now sweeps prior test rows from **any**
surface before writing the new batch, not just the prior `_test_seed`
batch. Add-without-clear from any prior surface is fully cleaned up.

### Test suite

`tests/test_test_people_320_all_surfaces.py` covers all five surfaces
individually plus a mixed end-to-end. testing_agent independently
reproduced Paul's exact survivor scenario with device_id
`qg-<13-digit epoch>-jy34olbg` against the public backend and
confirmed it's cleared on the first /clear. 16/16 focused tests pass
locally and against the public URL.


---

## 2026-08-30 — Task #193: Persistent offline queue for help reports (guaranteed delivery)

**Paul, 2026-08-30:** *"Right now, if someone taps 'I need help' with no signal, they may see it as sent when we never actually received it. That's the worst failure this app can have — a person believing help is coming while nobody knows they exist. I want the phone to hold onto that report and keep trying until our server confirms it has really arrived. Until then, the person must never see a tick, a green mark, or any wording suggesting it was sent. It should honestly say it's still trying, and tell them plainly the moment it gets through. Earthquakes knock out phone networks — this is the normal case, not a rare one."*

### Design (locked)
- **New module `frontend/src/utils/helpQueue.ts`** — persistent AsyncStorage queue (key `qa_help_queue_v1`) owns every check-in submission. Enqueue happens BEFORE any network call, so a phone that dies mid-tap still has the report on next boot.
- **Truth signal = HTTP 2xx from OUR backend** (`/api/status`). Legacy Render endpoint gets a fire-and-forget parallel post for compatibility, but its return does NOT count as "reached us". The rescuer dashboard reads from our backend — that's the only server whose 200 means a real person will see the report.
- **Retry schedule:** 2s, 5s, 10s, 30s, 60s, 120s, 300s, then every 5 min forever. Also kicks on AppState "active" transitions (phone regains signal → immediate retry).
- **UI state machine on `/alert`:**
  - `sending` → initial attempt in flight (GPS + first fetch)
  - `pending_retry` → attempts ≥ 1, still no 2xx. Amber toast "Still trying to reach the rescue team… Attempt N" plus a "Try now" button. Buttons show "Still trying…" — never green, never a tick, never "sent"/"delivered".
  - `sent` → queue reports `confirmed_at` set. Only NOW does the green success toast / "reached the rescue team" copy render.
- **Persistent local notifications** (identifier `quakeangel-help-pending` / `quakeangel-help-delivered`, channel `quakeangel-help-status`):
  - Pending: passive (no wake / no sound), sticky on Android — visible on the lock screen while unconfirmed so a user who locks their phone right after tapping still sees the honest state.
  - Delivered: active + default sound — the moment the user has been waiting for; we want them to notice from any screen.
- **Anti-lie contract (locked):** no user-facing surface on `/alert` — including the mobility and egress follow-up modals — may render the words "sent", "delivered", "received", or a green/tick/"Marked safe" visual while `helpQueue.confirmed_at` is null for the tracked item. The follow-up modal subtitle was reworded from "Your report is already sent — this adds one detail to it." to "Your first answer is safe on this phone — this adds a detail to it." to comply.
- **Egress render fix:** the trapped-toast summary was rendering `egress='not_answered'` (a real answer meaning "user did not answer") as "you cannot get out — extraction needed". Now gated on egress ∈ {can_exit, cannot_exit}.

### Files touched
- `frontend/src/utils/helpQueue.ts` (new, ~330 LOC)
- `frontend/src/utils/checkin.ts` — `postStatus()` → `submitStatus()` returning a queue item ID
- `frontend/src/utils/reminders.ts` — two new notification helpers
- `frontend/app/alert.tsx` — new `pending_retry` status, queue subscription, new toast, safe/trapped button relabels, follow-up-modal subtitle rewording, egress-render fix
- `frontend/app/_layout.tsx` — boots the queue on app launch, wires trapped-report pending/delivered notifications
- `frontend/app/index.tsx` — uses `submitStatus` for background `not_responding` pings

### Verification
- Testing agent iteration 50: 5/5 original scenarios (happy, offline, recovery via "Try now", reload-persistence, wording invariant) + 2 fix scenarios (mobility/egress subtitles, egress render) all PASS end-to-end using Playwright route.abort to simulate "no signal".
- No backend contract change. Backend pytest suite unchanged.


### 2026-08-30 — Task #193 v2: battery-conscious retry schedule

**Paul, same day:** *"It retries every 5 minutes forever. But the person most likely to be stuck retrying is someone trapped under rubble with no signal, and a phone hunting for a network is one of the biggest battery drains there is. Keep widening the gap: roughly every 5 min for the first half hour, every 15 min up to two hours, every 30 min up to twelve hours, then hourly. Also send immediately whenever the phone regains signal or comes back to the foreground, since that costs nothing. The person should never be told it's slowing down — it should just quietly last longer."*

**Change:** `nextRetryDelayMs(attempts, ageMs)` in `helpQueue.ts` — age-based back-off:
- 0–30 min: every 5 min
- 30 min – 2 h: every 15 min
- 2 h – 12 h: every 30 min
- 12 h+: hourly

**Never-attempted exception:** `attempts === 0 → 0 ms`. A brand-new item (including a follow-up submission enqueued mid-flush) always gets its first swing at the network immediately — the back-off only applies AFTER a real attempt. This closed a regression where a follow-up item was stuck at "Sending…" for 5 min because its initial `scheduleFlush(0)` was swallowed by the still-running flush loop.

**Zero-cost events still bypass the schedule:** AppState → active fires `kickFlush()` → `scheduleFlush(0)`. The "Try now" button does the same.

**Copy invariant (locked):** the pending toast NEVER mentions timing or slowing down — no "every 5 minutes", no "saving battery", no "less often". The visible copy is always "Still trying to reach the rescue team… Attempt N" regardless of what interval the queue is currently on. The person's phone quietly lasts longer without their being told.

**Verified:** testing agent iteration 52 — 5/5 scenarios PASS including the mid-flush-follow-up regression fix.


### 2026-08-31 — Task #321: pending button copy — "Not sent yet"

**Paul, 2026-08-31:** *"On the alert screen, when a report hasn't got through yet, the big yellow button says 'Still trying…'. That's unclear — it sits exactly where the person tapped 'I need help' a second earlier, so it looks like something to press. Please change that button to read 'Not sent yet'. Not 'waiting for signal' — we can't know whether it's their signal or our server, and we must never state a cause we don't know. 'Not sent yet' is true either way and says the one thing that matters. Leave the amber banner above it exactly as it is."*

**Rule locked:** a pending-state surface must never state a CAUSE we don't know (their signal vs our server). The copy is `Not sent yet` — a status, not a diagnosis, not an action.

**3 surfaces changed (banner intentionally NOT changed):**
1. `alert.tsx` — I'm-Safe button label (safe outcome, pending_retry state)
2. `alert.tsx` — I-need-help button label (trapped outcome, pending_retry state)
3. `reminders.ts` — lock-screen `quakeangel-help-pending` notification title

**Preserved:** the amber banner (`alert-pending-retry-toast`) still reads `Still trying to reach the rescue team.` — that copy is what tells the user the app is doing something on their behalf; the button underneath now just states the status honestly.

**Verified:** testing agent iteration 53 — 5/5 scenarios PASS (trapped-pending label, safe-pending label, banner preservation, happy-path no-flash, recovery, and wording-invariant sweep for absent causal phrases like "waiting for signal", "server down", "no signal", "battery", etc.).



### 2026-09-01 — Task #185: group size at the address (never a count)

**Paul, 2026-09-01:** *"Our board counts phones but shows them as people. A family of five with two phones appears as two, so a rescuer arriving expects two and finds five. When someone checks in, ask one extra question: 'Including you, how many people are here?' — answered in one tap: just me, 2, 3, 4, 5 or more. That number must never be added into any total. Our headline counts stay counts of people who have reported, so they can never be double-counted. The group size belongs to that one person — it shows on their map pin and in their details, so a rescuer going to that address knows there may be more people there than the one who answered."*

**Anti-double-count contract (locked in code + comments):**
- `group_size` is an OPAQUE STRING BUCKET, not an int, so it is not `sum()`-able by accident.
- Buckets: `just_me`, `2`, `3`, `4`, `5_plus`. Missing/null = "unknown group size", never "just 1".
- There is no `total_people_estimate` and there will not be one.
- The field is stored on `device_status.group_size` and surfaced on `/api/devices` and `/api/admin/device-history/{id}`. It is **absent from every count** — `Counts` dataclass, `people_counts.compute_counts`, `/api/public/summary`, dashboard totals.
- The contract is written into two long block-comments (backend `StatusInPayload`, frontend `GroupSize` type) that name the invariant, name the failure mode, and instruct future readers to push back if anyone asks to break it.

**UX invariants:**
1. **Non-blocking by design.** The report is enqueued on the primary tap. The group-size sheet opens IMMEDIATELY after — it never gates the send.
2. **One tap.** Five pills (Just me · 2 · 3 · 4 · 5 or more), each ≥ 56pt tall.
3. **Skippable.** A "Skip" is a real answer — it clears any previously chosen value rather than silently keeping the old one.
4. **Offline-safe.** The chosen value travels in the same `helpQueue` payload as the primary report; a follow-up made offline waits with the report and lands together on the next 2xx.
5. **Correctable from the sent toast.** Mis-taps under stress happen; the "Reported: you and 3 others here" line on the success toast is itself a button — tap to reopen the sheet and change the answer.
6. **Copy is honest.** "Reported: just you here." / "Reported: you and N others here." / "Reported: you and 4 or more others here." Skipped = "How many people are here? Tap to add."

**Trapped-flow ordering:** severity tap → INITIAL SUBMIT → group-size sheet → mobility (yellow) or egress (green) or nothing (red) → follow-up submit. Group-size interposes AFTER severity so the report is already on the board when the sheet opens, matching Paul's rule from #289.

**Dashboard follow-up (separate repo — `dashboard_build/`):** the API now returns `group_size` on `/api/devices[].group_size` and on `/api/admin/device-history/{id}.last_known.group_size` + on each `events[].group_size`. The dashboard rendering itself (map pin badge + details panel) has NOT been shipped in this repo — that is its own task on the dashboard side.

**Files changed (this ship):**
1. `backend/server.py` — `StatusInPayload.group_size`, `_normalize_status_payload`, `/api/devices` clean(r), `/api/admin/device-history/{id}` (event snapshots + `last_known`).
2. `frontend/src/utils/checkin.ts` — `GroupSize` type + threading through `submitStatus`.
3. `frontend/app/alert.tsx` — state, `openGroupSizeSheet`, `chooseGroupSize`, threading through severity/mobility/egress, group-size Modal, "Reported: …" tappable line on both safe and trapped success toasts, `groupSizeSentence` helper, `GroupSizePill` component.

**Verified:** [pending testing_agent — Task #185.]


### 2026-09-02 — Task #326: live map — silence is never invisible

**Paul, 2026-09-02:** *"The moment an alert is triggered, every phone we alerted should appear red on the map straight away — before they answer anything. Right now a person who doesn't answer appears nowhere at all. I tested that myself this morning on my own phone. Silence must never be invisible. As people answer, their colour changes: red for needs help now, yellow for hurt but stable, green for safe. Greens then leave the map entirely — they are done and should not clutter it. Rescued people leave too, whatever colour they were. Anyone who answered and then went quiet keeps their colour but gains a mark showing that pin is their last known position, not where they are now. The operator can switch each colour on or off. Switching one off must actually remove those pins from the map, not just fade them. Nothing is ever deleted — only taken off the live view. The full history stays available."*

**Follow-up (Paul, 2026-09-02):** *"Answer: (a) yellow. The reason matters — that person tapped 'I need help'. Anyone who asks for help must never leave the live map, whatever their severity. One change to your plan. You wrote that stand-down clears the alerted flag so silence stops looking like an emergency. Please don't do that. Someone who never answered is exactly who we must not lose, and calling off an alert must never make them invisible. They stay on the board, red, until a human closes their case. What can change at stand-down is the wording, not their presence — the board should say the alert is over but these people were never heard from."*

**Locked contract (backend `people_counts.py`, docstring on `map_color`):**

The map colour is DERIVED SERVER-SIDE. Never recompute in a client. One source, one truth.

| State                                          | `map_color`  | Notes                                                    |
|------------------------------------------------|--------------|----------------------------------------------------------|
| Rescued (any prior colour)                     | `null`       | Off the map. `Show rescued` toggle brings back.          |
| Silent-since-alert (alerted, never answered)   | `red`        | Silence must never be invisible. **INDEFINITE** — never cleared by stand-down, only by human resolution or a fresh report from the phone. |
| Trapped + severity=red (Immediate)             | `red`        |                                                          |
| Trapped + severity=yellow (Serious/Stable)     | `yellow`     |                                                          |
| Trapped + severity=green (Minor/walking wounded) | `yellow`   | "Anyone who tapped I need help stays on the live map."   |
| Self-reported Safe                             | `green`      | Off by default; green toggle brings them back.           |
| `not_responding` with no alert                 | `null`       | Never used the app; still on the board's counts, no pin. |

**`last_known_position` field:** true when the person answered post-alert but has since gone quiet (silence_state ≠ null). Their pin keeps its colour but gains a dashed halo — the map says "this is where we last heard from them, not where they are now." Silent-since-alert people are NOT last-known (they never answered, so nothing to distinguish).

**Filter model:** three independent toggles on the map bar — **Red · Yellow · Green** — each on/off. Green defaults OFF (self-reported safes don't clutter the live view). Rescued kept on its own toggle. Toggle off REMOVES the pin, not fades it. `renderFiltersOn()` announces which colours are hidden so nobody thinks they're seeing everything when they aren't.

**Persistence contract (locked in `server.py` + comment):**
- On alert broadcast: `device_status.last_alerted_at = now` is upserted for EVERY recipient (stub-row created if the phone has never checked in).
- Stand-down does **NOT** clear `last_alerted_at`. A silent-since-alert person leaves the working board only when a human resolves them (mark-rescued, resolve-with-reason) or their phone finally reports in.
- Nothing is ever deleted; history stays available via `/api/admin/device-history/{id}` and `status_events`.

**Files shipped this task:**
1. `backend/server.py` — POST-broadcast `bulk_write` of `last_alerted_at` on every recipient's device_status row; `/api/devices` `clean(r)` exposes `map_color`, `last_known_position`, `silent_since_alert`, `last_alerted_at`.
2. `backend/people_counts.py` — new derivation helpers `silent_since_alert`, `map_color`, `last_known_position`; `last_alerted_at` added to `_load_rows` projection.
3. `dashboard_build/index.html` — three-independent-colour-toggle filter bar; `markerVisual` reads `map_color`; `makeIcon` draws dashed "last-known" halo; `iconSignature` re-icons when the derived state changes; "never answered" tag on silent-red pins; `renderFiltersOn` announces hidden colours.

**Dashboard follow-up (separate deploy, `dashboard_build/index.html`):**
- Diff is ready in this repo. Push to the dashboard repo when convenient. Nothing else on the dashboard side is required for this task — colour derivation, marker rendering, filter UI, and "which colours are hidden" wording all live in the same file.
- Optional next iteration: at stand-down, add a single banner **"Alert over — N people were never heard from"** (Paul, 2026-09-02: "What can change at stand-down is the wording, not their presence"). Their red pins stay; the banner just names them collectively. Not blocking for this task.

**Verified:** [pending testing_agent — Task #326.]

**Post-review sort fix (iteration 55 → 56):** `/api/devices` sort was coalesced to `max(updated_at, last_alerted_at)` so silent-since-alert stubs (broadcast timestamp only, no updated_at yet) sort to the TOP of the paginated response rather than the bottom. Without this, a 1001st recipient could be truncated off — the very defect this task exists to fix.

**Verified:** testing_agent iterations 55 (33/33) + 56 (36/36) = 69/69 across the full Task #326 test suite. #185 group_size and #193 offline queue regressions preserved (46/46 combined pass).


## 2026-09-03 — Paul's dashboard trio: group size on dashboard, coverage caveat everywhere, sign-in banner cleanup

Three requests, delivered together because they touch the same file surface
(`memory/dashboard_build/index.html`) and share a single design principle: one
source of truth for the words we show, so no two surfaces can drift apart.

**1. Group size on the dashboard (#185 tail):**
The mobile app already asks "Including you, how many people are here?" after
`I'm Safe` or `Need Help` (Task #185). The answer arrives on `device_status.group_size`
and the API surfaces it, but the dashboard was ignoring it. Now:
  * **Map pin badge** — a small white pill on the top-right of the shape:
    `3` for `2..4`, `5+` for `5`, nothing at all for solo/unknown. A "1" badge
    would be visual noise on every marker. Muted (rescued/off-board) pins skip
    the badge entirely.
  * **Card / popup line** — one plain sentence: `Just this person`, `3 people
    at this address`, `5 or more people at this address`, or `Group size not
    given`. Words, not a bare number, so an operator cannot mistake it for a
    total.
  * **Never a total.** `group_size` is not summed anywhere. Overlapping groups
    (two people in the same building each answering "5+") and unknown groups
    make aggregation unsafe.
  * **Wording is centralised** in `window.qgGroupBadge(n)` and
    `window.qgGroupLine(n)` — one definition, three call sites (`makeIcon`,
    `popupHtml`, `itemHtml`).
  * **iconSignature** now includes `groupSize` so the badge appears the instant
    a phone reports a new number, not on the next zoom.

**2. Coverage caveat, single source (Paul directive, 2026-09-03):**
"These numbers count only people using Quake Angel. Others may be trapped who
we cannot see." This must appear permanently on the dashboard AND on every
report and export, and it must come from ONE place in the code.

Implementation:
  * **Backend constant** `people_counts.COVERAGE_CAVEAT` — the single source.
  * **API surface** — added `coverage_caveat` to `/api/public/summary` (signed
    out visitors) and `/api/devices` (signed-in operators). Both refresh the
    dashboard on every read.
  * **Dashboard element** — `<div id="qg-coverage-caveat">` sits below the
    header, always visible, never dismissible. Populated by
    `window.qgRenderCoverageCaveat()` which prefers the server's value and
    falls back to `window.QG_COVERAGE_CAVEAT` (identical string). Painted
    once at DOMContentLoaded so the sentence is visible before the first API
    call returns.
  * **Every export carries it, from the constant:**
    - Audit CSV: `coverage_caveat` row after the covers row.
    - Audit PDF: bold brand-red paragraph near the header.
    - B1 team PDF: same, right below the covers line.
    - B2 public PDF: same, tightened to fontSize=9 / leading=11 / spaceAfter=3
      so the report still fits on one page (#126 hardening test).
  * **Drift removed from B2 footer.** The B2 footer used to open with a second
    version of the same idea ("These counts reflect app users who have checked
    in via Quake Angel during the window shown. They do not represent the
    total affected population."). Removed — that was exactly the drift Paul
    warned about. The footer now sticks to the privacy / next-of-kin claim
    and the top-of-page caveat is the single place this document describes
    coverage.

**3. Remove the permanent "Trouble signing in?" banner:**
This 45s watchdog banner was a workaround from when a fixed-position red
`qg-session-expired-banner` was covering the Google button (#265). The real
cause was fixed in that ticket. A permanent "sign-in might fail" warning
told every operator the system was unreliable before they even tried, which
undermines confidence for no reason. Removed:
  * `_stuckWatchdogTimer`, `_armStuckWatchdog`, `_cancelStuckWatchdog`,
    `_renderStuckHint` and their invocations.
  * The 45s `setTimeout` and its DOM append.
  * Historical comments retained so an inquiry can find why the code left.
  * Real failures still surface at the moment they happen: the
    `_wrappedGoogleCredential` `try/catch` calls `showError("Sign-in failed:
    ...")` in the credential handler.
  * **Layout swept:** the only fixed-position elements that could sit over
    the sign-in are the stale-board bar (hidden when `boardSignedOut=true`),
    the tremor strip (admin+operator only), the toast banner (only shown on
    demand, hidden by translateY(-100%) at rest) and the modal backdrop
    (display:none at rest). None can permanently cover the Google button.

**Files touched:**
  * `backend/people_counts.py` — added `COVERAGE_CAVEAT` constant.
  * `backend/server.py` — expose caveat in `/api/public/summary` and
    `/api/devices`.
  * `backend/reports_export.py` — caveat in audit CSV, audit PDF, B1 PDF, B2
    PDF (tightened) + B2 footer duplicate removed.
  * `backend/tests/test_export_hardening.py` — updated the shape-lock set to
    accept `coverage_caveat` (aggregate-safe, not device-shaped).
  * `backend/tests/test_coverage_caveat.py` — NEW. 4 tests: constant wording
    lock, `/api/public/summary` carries it, `/api/devices` carries it, audit
    CSV export carries it.
  * `memory/dashboard_build/index.html` — group-size helpers + badge + card
    line + popup line; permanent coverage caveat strip + JS constant +
    server refresh hooks in both signed-out and signed-in code paths;
    watchdog banner and its 45s timer removed.

**Verified:**
  * Backend: 94/94 across the affected suites (test_group_size_185,
    test_326_map_derivation, test_326_sort_pagination, test_coverage_caveat,
    test_audit_log_export) + 23/23 in test_casualty_reports_iteration_31 +
    34/34 in test_audit_log_export + test_report_chart_caption_and_grammar_iter35
    + 36/37 in test_export_hardening (the one remaining failure is a
    pre-existing rescued-count narrative-vs-table drift, confirmed unrelated
    by `git stash` reproduction).
  * B2 still fits on one A4 page (`test_b2_fits_on_one_page` passes).
  * Dashboard: file:// screenshot confirms caveat strip visible, sign-in
    button not overlapped, no "Trouble signing in?" hint present, and
    `qgGroupBadge` / `qgGroupLine` return the exact spec values across the
    full input range (null/1/2/3/5/7/10).


---

## Dashboard trio v2 (Paul, 2026-08-29) — DONE

Three tightly-scoped fixes on `memory/dashboard_build/index.html`.

### 1. "Recentre on Malta" now goes to a FIXED home position

Verbatim: *"'Recentre on Malta' put me in southern Sicily — Scicli,
Modica, Pozzallo on screen, Malta not visible at all. It must go to a
fixed home position showing Malta and Gozo, never a position worked
out from wherever the markers happen to be."*

- New constant `MALTA_GOZO_BOUNDS = L.latLngBounds([35.78, 14.15], [36.11, 14.62])`
  — a hard-coded rectangle covering both islands with a small sea margin.
- New function `recentreOnMalta()` calls
  `map.fitBounds(MALTA_GOZO_BOUNDS, { padding:[24,24], animate:false, maxZoom:12 })`.
- The Recentre control button and the map's INITIAL paint BOTH go
  through the same bounds — nothing derives the view from markers.

### 2. Test people no longer drag the map away

Same fix. The map is not moved by any data-load / marker code — no
`fitBounds(markerGroup.getBounds())`, no `map.setView(marker.getLatLng())`.
Guarded by `test_recentre_fixed_home.py::test_no_marker_derived_fitbounds_anywhere`
which scans the file (with `//` comments scrubbed) for the forbidden
patterns.

### 3. Every "text-looking" control now reads as a button

Found 7 patterns of controls that rendered as body-copy text:

| # | What                                                         | Where |
|---|--------------------------------------------------------------|-------|
| 1 | "What happened" disclosure                                   | alarm cards (`.qg-alarm-story summary`) |
| 2 | "Show me who (N)" disclosure                                 | alarm cards (`.qg-alarm-names summary`) — 2 templates |
| 3 | "Tremor notifications — what's been sent" disclosure         | `#qg-tremor-panel > summary` |
| 4 | "🧪 Admin testing tools — Preview Mode & radius override"    | `#qg-admintools > summary` |
| 5 | "📱 Registered devices" disclosure                            | `#qg-devices-panel > summary` |
| 6 | "Show me who" disclosure in export receipt                   | `whoList` template |
| 7 | "Refresh now" inline anchor                                  | preview-panel meta line |

Two new shared treatments now cover every one of them:

- `.qg-alarm-story summary, .qg-alarm-names summary` — filled pill,
  custom `▸` chevron that rotates on `[open]`, hover feedback,
  visible focus ring, `list-style: none` and hidden
  `::-webkit-details-marker` so the native triangle can't show.
- `.qg-disclosure-btn` — reference-tier pill for admin/export
  disclosures (used on all three admin heads and the export "Show me
  who").
- `.qg-inline-btn` — pill treatment for anchors used as buttons (used
  on the "Refresh now" anchor).

### Tests added
- `tests/test_recentre_fixed_home.py` — 4 static-HTML asserts
- `tests/test_text_buttons_are_buttons.py` — 4 static-HTML asserts

### Verification
- 8/8 guard tests pass locally and via testing_agent.
- Playwright visual pass on a local `http.server`: Malta+Gozo visible
  on initial paint AND after Recentre at both 1400×900 and 500×900
  (narrow portrait). No Sicily creep. Alarm-card summaries, admin-tool
  summaries and the "Refresh now" anchor all render as obvious pills
  with chevrons and hover states.



### Follow-up (2026-08-29, same day): map didn't fit the visible viewport

Paul, verbatim: *"the map is taller than my browser window, so its
centre sits below the fold. After pressing Recentre on Malta I still
have to scroll down to see Malta at all."*

Root cause: `#map-wrap` was `flex: 1 1 60%` in a wrapping flex row,
so it grew to match the sidebar's tall intrinsic content (count pills,
count notes, off-board area, walking-wounded list). On any typical
laptop that pushed the map's centre below the fold — `fitBounds`
framed Malta correctly INSIDE the div, but the div itself extended
off-screen.

Fix (single CSS edit):
```css
.layout { … align-items: flex-start; }
#map-wrap {
  align-self: flex-start;         /* decouple from sidebar height */
  height: calc(100dvh - 220px);   /* fit visible viewport minus top chrome */
  min-height: 340px;
  max-height: 780px;
}
```

`map.invalidateSize()` (already wired to load/resize via `fixMapSize`)
picks up the new size automatically.

Verification (testing_agent iteration 58):
- 9/9 guard tests pass (including a new
  `test_map_container_is_capped_to_the_visible_viewport`).
- Geometric Playwright proof at four viewport sizes, on each the
  `#map-wrap` bottom edge sits INSIDE `window.innerHeight`, and after
  clicking "Recentre on Malta" `document.body.scrollTop === 0`:
  | Viewport | wrap bottom | innerHeight | Fits? |
  |---|---|---|---|
  | 1440×900 | 884 | 900 | ✅ |
  | 1366×768 | 752 | 768 | ✅ |
  | 1280×720 | 704 | 720 | ✅ |
  | 500×900 (mobile 50vh path) | 787 | 900 | ✅ |


---

## #322 — Group size reaches the board again (Paul, 2026-08-29)

Paul, verbatim: *"I tapped 'I need help', chose 'seriously injured /
can't move', then answered the group size question — 5 the first time,
4 the second. The app itself confirms it. […] But on the dashboard,
the map pin has no number badge, and its popup says 'Group size not
given'. […] Tell me which of those four it was."*

Answer: **#4 — the dashboard was not reading the field.**

Chain of evidence:
1. Follow-up sent? YES — `alert.tsx::chooseGroupSize` re-enters
   `submitCheckIn(..., isFollowUp=true, size)` which enqueues a fresh
   POST /api/status carrying `group_size: "<bucket>"`.
2. Rejected server-side? NO — `StatusInPayload.group_size` validates
   against `^(just_me|2|3|4|5_plus)$`; the value is stored on
   `device_status` and appended to `status_events`.
3. Missing from API? NO — `/api/devices` returns
   `"group_size": r.get("group_size")` unchanged (opaque bucket).
4. Dashboard reads it? **NO — this was the bug.** Ingest at
   `memory/dashboard_build/index.html` had:
   ```js
   groupSize: (typeof d.group_size === "number" && d.group_size >= 1)
     ? d.group_size : null,
   ```
   Every real wire bucket (`"2"`, `"4"`, `"5_plus"`…) is a STRING, so
   the type check discarded them all and the renderer received `null`,
   which prints "Group size not given".

Fix: one place, one function, converts the bucket to the number the
renderer wants:
```js
groupSize: (function (raw) {
  if (raw == null) return null;
  if (typeof raw === "number" && raw >= 1) return raw;  // legacy tolerated
  if (raw === "just_me") return 1;
  if (raw === "5_plus") return 5;
  if (raw === "2" || raw === "3" || raw === "4") return parseInt(raw, 10);
  return null;
})(d.group_size),
```

### Tests
- New: `tests/test_dashboard_reads_group_size_322.py` — 4 tests, includes
  a behavioural test that runs the JS normalizer via `node -e` against
  every bucket (`just_me→1`, `2→2`, `3→3`, `4→4`, `5_plus→5`, unknown→null).
- Testing agent iteration 59 added `tests/test_e2e_322_group_size_roundtrip.py`
  — 4 tests, does a real HTTP round-trip: POST /api/status → GET
  /api/devices → run the dashboard normalizer on the returned bucket.
  All pass.

### Verification (testing_agent iteration 59)
- New guard tests: 4/4 pass
- Existing `test_group_size_185.py`: 30/30 pass (anti-double-count intact)
- New E2E round-trip: 4/4 pass
- Regression pack: 70/71 pass, 1 skip; only failure is the pre-existing
  unrelated `test_b2_rescued_narrative_equals_table` (64 vs 70 rescued).

---

## #331 — A new alert reactivates previously rescued/safe on the map

Paul, verbatim: *"When a new alert fires, anyone previously marked safe
or rescued stays hidden on the map until they answer. […] Being
previously rescued cannot be an exception — those are exactly the
people standing in a damaged building when an aftershock hits."*

Root cause was a three-layer bug:
1. `people_counts.map_color()` short-circuited on `rescued_at` BEFORE
   checking `silent_since_alert`. A rescued phone we alerted again
   never got a colour.
2. Dashboard `matchesFilter` hid every `status === "rescued"` behind
   the "Show rescued" toggle regardless.
3. Dashboard `markerVisual` returned the green ✓ "found" visual for
   every rescued row regardless.

Fix (one function per layer):
- Backend `map_color`: `silent_since_alert(row)` is the first check;
  returns `"red"` unconditionally when true. Only falls through to
  the rescued/safe/trapped branches when NOT silent-since-alert.
- Dashboard `matchesFilter`: guard is now
  `if (u.status === "rescued" && !u.silent_since_alert) return showRescuedOnMap;`
- Dashboard `markerVisual`: same guard — rescued+silent draws the
  SILENT-RED visual ("never answered" tag), not the ✓ found visual.
- Dashboard `mapColorFor` legacy fallback: `silent_since_alert`
  check re-ordered above `status === "rescued"`.

**Nothing is deleted from the row.** `rescued_at`, `rescued_by`,
`pre_rescue_status`, `pre_rescue_severity`, `pre_rescue_mobility`
stay on `device_status`. `status_events` still contains the rescue
entry. Verified end-to-end by testing_agent iteration 60: after
stamping `last_alerted_at` on a rescued row, `/api/devices` returns
`map_color=="red"`, `silent_since_alert==True`, and `rescued_at` +
`rescued_by` + `status=="rescued"` are preserved byte-for-byte.

## #332 — Group-size line reads as REPORTED, not KNOWN

Paul, verbatim: *"Where the pin popup currently says '4 people at
this address', change it to: 'App user said 4 people are here
including them. There may be more we do not know about.' […] it
must be clear this is what the person told us, not something we
know. And it must never say those people are trapped — we only
asked how many are there, not how many are hurt."*

`window.qgGroupLine` rewritten to:
```
null → "Group size not given"
1    → "App user said they are the only person here."
2..4 → "App user said N people are here including them. There may be more we do not know about."
5    → "App user said 5 or more people are here including them. There may be more we do not know about."
```

The words "at this address", "trapped", "injured", "hurt", "casualty"
are all now banned inside `qgGroupLine` by a guard test. Behavioural
`node -e` test runs the function against every bucket and confirms
both the exact sentences and the absence of severity words.

### Tests
- New: `tests/test_331_silent_since_alert_beats_rescued.py` — 15
  tests, incl. `node -e` behavioural on `qgGroupLine`.
- Updated: `tests/test_326_map_derivation.py` — flipped
  `test_rescued_beats_silent_since_alert` to
  `test_silent_since_alert_beats_rescued`; added
  `test_rescued_with_alert_only_when_updated_is_after` for the
  ordering-matters boundary.
- Added by testing agent (iteration 60):
  `tests/test_iteration_60_step_b_e2e.py` — 2 tests, real HTTP
  round-trip against the public backend URL proving the doctrine.

### Verification (testing_agent iteration 60)
- 46/46 new + updated guard tests pass.
- 2/2 e2e round-trip tests pass.
- 66/66 regression pack tests pass.


---

## 2026-02-XX — Dashboard "Trapped for …" clock started from the wrong time

### Paul's report (verbatim)
> On the dashboard, a person's pin says "Trapped for 2 hours and 20 minutes"
> when they actually reported trapped less than 5 minutes earlier. I checked
> twice, 4 minutes apart, and the number went up by exactly 4 minutes — so
> it's a real clock, just starting from the wrong time. Can you find out
> why it's not using their most recent report as the starting point, and
> fix it?

### Root cause
`reports_export._trapped_since_map` walked back through the ENTIRE
`status_events` ledger for the device. If a device reported "trapped" in a
PRIOR alert and was never explicitly closed by a `safe` / `rescued` event,
that stale timestamp was returned as the CURRENT-spell start when the
device reported trapped again in a new alert. Two consecutive trapped
events across two different alerts were being read as one long spell.

### Fix
- New helper `reports_export._current_alert_start()`: returns the
  `created_at` of the latest `push_events` row of kind `trigger` (legacy
  rows without a `kind` are triggers by convention; `alert_stood_down`
  rows are explicitly ignored — a stand-down is NOT an alert start).
- `_trapped_since_map` now bounds its ledger query with
  `recorded_at >= _current_alert_start()`. Events strictly before the
  current alert cannot be part of the current spell. When there is no
  `push_events` on record we fall back to the original unbounded walk,
  so bootstrap/test-only fixtures still work.

### Tests
- New: `tests/test_trapped_since_bounded_by_alert.py` — 10 unit tests
  covering the verbatim Paul scenario, stand-down-after-prior-alert,
  same-alert multi-trapped, safe-between-trapped, no-push-events
  fallback, legacy-no-kind trigger, and the current-alert-start helper
  contract.
- New (added by testing_agent, iteration 62):
  `tests/test_trapped_since_live_integration.py` — 2 tests, real HTTP
  round-trip against `/api/devices` that seeds the verbatim Paul
  scenario in MongoDB and asserts `trapped_since` equals the fresh
  event (not the stale one), plus a device with only pre-alert trapped
  events getting `trapped_since = null`.

### Verification (testing_agent iteration 62)
- 10/10 new unit tests pass.
- 2/2 new live integration tests pass.
- `test_export_hardening.py::test_devices_trapped_since_and_short_codes`
  still passes (no regression).
- 61/62 adjacent regression pack tests pass (the single failure —
  `test_b2_rescued_narrative_equals_table` — is a pre-existing
  regression unrelated to this fix, tracked separately).

---

## 2026-02-XX — Pin popup wording second pass (#332, second cut)

### Paul's report (verbatim)
> The pin popup still says "X people at this address" instead of the
> wording we asked for: "They said X people are here. There may be more
> we do not know about." Can you check where this text actually lives
> on the popup, since the earlier change doesn't seem to have reached
> it, and update it there?

### Investigation
The "X people at this address" wording was already removed in the first
#332 pass (commit 53ebecf) — the current source has NO instance of that
string. Paul was looking at the DEPLOYED GitHub Pages copy, which lags
the repo until the site is redeployed. Confirmed with him. Not a code
regression.

### Wording change he asked for on top of that
The first-pass wording was "App user said N people are here including
them. There may be more we do not know about." Paul asked to tighten it
to "They said N people are here. There may be more we do not know
about." — dropping the "App user" category noun (reads awkward on a
pin), and the "including them" clause (redundant because the mobile app
already frames the question as "including you, how many people are
here?" so the answer already includes the answerer).

### Fix
`window.qgGroupLine` in `memory/dashboard_build/index.html` (~line 1507)
now emits:
- null → "Group size not given"
- 1    → "They said they are the only person here."
- 2..4 → "They said N people are here. There may be more we do not know about."
- 5+   → "They said 5 or more people are here. There may be more we do not know about."

Both call sites — `popupHtml` (map popup, ~line 7885) and `itemHtml`
(sidebar card, ~line 8024) — go through `qgGroupLine`, so the map pin
and the sidebar card share the same string.

### Tests
Updated `tests/test_331_silent_since_alert_beats_rescued.py::TestGroupSizeWording`
- 5 tests total, including new `test_qg_group_line_no_longer_says_app_user_or_including_them`
  guard that fails if the old strings ever come back.
- Behavioural node test now asserts the exact new sentences (anchored
  with `^` and `$`) and adds `App user`, `including them` to the
  leaked-forbidden-words check.

### Verification (testing_agent iteration 63)
- 5/5 TestGroupSizeWording tests pass.
- 53/53 adjacent regression tests pass (test_dashboard_reads_group_size_322,
  test_group_size_185, test_e2e_322_group_size_roundtrip,
  test_iteration_60_step_b_e2e, full test_331).
- Grep confirms no live code path emits the banned strings.
- Redeploy required to make Paul see the change on the live GitHub
  Pages dashboard.

## #341 — Card note + preserve last-known location (2026-09-04)

### Symptom (Paul, verbatim)
> QQ43D is tracked as trapped, with a full history in the alarm panel
> and counted in "1 TRAPPED" — but it has no pin anywhere on the map.
> Can you check whether this device has a saved location? If it
> doesn't, please add a plain note on its card saying so, instead of
> it just having no pin with no explanation. If it does have a
> location, something is failing to draw the pin — please find out
> why.

### Root cause (two-part)
1. Every check-in through `POST /api/status` used `{"$set": doc}` on
   `db.device_status`, so a re-check that arrived WITHOUT lat/lng
   (permission revoked, indoors, quick-answer flow, pre-permission
   build) unconditionally nulled a previously-known fix. Result: pin
   silently vanishes even though the row is still on the board.
2. The dashboard card (`itemHtml` in `memory/dashboard_build/index.html`)
   said nothing when a person had no coordinates. Operator saw a card
   in "1 TRAPPED" with no matching pin and no explanation.

### Fix
- **Backend (`/app/backend/server.py` post_status handler):**
  Build a sanitized `set_doc` and, if both new lat and lng are None,
  pop `latitude`, `longitude`, and `accuracy_m` before `update_one`.
  Rule: software may improve the map (fresh fix → stored) but never
  erase it (locationless report → previous fix stands).
- **Dashboard (`/app/memory/dashboard_build/index.html` itemHtml):**
  New `hasLoc` guard, new `noLocLine` rendered between `battLine` and
  `groupLine`, dedicated `.qg-card-no-loc` style (muted grey — a
  fact, not a failure). Exact wording: *"📍 No saved location — this
  phone never shared its position, so there is no pin on the map for
  them."*

### Tests
- `/app/backend/tests/test_341_no_pin_note_and_location_preserved.py`
  (7 tests: card sentence, hasLoc guard, HTML splice, CSS class,
  preservation of prior coords, fresh-fix overwrites, static server
  guard against `$set: doc` regression).
- `/app/backend/tests/test_341_live_http.py` (3 tests: live POST
  /api/status confirms round-trip through Mongo).
- 10/10 pass. 58 adjacent regression tests still green.

## #341 follow-up — Alarm-panel card also carries the note (2026-09-04)

### Symptom (Paul, verbatim)
> We checked live: the code for the "no saved location" note is
> deployed and correct, but it does not appear on the red "NEEDS HELP"
> alarm card for QQ43D — that card only shows the alarm history,
> nothing about location.

### Root cause
The first #341 pass added the note only to the SIDEBAR triage card
(`itemHtml`). Paul was reading the ALARM PANEL card, populated by
`GET /api/admin/alarms` from `board_alarms.list_open()`. Different data
source, different renderer, no location fields flowing through.

Live probe (testing_agent, iteration 65): 76 of 83 open alarms on
Paul's live DB have `has_location=False`, and 1 has a partial gap
(4/6 devices missing coords). So QQ43D isn't unusual — most alerted
phones on that deployment never shared a location.

### Fix
- **`/app/backend/board_alarms.py` `list_open()`**: batched
  `db.device_status.find({device_id: {$in: [...]}})` lookup builds a
  `_has_location` per row. Group fold sets `has_location` (strict-AND
  across members) and `missing_location_count`. Every person in
  `groups[].people[]` carries `has_location`.
- **`/app/memory/dashboard_build/index.html` alarm renderer** (~9276):
  emits `noLocLine` when `g.has_location === false`. Single-person
  wording matches the sidebar exactly; multi-person cluster says
  "N of M have no saved location". Amber-on-dark styling
  (`.qg-alarm-no-loc`) distinct from the muted-grey sidebar variant.

### Tests
- `/app/backend/tests/test_341_alarm_card_no_loc_note.py` (8 tests:
  renderer wording, guard on `g.has_location === false`, partial-gap
  message, HTML splice position, dedicated CSS class, backend fold-up
  in 4 shape variants).
- Adjacent regression suite (test_board_alarms_296, test_303, test_304,
  test_268 e2e, test_alarm_panel_clicks_302, test_alarm_buttons_never_
  covered_307) plus prior #341 tests: 85 passing.

### How to inspect a device's saved location on record
`GET /api/admin/device-history/{device_id}` returns every status event
with `latitude`/`longitude` per event AND the current row. For QQ43D
this endpoint answers "does this device have any location on file"
without needing a Mongo shell.

## Bug #342 — "No saved location" note clipped behind red header on QQ43D (2026-09-05 — Paul, live)

### Report
> "the No saved location note you just added is covered up on the live
> dashboard. On the red NEEDS HELP card for QQ43D, the note box sits
> under the red header, so its first line is unreadable and it also
> hides 'Send a team — IMMEDIATE, cannot move.' This is the fourth
> time content has been clipped behind a fixed element (#295, #307,
> #209, #253). Fix the layout cause once and sweep every card type."

### Root cause (systemic, not per-instance)
`.qg-alarm` was a horizontal flexbox: `[shape] [body] [Acknowledge]`
with `align-self: center` on the button. When the body grew (a
`.qg-alarm-since` line, an expanded story, the new `.qg-alarm-no-loc`
note), the button vertically centred against a body that was now ~120px
taller and ended up hovering NEXT TO the action line "Send a team —
IMMEDIATE, cannot move." — reading as "the button is on top of the
order to send a team". Two secondary contributors: `#qg-annun-rows`
had its OWN `overflow-y:auto + max-height:38vh` scroll INSIDE the
sticky topstrip's `overflow-y:auto + max-height:50vh` — a nested
scroll where a tall card could slip half-in-half-out of the inner
container while the outer container stayed still. And the sidebar's
`.qg-card-no-loc` was `display:inline-block`, so it COULD share a line
with the next inline element.

### Fix (`/app/memory/dashboard_build/index.html`)
1. **`.qg-alarm` → `display: grid`** with `grid-template-columns:
   22px 1fr; grid-template-rows: auto auto`. Shape + body on row 1;
   the **Acknowledge button spans row 2 full-width** below every piece
   of body content. No `align-self`, no vertical centring, no second
   axis on which anything can drift over anything else.
2. **`.qg-alarm-body { min-width: 0 }`** — long headlines wrap inside
   the grid column instead of pushing the column wider.
3. **`.qg-alarm-ack { min-height: 44px; width: 100% }`** — full-width
   tap target on its own row (also brings it up to iOS/Android touch
   target guidance).
4. **`.qg-alarm-no-loc { display: block; width: 100%; box-sizing:
   border-box }`** — the amber note is a standalone line, always.
5. **`.qg-card-no-loc { display: block; width: 100% }`** — same for
   the sidebar-triage variant.
6. **Removed `#qg-annun-rows` inner scroll** (kept only the outer
   `.qg-topstrip` scroll cap #295 defined) — one scroll ancestor for
   the whole panel; a card is either fully in view or the panel scrolls
   to bring it fully in view, half-in-half-out is now geometrically
   impossible.
7. **`scroll-margin-top: 12px` on `.qg-alarm`** — belt and braces for
   scroll-into-view calls in the future.

### Runtime detector extended (`checkNothingIsCoveringTheButtons`)
Was: checked ONLY `#qg-annun button` (the Silence / Acknowledge
buttons, #302/#307 doctrine — a covered button that does nothing on
tap is a silent failure). Now: also samples the geometric centre of
every `.qg-alarm-ack`, `.qg-alarm-action`, `.qg-alarm-no-loc`,
`.qg-alarm-headline`. If any critical line lands on a foreign element
the panel says so in plain English and logs the raw covering element
to the console for the developer.

### Sweep — how many card types were checked
Every card type in the dashboard:
1. **Alarm-panel card** (`.qg-alarm`) — FIXED (grid layout + note
   `display:block`).
2. **Sidebar triage card** (`itemHtml` → `<li>`) — FIXED (`.qg-card-
   no-loc` now `display:block; width:100%`).
3. **Map popup** — inspected; does not render the No-Saved-Location
   note (pin only exists when location exists), no button-over-text
   layout. No change needed.
4. **Rescued list, off-board list, audit-item cards, stat cards, ask-
   flow modal, Recent Activity items** — inspected; none use
   `align-self: center` on a horizontal-flex button, none carry the
   note. No change needed.

**2 card types were carrying the note; both fixed. 1 layout pattern
(horizontal-flex + `align-self:center` button next to variable-height
body) was the root cause and it was found in exactly one place — the
alarm-panel card.**

### Test steps (paste to Paul)
1. Hard refresh live: `Cmd+Shift+R` on Safari / `Ctrl+Shift+R` on
   Chrome & Firefox — the CSS is cached at Render's edge for ~1h so
   a soft refresh is not enough.
2. Sign in with a TEST operator account (not `pmvincenti@`).
3. From a TEST device (not Paul's phone), fire an alert without ever
   sharing location — the pattern that produces QQ43D on the map.
4. Look at the red NEEDS HELP card in the top-right alarm panel:
   - `NEEDS HELP NOW` header
   - Headline (short code — Trapped)
   - `Send a team — IMMEDIATE, cannot move.` — fully readable, no
     element sitting on top of it
   - `14:32 Malta time` meta line
   - Amber `📍 No saved location…` note — full width, standalone line,
     never overlapping anything
   - Full-width white `Acknowledge` button ON ITS OWN ROW below all
     the text. It is NEVER beside the action line.
5. On an unacknowledged card the panel-level `qg-annun-blocked` line
   stays hidden — the extended detector actively verifies nothing
   covers the action or the note.

### Regression tests (unit / visual)
- Grid geometry unit tests to be added: alarm-panel card has button
  in `grid-row: 2`, no `align-self: center` anywhere in the sidebar
  alarm CSS.
- Runtime detector unit test to be added: covers every
  `.qg-alarm-action` / `.qg-alarm-no-loc` / `.qg-alarm-headline` in
  addition to buttons.


---

## Dashboard map — colour + shape + legend (2026-02-11)

### Ask
1. Three states by colour on the operator map: red = needs help now,
   yellow = hurt but stable, green = self-reported safe.
2. Split red into TWO shapes so colour is never the only signal:
     * FILLED red circle → they asked for help.
     * OUTLINE (hollow) red ring → we alerted them, they never
       answered.
   Must still work when printed in black and white.
3. Hide greens and rescued from the map by default. Toggles stay.
4. Filters REMOVE pins, they do not dim them.
5. Add a legend naming every colour and every shape in plain English,
   with a line stating that greens and rescued are hidden by default.
6. Invariants preserved:
     * Greens and rescued still count in every headline number
       (filters change the map only, never the totals).
     * Anyone hidden who flips back to needing help reappears
       immediately (next poll tick, ≤ 4 s).
     * When someone turns red is unchanged (server-side map_color
       rule set — a phone that gets an alert and does not answer is
       still red the instant we alerted it).
     * Nothing is deleted; full history stays available.
     * Filters are a lens.

### Implementation (files touched: 1, discrete places: 4)
All changes are in `/app/memory/dashboard_build/index.html`:
  1. `svgShape()` — new `"ring"` shape (white centre, thick coloured
     stroke, thin black outer stroke so the outline reads in B&W).
  2. `markerVisual()` — silent-since-alert reds now pick shape
     `"ring"` instead of `"circle"`; asked-for-help reds keep the
     filled circle. Tags ("SOS" / "never answered") stay for extra
     redundancy.
  3. Legend HTML — floating panel at the bottom-left of the map,
     `<details>` element, default open. Names all five pin visuals in
     plain English (red filled circle, red outline ring, yellow
     triangle, green dot, grey circle with a tick) plus the
     "hidden by default" note and the extra-marks note (dashed halo
     for last-known-position, small badge for group size).
  4. Legend CSS — `.map-legend` + rows + summary + print rules; no
     `position: fixed`, uses `absolute` inside `#map-wrap`.

### Verification
- Visual verification of the legend + filter controls done against a
  locally-served copy of the dashboard file (signed-out view renders
  the map, filters and legend without needing backend data).
- Visual verification of the five pin shapes (colour and B&W)
  rendered via a temporary standalone harness; each of the five
  shapes is distinguishable without colour.
- No touch to any counts code path (`updateCountPills`,
  `pillsFromServerCounts`), so totals cannot be affected by filter
  state.
- No touch to server-side `map_color` rules.
