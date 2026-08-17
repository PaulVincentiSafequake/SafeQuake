# C1 — Periodic re-check-in and operator re-check: design
2026-08-17 · DESIGN ONLY, nothing built. For Paul's review.

## Current state (confirmed by reading the code, not assumed)
Nothing re-checks on anyone. `status_events` is append-only and every POST
/api/status is recorded, but nothing ever *asks* again. A person who reports
trapped, or who never answers, keeps that status forever — the dashboard's
`trapped_since` just keeps counting up. There is no scheduler, no re-ping, no
staleness concept beyond the `is_stale` flag (>30 min silent) added for the
per-person history modal in batch 4.

## Agreed shape (Paul's, restated so we're working from the same spec)
Battery is the constraint that decides everything. Widening intervals:
first hour every 15 min → next few hours every 30 min → then hourly → then
every few hours, stretched further when reported battery is already low.
One-tap response: SAME · WORSE · BETTER, nothing else. "Worse" escalates
visibly. Silence is information, and its two kinds must look different.
Never auto-downgrade. Operator-initiated re-check as well as automatic,
targetable, defaulting to everyone unresolved, with the battery cost shown.
Log every check, response and non-response.

## Approach — REVISED 2026-08-17 after Paul's challenge

### 1. Where the schedule lives: ON THE SERVER (changed)
My first draft scheduled the ladder as local notifications on the phone. Paul
asked why, given the server already knows who is trapped. **He's right and I've
changed it.** Server-driven re-checks are strictly better on every axis that
matters here:

- **Not throttled.** An ordinary visible alert push at priority 10 is delivered
  normally; the throttling and Low-Power-Mode restrictions I flagged apply to
  *silent* (`content-available`) pushes, which is what my local-schedule top-up
  would have depended on. Designing around a limitation I'd just invented was
  the wrong instinct.
- **No dependency on the app having run recently.** A local schedule only
  exists because the app was open at some point; a push needs nothing.
- **Operator control of timing**, which is half of what C1 asks for. An
  operator can bring a check forward, delay one, stop and restart the ladder —
  impossible if the schedule is baked into 8 pre-armed local notifications.
- **The ladder is tunable server-side**, so changing 15/30/60 minutes never
  needs an app release.
- **The audit record is written where the decision is made.** "Check sent at
  14:20" is a server fact, not a claim we have to trust the phone for.
- Battery cost is identical: one wake-up either way.

**The one thing local scheduling does better, and how we keep it.** A local
notification fires with no connectivity. A push does not. But if the network is
down, the person's *answer* can't reach us either — so a re-check they can't
answer buys nothing at the moment it fires. What it buys is later: an answer
tapped offline and delivered when signal returns is real information with a real
timestamp. So:

- **Primary: server-driven visible pushes.** A due-check sweeper on the backend
  (same pattern as the existing testimonies sweeper) picks up everyone whose
  next check is due and sends.
- **Safety net: one local notification, armed a ladder-step ahead**, cancelled
  and re-armed every time a server check arrives. If the server's checks stop
  reaching the phone, the person still gets asked at the widest interval.
- **Answers queue on the device** (AsyncStorage) and flush on reconnect, tagged
  with the time they were actually tapped, never the time they arrived. A
  trapped person's phone should never lose an answer because the tower was down.

### 1a. The ladder itself
Tunable per country_config, not hard-coded, and now enforced server-side:

| Since trapped | Interval | Wake-ups |
|---|---|---|
| 0–1 h | 15 min | 4 |
| 1–4 h | 30 min | 6 |
| 4–12 h | 60 min | 8 |
| 12 h+ | 3 h | 8/day |

