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
