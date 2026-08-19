# Design Invariant (established 2026-08-19, Batch 7 #225)

## A feature that is switched off must say so somewhere a human will see it.

Recorded alongside the six patterns as the seventh discipline the product
must be built to. Motivating incident: for weeks Paul believed the app
was sending tremor notices; the shipped code disagreed; nothing anywhere
in the product made that disagreement visible. Same failure class as a
dashboard showing stale data as though live.

**Rule form:** any flag, kill-switch, feature-gate, or config value that
can silently disable a feature a user is relying on must render its own
"currently off" state somewhere that user will actually see. Not in a
log, not behind a debug flag, not in a diagnostics endpoint alone —
somewhere they will read without going looking.

**Two-layer discipline** (from the panel Paul asked for):
1. **Standing condition** → persistent, non-dismissible surface (a strip,
   a chip, an inline note beside the affected value). One sentence,
   plain language, no enums.
2. **Detail on demand** → collapsible or "reference tier" panel with
   the full state (last sends, ingest counts, poller health). Not in
   the working area.

The strip is the invariant. The panel is the follow-up.

---

## Kill-switch audit — every gate found in the codebase, and whether it
## currently announces itself when off

Ordered by product risk (highest first).

### 1. `country_configs.preview_mode.enabled` (per country)

- **Effect when off:** No tremor notifications are sent to ANY device.
  `dispatch_preview_if_needed` and `dispatch_place_notices` both bail out
  at the first gate (`emsc/preview.py:196, 692`).
- **Currently announces itself?** ✅ AS OF THIS BATCH.
  Persistent top-strip on the dashboard reads *"This system is not
  sending tremor notifications to anyone at the moment"* whenever the
  admin/operator is signed in, plus the reference panel with detail.
  Fetched from `GET /api/admin/tremor-diagnostics`.
- **Before this batch:** silently off. Motivating example for the whole
  invariant.

### 2. `country_configs.preview_mode.device_ids` (empty list)

- **Effect when empty:** Preview mode may be `enabled=True` but nobody
  is on the allowlist, so no notification ever fires.
- **Currently announces itself?** ✅ AS OF THIS BATCH.
  The same top-strip reads *"Tremor notifications are switched on, but
  nobody is on the list to receive them"* — computed as a distinct
  human_state from `is_sending: false, phones_on_list: 0`.

### 3. `country_configs.shadow_mode` (per country, currently always True)

- **Effect when True:** The `would_have_fired` events are logged but no
  real broadcast happens. Preview mode is the ONLY tremor-notification
  path while shadow_mode is True.
- **Currently announces itself?** ⚠️ NO. Shadow mode is a
  design-lifetime flag, not something an admin flips per-day — but if
  Round 3 or later brings the "exit shadow mode" launch, the same
  invariant applies: while shadow_mode is True, the top-strip should
  say *"Real tremor broadcasts are not enabled yet; only the preview
  allowlist receives notifications."* Noted for the launch plan.
- **Follow-up:** add to the tremor-diagnostics endpoint's headline
  computation when shadow_mode ships as a user-visible thing.

### 4. `recheck_sweeper.enabled` (in-memory flag on the RecheckSweeper)

- **Effect when off:** The C1 re-check ladder does not automatically
  ask trapped people "are you still OK?". Operators can still ask
  manually via `POST /api/admin/recheck`.
- **Currently announces itself?** ✅ Already announced.
  The re-check panel (`refreshState`, dashboard line ~2483 area, batch
  6 wording) prints *"Automatic checks are PAUSED — nobody is being
  asked"* in-panel and highlighted. Complies with the invariant.
- **Note:** not on a top-strip because "paused" is an operator
  decision made deliberately from that same panel, so an operator
  is by construction looking at the announcement when they cause it.

### 5. `device_status.places_enabled` (per device)

- **Effect when False on a device:** That user's saved-place notices
  are not sent to them. Individual, per-device opt-out.
- **Currently announces itself?** ✅ App-side — the mobile settings
  screen shows the toggle state. Not a dashboard-side concern (nobody
  else needs to know a given user turned off their own notices).

### 6. `legacy_token_enabled()` (in `auth.py`)

- **Effect when False:** The old `X-Admin-Token` auth path is refused;
  only JWT sign-in works.
- **Currently announces itself?** ⚠️ Partial. When flipped off, any
  legacy caller gets a 401 with `Not authenticated` — which via the
  A1 Round 1 fix now surfaces the session-expired banner. So the
  human sees "you're signed out" rather than the specific "legacy
  path off" state. Good enough for now (the transitional state is
  not user-facing operational logic), but worth reviewing when the
  legacy path is retired.

### 7. `threshold_set.enabled` (per set inside country_configs)

- **Effect when False:** That specific severity tier's threshold set is
  skipped in `would_have_fired` evaluation. Purely internal.
- **Currently announces itself?** N/A — not user-facing. Would matter
  if we ever expose a "tier off" per-country UI. Note for later.

### 8. `emsc_poller.task` (asyncio task existence)

- **Effect when None or done:** The poll loop is not running; no
  events are ingested; no notifications can fire even if preview is
  on. Silent unless a poller_health row is inspected.
- **Currently announces itself?** ✅ AS OF THIS BATCH.
  The tremor-diagnostics endpoint reports `poller_last_success_at`
  and `poller_last_error`, which the panel renders in plain words
  ("The earthquake feed is being checked normally" vs "Last check-in
  with the earthquake feed failed: …"). Not on the strip, because a
  transient poll failure isn't a standing condition.

---

## What isn't a kill-switch but resembles one

- `is_test_device()` filter — filters test rows out of counts and PDFs.
  Not user-visible as a switch; the invariant doesn't apply. But the
  reports do label the filter honestly ("test entries hidden by
  default"), which is the right instinct in the same family.
- Idle timeout — logs the user out after inactivity. The Round-1
  minimum banner announces it. Round 3's full stale-banner design is
  where this gets its complete treatment.
