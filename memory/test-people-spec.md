# 33 Test People — spec (#229)

**Approved by Paul, 2026-08-19.**

## The rule that matters most

Test people MUST be obviously fake to a human reading the screen. Not
only behind the existing "show test data" checkbox, but visibly marked
wherever they appear. This project has already lost more than a week
to realistic-looking fake data sitting in the trapped list, and the
rule is: if a real operator glances at a row and cannot tell in one
second that it is fake, the seed is wrong.

Structural markers:
- **Name prefix**: every display_name starts with `TEST` (all caps).
- **Rescue code prefix**: every short_code starts with `Z` (real
  codes never begin with Z — reserved).
- `is_test` flag set to `true` in device_status.
- `_test_seed` tag set to `"seeded-33"` so the whole set can be
  removed atomically.
- `synthetic` flag set to `true` (belt and braces with `is_test`).

## Composition — 33 people

| State                                 | How many |
|---------------------------------------|---------:|
| Immediate — seriously injured, cannot move | 3 |
| Serious — hurt but stable                  | 5 |
| Minor  — walking wounded                   | 4 |
| Not responding, phone still alive          | 3 |
| Not responding, phone gone dark            | 2 |
| Rescued                                    | 4 |
| Safe                                       | 12 |
| **Total**                                  | **33** |

## Wait times (Serious group)

- One at 8 hours
- One at 5 hours
- One at 3 hours
- **Two within the last 20 minutes, reported a few seconds apart**
  → these two test the tie-break rule (whichever of arrival order,
  short_code, or device_id the sort uses).

## Battery

- One Immediate at **4%** (the very-low badge).
- One Serious at **9%**.
- One Serious at **11%**, reported at almost the same moment as
  another at **80%** — tests the sort tie-break when two rows have
  wildly different battery levels but matching arrival times.
- The rest: spread 30–95%.
- The two dark phones report nothing (battery_pct = null).

## Location

- Spread across Malta and Gozo.
- **Four at exactly the same address** — tests location grouping
  and is what a collapsed building actually looks like on the map.
- Two on Gozo.
- One with an approximate position (large `accuracy_m`) to test how
  uncertainty is displayed.

## Mobility (Serious group only)

Mix of:
- `can_move` — mobile
- `cannot_get_out` (`needs_extraction`: true) — extraction needed.

The extraction case currently has no test coverage anywhere and is
the one that most changes what a rescue team brings on scene.

## Behaviour

- **One button** in the dashboard admin/diagnostic panel to add all 33.
- **One button** to remove all, with a count confirming.
- Adding them **sends nothing** — no APNs pushes, no sirens, no
  re-check ladder queued. They exist only in the database.
- Wait times set **relative to creation** so they do not decay into
  stale clutter as the fixture ages.

## Data-model tags

Every seeded document (both `device_status` and any `status_events`
we insert) carries:
- `"is_test": true`
- `"synthetic": true`
- `"_test_seed": "seeded-33"`
- `"display_name"` starts with `"TEST "`
- `"short_code"` starts with `"Z"`
