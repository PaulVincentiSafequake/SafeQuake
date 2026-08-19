# Dashboard push — DONE, 2026-06-18

**PUSHED** to `PaulVincentiSafequake/SafeQuake`, branch `main`, path
`backend_dashboard/public/index.html` — commit `c871710073`. Verified by reading
the file back from the repo: it matches the staged copy byte for byte. Paul's
PAT should now be revoked.

## Repo details — RECORD THESE, they were not written down before
- Repo: `PaulVincentiSafequake/SafeQuake` (public, default branch `main`)
- Dashboard file: `backend_dashboard/public/index.html`
- The repo contains only `backend_dashboard/` — `package.json`, `server.js`,
  `usgs_poller.js`, `public/index.html`. `dashboard-v3.js` is already gone, so
  that carried-over cleanup item is closed.
- The app repo (`quake-angel-app`) is a DIFFERENT repo and does not deploy this.

## Two live bugs this push also fixed
1. **The name was printed twice in the map popup** — "ABC12 Anna — Anna". The
   C3 change was hand-made in the repo and added the styled name without
   removing the older `" — " + u.displayName` line. The agent-side copy had it
   right, so this had been live and unnoticed.
2. The popup did not show **"Cannot get out — extraction needed"**, so the
   egress answer was invisible on the map.

Found by diffing the live file against the staged one before pushing, rather
than pushing the staged file over the top of it — the live file was AHEAD on C3,
and a wholesale overwrite would have reverted names on pins.

## The file
`/app/memory/dashboard_build/index.html` — 5,467 lines, committed locally in
`f4279c2`. Replace the live dashboard's index.html with it wholesale; nothing
else changed and there are no new assets.

**Diff for review:** `/app/memory/dashboard-push-2026-06-18.diff`
(624 insertions, 23 deletions, one file, against the previous state `86e96f7`).

**Secret check before push:** clean. No `ADMIN_TRIGGER_PASSWORD`, no admin
token literal, no PAT, no JWT secret, no `.p8` material. The only hit for a
naive secret grep is the base64 Quake Angel logo, which is already public.

## ORDER — backend first, or the page half-works
1. **Publish the backend.** The feed reads the new `/api/audit` re-check event
   kinds and the panel calls `POST /api/admin/recheck`. Push the dashboard
   first and the panel 404s and re-check rows render as ordinary updates.
2. Push this file.
3. Build iOS 1.0.28.

## Commit message

    Batch 6 B6 + C1 phase 2: activity feed grouped by person, operator re-check panel

    - Feed defaults to one row per person, most urgent first: current state,
      plain sentence, update count, battery, map recentre, shared history modal.
      "Every update" keeps the raw log; the choice is remembered in localStorage.
    - Every state carries a shape and a word as well as a colour (greyscale and
      colour-blind safe). Wire values like not_responding are gone from both
      views. Timestamps read "14 minutes ago · 13:05 UTC".
    - New re-check panel above the feed: ladder state in plain words, two-step
      ask (the preview states the battery cost before anything is sent),
      pause/resume with an admin-only message.
    - Requires the backend published first (/api/audit re-check event kinds,
      POST /api/admin/recheck).

## What changed, by region of the file

| Where | Change |
|---|---|
| `<style>` after `#qg-audit code` | B6 styles: `.qg-feed-modes`, `.qg-person-row` (+ 6 state classes), `.qg-state-chip`, `.qg-person-flag` |
| new `<style>` + `<div id="qg-recheck">` + its `<script>`, immediately above `<div id="qg-audit">` | C1 phase 2 operator panel |
| `#qg-audit` header | "By person" / "Every update" toggle buttons |
| `formatEvent()` | rows lead with a plain sentence; `📱 UPDATE` / `🔁 RE-CHECK`; `whenWords()` timestamps; `sevWord()` fallback prints IMMEDIATE/SERIOUS/MINOR, never `red`/`yellow`/`green`; rescued and reverted rows no longer print a raw status |
| new block after `formatEvent()` | `SHAPE`, `stateOf`, `stateChip`, `ANSWER_WORDS`, `eventSentence`, `feedAgoWords`, `personRow`, `groupByPerson`, `renderPersonFeed`, delegated history click, mode buttons |
| `refresh()` | renders `renderPersonFeed(events)` in person mode, the old log otherwise |
| near `openHistoryModal` | `window.qgOpenHistory = openHistoryModal;` so the feed and the triage cards share ONE history modal |

## How it was verified without the live site
Both feed views and the panel's full preview → confirm → result flow were
rendered in a local harness that slices the real `<style>`/`<script>` blocks out
of this file and stubs `qaApi` / `qaAuth`, so the code under test is the code
being pushed. All 9 script blocks pass `node --check`. Static guards live in
`backend/tests/test_b6_activity_feed.py` (10) and
`backend/tests/test_c1_phase2_manual_recheck.py` (11) — they fail if the person
grouping, the shape+word rule, the plain wording, the shared history modal or
the preview-before-send order is ever removed.

## Still owed at the repo (carried over, not done here)
- `dashboard-v3.js` should be DELETED from the repo — it was folded inline and
  its duplicate audit widget double-polled `#qg-audit-body`.
- Cache-Control headers (item 1.1) — needs the repo's server config, which has
  never been visible from inside the pod.
