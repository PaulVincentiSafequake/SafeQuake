# Dashboard + backend push — the board batch (23 Aug 2026)

Items: **#295** the board says whether it is live · **#194** a stale board
admits it · **#296** the annunciator, built to the ISA-18.1 sequence ·
**#297** walking wounded off the working board · **#298** the
ask-to-check-in dialog's false promise.

Phone app: **unchanged**. No new build needed for any of this.

## What ships where

**Backend** (Publish button):
- `board_alarms.py` (new) — what is an alarm, what is only information,
  grouping, acknowledgement, resolution. One place.
- `server.py` — `POST /api/status` raises alarms; `GET /api/devices`
  sweeps for people who asked for help and have gone quiet;
  `GET /api/admin/alarms`; `POST /api/admin/alarms/ack`; rescue and
  take-off-the-board resolve alarms; `/api/audit` gained
  `alarm_acknowledged`; index bootstrap on startup.
- `tests/test_board_alarms_296.py` (new) — 6 tests, all passing against
  the live API.

**Dashboard** (`/app/memory/dashboard_build/index.html` → GitHub → Render):
- The live strip, the stale bar, the frozen-tab gap notice.
- The annunciator strip: count, flashing rows, shapes, Acknowledge,
  mute with a countdown.
- Walking wounded in their own list, with the count on the board.
- Rescued off the map by default, small and grey when shown.
- The "showing less than everything" chip, and filters reset on a new
  alert.
- The rewritten ask-to-check-in dialog.

Also carried in this file from the last round, never pushed: **#278**
(sentence case in the ask history).

## Order

1. **Backend first** — Publish. The old dashboard against the new backend
   is harmless (it simply does not ask for alarms).
2. **Dashboard second** — the new dashboard against the old backend would
   show "We cannot read the alarms right now" for ever, because
   `/api/admin/alarms` would not exist.

```bash
cd /app
git add -A
git commit -m "board: live/stale, the annunciator, walking wounded, honest ask dialog"
git push origin main
```

Render redeploys within ~2 minutes. The push needs a GitHub PAT with
`repo` scope for `PaulVincentiSafequake/SafeQuake` — there is none in this
environment.

## The #295 reproduction, step by step

What the strip at the top of the triage panel should read:

| When | What it says |
|---|---|
| **Before** — page open, nothing happening | `Live — updated 3 seconds ago, at 20:14:32 Malta time.` The number counts 1, 2, 3, 4 and resets. If it climbs past 15 the board is not updating and it says so instead. |
| **During** — you tap "I need help" on the phone | Within 4 seconds: your card appears in the list with nothing touched, and the alarm strip goes red — `1 alarm nobody has acknowledged`, a flashing row reading `NEEDS HELP · Paul · <code> needs help`, and one short chime. Click the page once first, or the browser will not let us make the sound (the strip says so when that is the case). |
| **After** — you press Acknowledge | The sound stops, the flashing stops, the row stays, and it reads `acknowledged by <your email> at 20:15`. It does NOT disappear. It disappears only when that person is marked rescued or deliberately taken off the board. |

Two more worth doing while you are there:

- **Stale:** turn your laptop's wi-fi off for 20 seconds. A red flashing
  bar appears across the top: "The board is not updating. Last real update
  20:14:32 Malta time, 22 seconds ago. Anyone who has reported since then
  is not on this screen." Turn wi-fi back on: it clears itself.
- **Frozen tab:** switch to another tab for a couple of minutes and come
  back. A yellow line appears saying how long the board was not updating,
  instead of quietly catching up.
