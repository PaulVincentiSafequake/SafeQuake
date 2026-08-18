# Swarm grouping for informational tremor notices — spec
2026-06-18 · DESIGN ONLY, nothing built. For Paul's approval.

## The problem, precisely
On 18 Aug two notices arrived minutes apart: M3.7 at 251 km and M3.3 at 249 km,
depths 9 km and 10 km. They looked like one earthquake reported twice. They were
not — two different EMSC events (`20260818_0000267`, `20260818_0000277`), origin
times 34 s apart, epicentres ~2 km apart. A genuine swarm.

Revision handling already exists and is a different thing: when EMSC republishes
the SAME event with a refined magnitude we either suppress the second notice or
label it "Updated seismic reading" (`emsc/preview.py`). That does nothing for
this case, because these were two real tremors.

So: a swarm produces several honest notifications that a reader reasonably
totals up as several separate events "happening", and the feed starts to feel
like noise. Noise is the thing that gets tremor notices switched off, and a user
with notices switched off has been trained to ignore the channel we need in a
real earthquake.

## Paul's three conditions (2026-06-18), taken as hard requirements
1. **One notification that updates its count, never a stack of new ones.**
2. **A genuinely significant event must never be buried inside a group** — if
   one is much larger it stands alone, or headlines the group.
3. **Grouping never applies to the critical earthquake alert.**

## Proposed design

### 1. What counts as the same swarm
A new event joins an open swarm when ALL of:
- within **50 km** of the swarm's centroid,
- within **60 minutes** of the swarm's most recent member,
- depth within **±20 km** of the centroid depth.

The swarm closes 60 minutes after its last member. Values live in
`country_config.swarm_grouping` so they are tunable without an app release,
exactly like the re-check ladder.

Rationale for 50 km: it is comfortably wider than the epicentral scatter of a
single sequence (the 18 Aug pair was 2 km apart) and comfortably narrower than
"two unrelated Mediterranean regions", so a Sicilian sequence can never absorb
an Algerian event.

### 2. One notification that updates in place
- The first member sends a normal notice.
- Each subsequent member **replaces** it rather than adding one:
  - **iOS:** `apns-collapse-id: swarm-<swarm_id>`. iOS replaces the existing
    notification in Notification Center with the new content. One row on the
    lock screen, updating.
  - **Android:** the same `swarm_id` as the notification tag/id, which replaces
    rather than stacks.
- Body text carries the count and the largest so far:
  > `4 tremors near Sicily in the last 25 minutes. Largest M3.7, 251 km from you, depth 9 km. Source: EMSC.`
- Rate limit stays per device as today; an update never bypasses it, so a
  50-event swarm cannot produce 50 wake-ups.

### 3. The stand-alone rule (condition 2)
A member is sent as **its own notification**, outside the group, when either:
- its magnitude is **≥ 0.8 above** the largest so far in the swarm, or
- it crosses an **intensity tier boundary** the swarm has not yet crossed
  (once #106 intensity thresholds land — that is the better trigger and should
  replace the magnitude rule when available).

Wording makes the escalation explicit rather than resetting the count:
> `Larger tremor near Sicily: M4.6, 240 km from you, depth 8 km. Source: EMSC.`

The swarm then re-bases on the larger event, so subsequent smaller members
group under the new headline instead of re-escalating.

### 4. Critical alerts are untouched (condition 3)
Grouping lives entirely inside the informational dispatch path
(`dispatch_preview_if_needed` / the future production tremor path). The critical
path (`send_critical_alerts`) never reads swarm state, never sets a collapse-id,
and never suppresses a send. A test asserts the critical payload carries no
collapse-id and that the swarm code is not reachable from the alert path — the
same structural separation that keeps the tremor notification category out of
the critical alert.

### 5. What gets stored
New collection `tremor_swarms`:
```
{ swarm_id, country_code, opened_at, last_event_at, centroid: {lat, lon, depth_km},
  member_count, max_magnitude, max_event_id, notified_device_ids: [...],
  headline_event_id, closed_at }
```
Every send and every suppression continues to write to
`emsc_preview_notifications` with `swarm_id` and either
`grouped_into_existing` or `sent_standalone_larger`, so a day-14 review can ask
"how many notices did grouping remove, and did it ever hide something it
shouldn't have?" — which is the only way to know the feature is safe.

### 6. Open questions for Paul
1. **Count wording.** "4 tremors near Sicily in the last 25 minutes" — or name
   the region from the EMSC region string verbatim, which is sometimes
   technical ("Sicily, Italy" is fine; "Strait of Sicily" less so)?
2. **Does a grouped update make a sound?** My recommendation: the first notice
   sounds, updates are **silent** (`sound: null`, content updates in place).
   A swarm that pings every four minutes is the noise problem with extra steps.
   A stand-alone larger event always sounds.
3. **Is 0.8 the right stand-alone gap** before intensity tiers land, or would
   you rather it were 0.5 (more stand-alones, fewer things buried)?

## Status
§6 ANSWERED by Paul, 2026-06-18 — build accordingly:
1. **Region wording:** use the EMSC region string **verbatim** when every member
   of the group shares the same one. Fall back to our own broader label ONLY
   when they differ (e.g. "SICILY, ITALY" + "STRAIT OF SICILY"). EMSC is
   authoritative and we must not introduce errors into it; our own wording is
   the exception, not the default.
2. **Sound:** first notice sounds, updates silent, a stand-alone larger event
   always sounds. Sound means "something new that matters".
3. **Stand-alone gap: 0.5, not 0.8.** Magnitude is logarithmic — +0.5 is about
   five times the energy, +0.8 about sixteen. The failure modes are not equal:
   a needless notification costs a moment's attention, burying a significant
   quake costs more. Err toward showing.

NOT BUILT YET. Depends on nothing else. Build TOGETHER with D1 (2026-06-18):
D1 is "name the region in tremor notices" — the same region-label rule as §6.1,
applied to single notices as well as grouped ones. Region is ADDED to the
notice, never substituted for the distance.
