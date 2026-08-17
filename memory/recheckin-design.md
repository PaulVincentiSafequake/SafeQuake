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

## Approach I'd propose

### 1. Where the schedule lives: on the phone, mirrored on the server
The ask must survive the app being killed, so the *prompt* has to be a local
notification scheduled on the device (same mechanism as the check-in
reminders, now that B1 has proven it). The server keeps the authoritative
`recheck_schedule` per device so the dashboard can say "next check due
14:20" and so an operator re-check can pre-empt it.

Ladder (tunable per country_config, not hard-coded):

| Since trapped | Interval | Wake-ups |
|---|---|---|
| 0–1 h | 15 min | 4 |
| 1–4 h | 30 min | 6 |
| 4–12 h | 60 min | 8 |
| 12 h+ | 3 h | 8/day |

≈ 26 wake-ups in the first 12 hours against 48 for a flat 15-minute ladder,
and ≈ 8/day after that against 96. Below 20% reported battery every interval
doubles; below 10% it triples and the notification says so ("we'll check less
often to save your battery") — otherwise the app looks like it has forgotten
them.

**Concern to flag now:** iOS gives no reliable open-ended repeating local
notification with a custom interval, so the app must top up the schedule
whenever it runs. If the user never opens the app again, the ladder runs out
after the batch we last scheduled (~24 h at the 3-hour tier). A silent
background push from the server can re-arm it, and that mechanism now exists
(B1's kill switch) — but silent pushes are best-effort by design: Apple
throttles them, and a phone in Low Power Mode may not run them at all. So the
honest position is: **the ladder is reliable for roughly the first day, and
best-effort after that.** I'd rather write that down than let the dashboard
imply a guarantee it can't keep.

### 2. The prompt itself
Critical interruption level (the person is trapped — this is exactly what the
entitlement is for), three full-width buttons, no scrolling, no forms. On iOS
these become notification **actions** on a dedicated `RECHECK` category, so
SAME/WORSE/BETTER can be answered straight from the lock screen without
unlocking the phone or launching the app — one tap, minimal battery, and it
works with a cracked or dusty screen. (B9 has just proven the actions
mechanism; that category must stay separate from both TREMOR_INFO and the
critical alert.)

### 3. What the answers mean
- `SAME` → new `status_events` row, unchanged severity, `kind: "recheck"`.
  Meaningful data, never discarded as "no change" — it proves they're
  conscious and reachable.
- `WORSE` → severity escalates one step (green→yellow→red), row flagged
  `deteriorating: true`, and the dashboard sorts them to the top of their new
  band with a visible "↑ deteriorating" badge. Escalation is one-way per
  event: nothing but an explicit BETTER or an operator action steps it back.
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

## Open questions for Paul
1. **Who may re-triage on WORSE?** My proposal escalates automatically on the
   person's word. Alternative: flag it for the operator and let them move it.
   Automatic is faster and matches "priority that tracks changing condition";
   operator-gated keeps one human in the loop. I'd ship automatic.
2. **Does the first check start on the trapped report, or on the alert?**
   I'd start it on the trapped report — before that we have nothing to
   re-check.
3. **Do we ever stop?** After, say, 72 hours with no contact, does the ladder
   keep running (battery cost, no information) or park with the person still
   listed as unresolved? I'd stop the *asking* and keep the *listing*.
4. **Non-responders**: should they get the ladder too? They never answered
   once, so each wake-up is a guess — but a phone that's alive and silent is
   exactly where a late answer changes the picture most. I'd include them at
   the widest interval.