≈ 26 wake-ups in the first 12 hours against 48 for a flat 15-minute ladder, and
≈ 8/day after that against 96. Below 20% reported battery every interval
doubles; below 10% it triples and the prompt says so ("we'll check less often to
save your battery") — otherwise the app looks like it has forgotten them. The
multipliers need tuning against real battery measurements, not guesses.

### 1b. What interruption level to use
Not the 30-second siren — that's for "an earthquake is happening now". Re-checks
go out as **critical-level with a short sound**: critical guarantees delivery
through Do Not Disturb and a locked, face-down phone, which is exactly the
situation, without turning every check into an emergency. Worth flagging that
Apple reviews critical-alert use, so the justification needs writing down:
these go only to people who have self-reported as trapped.

### 2. The prompt itself
Four full-width buttons, no scrolling, no forms: **SAME · WORSE · MUCH WORSE ·
BETTER**. On iOS these are notification **actions** on a dedicated `RECHECK`
category, so any of them can be answered straight from the lock screen without
unlocking the phone or launching the app — one tap, minimal battery, and it
works with a cracked or dusty screen. (B9 has just proven the actions mechanism;
that category must stay separate from both TREMOR_INFO and the critical alert,
for the same reason.)

### 3. What the answers mean
- `SAME` → new `status_events` row, unchanged severity, `kind: "recheck"`.
  Meaningful data, never discarded as "no change" — it proves they're
  conscious and reachable.
- `WORSE` → escalates and flags `deteriorating: true`; the dashboard sorts them
  to the top of their new band with a visible "↑ deteriorating" badge.
  **Escalation is NOT forced stepwise (Paul, 2026-08-17): green straight to red
  must be possible.** A single WORSE button can't express how much worse, so the
  notification carries four actions instead of three:
  `SAME · WORSE · MUCH WORSE · BETTER`. WORSE moves one band; **MUCH WORSE goes
  straight to red** whatever they were before. Both are still one tap, and iOS
  shows all four on expand. In-app the same four buttons, full width.
  Escalation is one-way per event: nothing but an explicit BETTER or an operator
  action steps it back.
- `BETTER` → recorded, severity **not** automatically reduced. Improvement is
  self-reported and easy to be wrong about (adrenaline, shock). It shows as
  "reports improving" on the card and an operator may re-triage. This is the
  asymmetry I'd argue for: we escalate on the person's word, we de-escalate
  only on a human decision.

### 4. Silence: two states, never one
Derived server-side, from `device_status.updated_at` plus whether anything at
all (including background battery pings) has arrived:

- **`silent_alive`** — no answer to N consecutive checks, but the phone is
  still reporting. Possibly unconscious, possibly can't reach the phone,
  possibly a phone under debris with the person elsewhere. Stays at current
  priority; dashboard shows "no answer · phone alive · battery 34%".
- **`dark`** — nothing at all for > 45 min (2× the longest expected ping).
  No contact possible. Dashboard shows "phone dark since 14:02 · last known
  status + location" and keeps the last known values pinned rather than
  blanking them.

Neither reduces priority. Both need their own dashboard treatment (I'd use a
hollow/hatched marker for `dark`, since "we don't know" should not look like
"we know they're fine").

### 5. Operator re-check
`POST /api/admin/recheck` with a target selector: individual, severity band,
non-responders, or zone (once B4 zones exist). Default target: **everyone
unresolved** — agreed, and for the reason Paul gave: reds are already top
priority and already visible, so the highest-value new information comes from
yellows who may be deteriorating and from non-responders where any answer at
all changes the picture.

The confirm dialog states the cost before sending, computed from live data:
"This will wake 12 phones. 3 are below 20% battery." Soft rate limit — one
broadcast re-check per 10 minutes, overridable with a typed reason that lands
in the audit log. Not a hard block: the operator with a team at the door
knows something the rate limiter doesn't.

### 6. Pair it with "help is on the way"
Agreed, and I'd go further: a re-check that carries reassurance costs exactly
the same single wake-up as one that doesn't, so the default operator flow
should offer it. Optional line appended to the prompt: "A team has been
assigned to your area." Only when true — it must be tied to a real zone
assignment (B4), never a free-text morale message, or it becomes a promise
nobody can audit.

### 7. Logging
Every check sent, every answer, every non-response gets a `status_events` row
with timestamp, battery and location, plus `kind` ∈
`recheck_sent | recheck_answered | recheck_missed`. Non-responses are written
by the server when the window expires, so "we asked and heard nothing" is a
positive fact in the record rather than an absence of one. The per-person
history modal and the audit CSV/PDF already render everything in
`status_events`, so both surfaces pick this up for free.

## Decisions (all four answered 2026-08-17)
1. **Who may re-triage on WORSE? — ANSWERED (Paul).** Automatic escalation on
   the person's word; no auto-downgrade on BETTER; and not forced stepwise —
   hence the MUCH WORSE action above.
2. **Start on the trapped report, not the alert — ANSWERED (Paul, agreed).**
3. **When do we stop asking? — ANSWERED (Paul).** Not on a flat timer. Stop
   when the phone has **gone dark**, because asking a dead phone achieves
   nothing. While the phone is still **alive**, keep asking at the widest
   interval however long it takes — an answer is still possible, and that is
   the whole point. An operator can restart the ladder by hand (e.g. after a
   phone comes back). So the stop condition is a state, not a clock.
4. **Non-responders get the ladder too — ANSWERED (Paul, agreed)**, at the
   widest interval.

## Status
All four questions answered; architecture revised to server-driven. Remaining
unknowns are implementation-level (sweeper cadence, exact battery multipliers,
critical-alert justification wording) and don't need a decision from Paul before
building. NOT STARTED — waiting for the go-ahead.
