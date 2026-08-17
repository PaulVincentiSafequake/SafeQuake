# B5 — Load test plan: 10k–30k alerts (2026-08-17)
# STATUS: stage 1 RUN — results in /app/memory/load-test-results.md.
# Harness: /app/backend/scripts/load_test_seed.py + load_test_measure.py

## Hard safety guarantee #1 — no real push can ever fire
- The seeder writes ONLY to `device_status` / `status_events` via direct DB
  insert (script in /app/backend/scripts/, never over the public API). It NEVER
  touches the push-token collections and NEVER calls any alert-trigger or
  APNs/Expo send path. Pushes are only sent to tokens in the token registry;
  synthetic devices have no tokens, so a real notification is structurally
  impossible — not merely filtered out.
- All runs execute against the PREVIEW environment first. A production-scale
  run (if ever wanted) needs Paul's explicit go-ahead.

## Hard safety guarantee #2 — identifiable + fully clearable (ties into #146)
- Every synthetic row carries `synthetic: true`, `load_test_run_id: "<uuid>"`,
  and device_id prefix `qg-loadtest-`.
- Cleanup: one command deletes by run_id; a dry-run mode counts first.
- Same `synthetic` flag is the recommended fix for #146 (stale test entries):
  dashboard can badge or hide flagged rows.

## Stages and what we measure at each
Stages: 100 → 1,000 → 10,000 → 30,000 (stop at first degradation, report it).
Mixed realism per stage: 20% red / 30% yellow / 30% green / 15% safe / 5%
not-responding; reconfirmation events over a simulated 6-hour window; battery
draining per event; GPS jitter around Malta.

| Surface | Metric | Worry threshold |
|---|---|---|
| GET /api/devices | p95 latency, payload size | > 2 s / > 5 MB |
| Dashboard triage list | render + poll-cycle jank (rescued-collapse #142, sort #143 — never tested past 4 entries) | > 1 s re-render |
| Leaflet map | marker render, pan/zoom FPS | unusable > 3k pins → needs clustering |
| Team report PDF (full) | generation time, page count | > 30 s or timeout |
| Team report PDF (summary) | generation time | > 10 s |
| Audit CSV | generation time, size | > 30 s |
| Mongo | CPU, memory, collection sizes | sustained > 80% |
| Backend | request timeouts, 5xx | any |

## Deliverable
"Where does it break first" report: per surface, the entry count at first
degradation, with numbers — not a pass/fail.
