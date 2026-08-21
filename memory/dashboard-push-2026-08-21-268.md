# Dashboard push — #268 (the phantom casualty)

Prepared 2026-08-21. App version unchanged: **v1.0.40, build 40** — no
mobile code changed. Backend + dashboard only.

## What ships

**Backend** (deploys with Paul's Publish button):
- `record_state.py`, `duplicates.py` (new), `people_counts.py`,
  `server.py`, `apns.py`, `reports_export.py`.
- `/api/devices` now returns the working board in `devices` and everything
  deliberately not on it in `off_board`, plus `notices`, `counts` and
  `count_notes`.

**Dashboard** (`/app/memory/dashboard_build/index.html`, deploys via
GitHub → Render):
- Four plain-English silence labels on every card, with the real elapsed
  time and a held-reason line whenever a device signal was overridden.
- Duplicate banner with the evidence and Confirm / Reject buttons.
- Count provenance in words under the numbers.
- Mass-dark notice above the list.
- Collapsible "Not on the working board (N)" area with Put-back.

## Order matters

Push the **backend first**, then the dashboard. The old dashboard against
the new backend is safe (it simply stops showing removed records, which
is the fix). The new dashboard against the old backend would show empty
state lines, because `record_state` would be missing.

## Deploy

1. Backend: Publish button, top right of Emergent.
2. Dashboard:

```bash
cd /app
git add -A
git commit -m "#268: four kinds of silence, duplicate flagging, honest counts"
git push origin main
```

Render redeploys within ~2 minutes. The push needs a GitHub PAT with
`repo` scope for `PaulVincentiSafequake/SafeQuake` — there is none in this
environment, so either paste one in chat or run the push locally.

## How Paul checks it himself

Everything below is on the live dashboard, no test harness.

### 1. See the phantom come off the board
- Open the dashboard and sign in.
- `F6XJY` should no longer be in the working list.
- Scroll to the bottom of the triage panel, open **"Not on the working
  board (N)"**. `F6XJY` is there, labelled **"App removed from this
  phone"**, with the time Apple reported it and the words "This is a
  deleted app, not a missing person."
- The count provenance under the numbers should say, in words, that "not
  responding" does not include it.

### 2. Reproduce an "App removed from this phone" from scratch
1. On a spare iPhone (or your own), install and open the app once so it
   registers and checks in. Note its rescue code on the board.
2. **Delete the app** from that phone. Do not just close it.
3. Trigger one alert from the dashboard (Test trigger is enough). Apple
   only tells us the app is gone when we try to push to it — so the state
   cannot appear until a push is attempted.
4. Refresh the board. Within one poll the record moves into "Not on the
   working board", labelled "App removed from this phone".
   - **If an alert is still live** it stays in the working list instead,
     with a yellow line saying it is being kept there deliberately and is
     not counted as not responding. Call the alert off (the "Call alert
     off" button) and refresh, and it moves.

### 3. Reproduce a "Phone went dark"
1. On a phone that has checked in, turn on **Airplane Mode** (or power the
   phone off).
2. Wait 45 minutes — 15 minutes if that person's last report was trapped
   or injured.
3. The card reads **"Phone went dark"** with the real elapsed time, and it
   stays in the working list. That is the highest-concern state and
   nothing moves it automatically.
4. Turn Airplane Mode off, open the app so it checks in, and the line
   clears on the next refresh.

### 4. Reproduce a duplicate pair
1. Delete the app from a phone that has already checked in.
2. Reinstall it and check in again within half an hour, with the same
   first name and from roughly the same place.
3. Both records now carry: **"This may be the same person as [code]"**
   with the evidence (same first name, positions N m apart, the old record
   went quiet N minutes before the new one appeared).
4. Press **"Yes — same person"**. The older record moves off the working
   board with your name and the reason on it. Press **"No — two different
   people"** and the suggestion stops coming back. Neither action merges
   or deletes anything, and both are recorded.
5. Anything you moved can be put back from the off-board area.

### 5. Check "Never used the app"
Any phone that registered for alerts and never opened the app appears in
the off-board area labelled "Never used the app", and the count line says
how many phones received the alert with no location for them.

### 6. Check the printed and exported reports
- **Team report (B1)**: the summary table now has indented lines for
  "waiting for an answer" and "phone went dark", and separate lines for
  the records not on the working board; underneath, sentences saying what
  each number leaves out; at the end, a **"Records not on the working
  board"** appendix with code, name, what it is, when it moved, who moved
  it and why.
- **Public report (B2)**: still one page, with one sentence saying the
  numbers cover the working board only and how many records are set aside.
- **Audit CSV**: the metadata block carries all the counts, the
  exclusion sentences, and one `not_on_working_board_record` row per
  set-aside record.

## Things Paul asked to be told plainly

- **Android can never say "app removed".** Only Apple reports a token as
  unregistered per device; the Android relay reports batch-level status
  only. An Android phone with the app deleted will read "Phone went dark"
  for ever. If that matters for the Malta group, the options are a
  server-side FCM integration (real work) or an operator resolving the
  record by hand with a reason (available today).
- **`Unregistered` is not proof of intent.** It also fires on a wiped or
  restored phone. It is reported as "the phone told us the app is gone",
  never as "they deleted it".
- **A phone destroyed in the earthquake reads "Phone went dark"**, not
  "removed", because Apple gives us no signal at all in that case.
