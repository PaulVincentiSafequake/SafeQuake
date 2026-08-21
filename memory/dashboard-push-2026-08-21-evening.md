# Dashboard push — #274, #271, #272, #270 (21 Aug 2026, evening)

App version unchanged in numbering; the phone app DID change (calm check-in
screen + tap route), so a new build is needed for #271 on the phone.

## What ships

**Backend** (deploys with the Publish button):
- `server.py` — `_stand_down_split`, the stand-down preview + POST split,
  `_ask_state`, the reworked `ask-to-check-in`, `ask_state` on `/api/devices`.
- `apns.py` — `_build_check_in_request_payload` + `send_check_in_request`
  (ordinary notification: default sound, `active`, priority 5).
- `people_counts.py` — `asks` added to the projection, so the classifier can
  finally see what we asked (this is what makes "not asked since" honest).
- `reports_export.py` — Malta time on every human-readable timestamp, offset
  printed on legal records, CSV `times_note`.
- `timefmt.py` — unchanged this round, but it is the authority.

**Phone app** (needs a new build):
- `app/alert.tsx` — `checkin=1` calm mode.
- `app/_layout.tsx` — `kind: "check_in_request"` tap route.
- `app/quake/[unid].tsx`, `src/utils/time.ts` — "Time (Malta)".

**Dashboard** (`/app/memory/dashboard_build/index.html`, deploys via
GitHub → Render):
- "Ask them to check in" on every card, with the ask history under it and a
  greyed-out state that says why.
- Stand-down dialog lists every person still asking for help by name, code,
  how bad, when last heard and battery.
- All dialogs are ours now, in short lines. `window.qgAsk` / `confirmPlain`.
- Every time on the page is Malta time (`window.qgTime` / `qgWhen`), with the
  offset on the audit feed and the full-history modal (`qgWhenLegal`).

## Order

Backend first, then the dashboard. The old dashboard against the new backend
is safe (it simply ignores the new fields). The new dashboard against the old
backend would show "Not asked yet." on every card and an empty staying list on
the stand-down dialog — misleading, so do not do it that way round.

## Deploy

1. Backend + app: Publish button, top right of Emergent. Then generate a new
   iOS build for the check-in screen.
2. Dashboard:

```bash
cd /app
git push origin main
```

Render redeploys within ~2 minutes. Needs a GitHub PAT with `repo` scope for
`PaulVincentiSafequake/SafeQuake`.

## How Paul checks it himself

1. **Stand-down.** With at least one person on the board asking for help,
   press "Call the alert off". The dialog must name them — code, how bad, when
   last heard, battery — and say their phone is NOT being told. Confirm; the
   banner says how many were told and how many stay.
2. **Ask them to check in.** Press it on a quiet card. Read the dialog: it
   quotes the exact words the person will see, warns about their battery, and
   says your name and the time go on the record. After sending, the card says
   "Asked once. Last asked less than a minute ago, no answer." and the button
   greys out with "Asked less than a minute ago. Wait 60 minutes."
3. **The notification.** On the phone: title "Are you all right?", body
   "No new earthquake. Please tap to tell us how you are." No siren. Tap it:
   blue screen, "No new earthquake." first, then "Are you all right?", then
   I'M SAFE / I NEED HELP. Tap I NEED HELP and the report appears on the board
   like any other.
4. **Times.** Any card, any audit row, any PDF: one time, in Malta time, and
   the audit rows carry "(Malta time, UTC+02:00)". Export the CSV: the file
   name still ends in `Z`, the `at` column is the exact instant with its
   offset, `at_simple` is Malta time.
5. **Dialogs.** No grey browser box with a web address in it anywhere.
