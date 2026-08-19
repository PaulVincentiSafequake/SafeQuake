# Dashboard push — Batch 7 Round 3 (final version, ready to push)

Prepared: 2026-08-19

## Files changed (dashboard only)

- `backend_dashboard/public/index.html` ← mirrored from
  `/app/memory/dashboard_build/index.html`.

## What's in this push

### #230 — Stale-session banner is no longer dismissible
Dismiss button removed. Replaced with a "Sign in again" button that
either re-runs the sign-in flow or reloads the page.
`aria-live="assertive"` so assistive tech announces it over any
in-progress reading. (Round 2)

### #231a — Grammar helper: "1 person currently needs help"
Same singular/plural pattern the reports use — `needs` for one,
`need` for many. (Round 2)

### #228 — Sweep "device" → "person"/"people" (user-facing)
Places found and changed (dashboard):
- Header subtitle: "Live device data" → "Live data on people who
  have checked in".
- Search field placeholder: "Search code, name or device ID…" →
  "Search by name or rescue code…".
- Team report size dropdown: "Summary only — no per-device table" →
  "Summary only — totals". "Full detail — with per-device table" →
  "Full detail — one row per person". Tooltip updated to match.
- Trigger-alert audit line: "N device(s)" → "N person / N people".
- Mark-rescued modal error: "Device is not currently rescued" →
  "This person is not currently marked as rescued".
- Admin section blurb (test-tools panel): "Test devices" → "Test people".

Legit device-level uses (battery reads, iOS device enrolment for
preview mode, "device_id" as a technical identifier in exports) kept
as-is. Full list: 5 user-facing surfaces changed on the dashboard.
App-side: 0 changes required (only "Device identity" remains, in
`/diag`, which is legitimately about the phone hardware).

### #231b — Re-check refusal branches by real reason
Old: one catch-all sentence. New:
- Nobody trapped at all → "Nobody needs help right now — there is
  nobody to ask."
- Someone trapped but unreachable → "1 person needs help, but their
  phone has gone dark — we cannot reach them to ask." (plural
  handled).
- Rare severity-filter-empty corner case → "No one matches this
  filter right now."
Backend was extended to return `trapped_total` and `unreachable` on
the preview response so the dashboard can branch honestly.

### #213 — "See on map" reads as a control
`.qg-maplink` now renders as a filled button (blue background, white
text, bold, cursor: pointer) with hover, active/pressed and
`focus-visible` states. Label changed from "📍 map" to
"📍 See on map" so it reads as an action. Applied at all three
audit-row/history-modal render sites.

### #232 — "Who can use this dashboard"
- Panel title "Operators & Access" → "Who can use this dashboard".
- Subtitle rewritten in plain language ("Choose who can sign in,
  and what they're allowed to do…").
- Role explainer added ABOVE the add-form so the person adding
  someone reads what each role does before choosing. Framing lock
  held: an OPERATOR is at a desk, not on the ground.
- Placeholder: "new.operator@example.com" → "their email address
  (e.g. name@example.com)".
- Button: "Add operator" → "Add person".
- Columns: Email → Person · Role → What they can do · Expires →
  Access ends · Last login → Last signed in.

### #229 — 33 test people (add / clear in one click)
Two buttons added to the "Admin testing tools" panel:
- "Add 33 test people" — calls `POST /api/admin/test-people/seed`.
- "Remove all test people" — calls `POST /api/admin/test-people/clear`.
Every seeded row is TEST-prefixed (display_name starts with
"TEST Person NN") and has a Z-prefixed rescue code (the last five
chars of the seeded device_id are engineered so the derived
short_code always starts with Z). Neither button queues any push or
schedules any re-check. Full spec in `/app/memory/test-people-spec.md`.

## How to push (when the PAT arrives)

```bash
cd /tmp && rm -rf sq-tmp && \
  git clone --depth 1 https://x-access-token:$PAT@github.com/PaulVincentiSafequake/SafeQuake sq-tmp && \
  cd sq-tmp && \
  cp /app/memory/dashboard_build/index.html backend_dashboard/public/index.html && \
  git add backend_dashboard/public/index.html && \
  git -c user.email=agent@quakeangel.app -c user.name="Quake Angel Agent" \
    commit -m "Batch 7 R3: wording pack, #232 Who-can-use, #229 test-people seed, #213 See-on-map, #231b branched refusal" && \
  git push origin main
```

Render will redeploy within ~2 minutes.
