# B5 — Load test results

Harness: `/app/backend/scripts/load_test_seed.py` (seed/count/clear) +
`/app/backend/scripts/load_test_measure.py` (read-only timing, median of 3).
Environment: PREVIEW backend (localhost:8001) + preview Mongo. No push tokens
touched, no alert path called — synthetic devices have no token, so a real
notification is structurally impossible.

## Stage 1 — 100 devices (run 2026-06, run_id 7d808608)
Seeded: 100 `device_status` rows + 240 `status_events` (1–4 check-ins each over a
6-hour window, battery drain + GPS jitter around Malta). Mix: 20% red / 30%
yellow / 30% green / 15% safe / 5% not-responding.

| Surface | Baseline (14 real rows) | Stage 1 (114 rows) | Worry threshold | Verdict |
|---|---|---|---|---|
| GET /api/devices | 0.00 s / 6.3 KB | **0.01 s / 52.8 KB** | > 2 s / > 5 MB | fine |
| GET /api/public/summary | 0.00 s | **0.00 s / 0.2 KB** | — | fine |
| Team report PDF (summary) | 0.02 s | **0.02 s / 8.4 KB** | > 10 s | fine |
| Team report PDF (full) | 0.02 s | **0.20 s / 28.1 KB** | > 30 s | fine |
| Safe-to-share PDF | 0.02 s | **0.02 s / 7.8 KB** | — | fine |
| Audit CSV | 0.00 s | **0.01 s / 42.9 KB** | > 30 s | fine |
| Audit PDF | 0.02 s | **0.21 s / 34.0 KB** | > 30 s | fine |

Nothing degraded. Scaling read from stage 1: `/api/devices` costs ~0.46 KB per
device, so 30k devices ≈ **14 MB payload** — that alone will breach the 5 MB
worry line well before latency does. Expect pagination/field-trimming to be the
first required change, not query speed.

Cleanup verified: dry-run counted 100/240, delete removed exactly 100/240, and
the 14 real rows were untouched.

## Not measured at stage 1 (deliberate)
Dashboard triage-list render, Leaflet marker/pan FPS and Mongo CPU are only
meaningful at 10k+. They also need the dashboard pointed at the preview backend
(it currently points at production), so they are folded into stage 3.

## Next
Stage 2 (1,000) → stage 3 (10,000) → stage 4 (30,000), stopping at the first
degradation. Same commands, `--count` changed.
