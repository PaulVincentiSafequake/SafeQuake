# Email to support@emergent.sh — QuakeAngel migration & reliability

**Draft prepared 2026-08-06. Copy-paste from your own address.**

Recommended send: from `paul@karenvincenti.com` (or whichever address is on the Emergent account) to `support@emergent.sh`. Add a specific subject line so it's easy for support to route.

---

## Subject

> QuakeAngel (safety-critical) — migration/export questions + reliability escalation before paid launch

## Body

Hi Emergent support,

I'm about to accept paying subscribers on QuakeAngel — a safety-critical earthquake-alert app for Malta that pushes iOS Critical Alerts (siren + drop-cover-hold) when the seismic monitoring pipeline flags a dangerous event. Because this is a "when the phone rings, someone runs to safety" product, I need higher confidence in backend reliability than the standard balance-based tier gives me, and I need clarity on how integrations behave if I have to move.

Before I go live I have five concrete questions. If any of these have a documented answer I've missed, a link is perfect — I've read the current docs and only found "Save to GitHub" as an export path.

### 1. Reliability / SLA escalation — the primary ask

The "balance runs out → pod suspends silently" model is my #1 blocker for taking payment. If a suspension happened during a live seismic event I would ship a false-negative to end-users who trusted the app to wake their phone.

- Do you offer a paid SLA tier (enterprise / dedicated / reserved-instance) with:
  - Explicit uptime commitment (99.9% or better),
  - Written escalation contact and response SLA,
  - Advance notice of any billing/balance action that would take the pod offline (email X hours ahead, not silent),
  - Dedicated compute so I'm not sharing kernel with the free tier?
- If so — pricing sheet + how to sign up? I'm willing to prepay 12 months to lock this in.
- If not — is there a roadmap ETA I should wait for, or should I plan on migrating?

### 2. MongoDB data export

"Save to GitHub" covers code but not data. The QuakeAngel Mongo instance has several collections that need to survive any migration (audit log, EMSC continuity tracking, entitlements, device push tokens):

- Is there an official export path (mongodump credentials, backup snapshot, S3 dump) for the Emergent-hosted Mongo? Or do I need to run my own `mongodump` from inside the pod against `$MONGO_URL`?
- If self-service via the pod: any size/rate limits I should know about? The soak dataset is small (single-digit GB) but I don't want to trip a throttle mid-dump.
- Can I take a live consistent snapshot without freezing writes, or do I need a maintenance window?

### 3. Integration continuity if the backend moves off Emergent

I may need to point the FastAPI backend at Fly.io (Frankfurt) while keeping the mobile deploy on Emergent. Three integrations I need behaviour clarity on:

- **Emergent LLM key (universal key for OpenAI/Anthropic/Gemini)** — does this key work when the calling backend is on Fly.io, or is it network-scoped to Emergent-hosted requests? If scoped, what's the migration path — do I switch to direct OpenAI/Anthropic/Gemini keys, or does Emergent issue a "portable" universal key?
- **Emergent Push Notifications relay** — Android push tokens my backend already holds: will they keep working if I POST to the relay endpoint from Fly.io instead of from an Emergent pod? Any auth/allowlist implications?
- **Emergent-managed Resend integration** — same question. Does it work from any host, or is it Emergent-network-only?

For each: if it stops working off-platform, I need to know now so I can budget for a direct-vendor key and re-integrate before cutover.

### 4. Mobile app + App Store review implications

Current setup: Expo mobile deploy via Emergent, iOS TestFlight builds working, `EXPO_PUBLIC_BACKEND_URL` points at the Emergent-hosted FastAPI.

If I move the backend to Fly.io and update `EXPO_PUBLIC_BACKEND_URL` in the mobile app then re-publish + rebuild:

- Any conflict / gotcha with mobile-on-Emergent + backend-off-Emergent? (Assuming CORS is configured correctly on the Fly.io side.)
- Does Apple treat a backend-URL change as a new binary requiring full re-review, or is it metadata-only? This matters because I'm mid-soak on the seismic monitoring — a review gap of 3-7 days would break the continuity data.
- Do you have a recommended way to test the mobile app against a swapped backend URL BEFORE promoting to production TestFlight? (Ideally: two separate mobile app bundles pointing at prod and dev backend, so I can validate the Fly.io backend end-to-end before flipping real users.)

### 5. Documented "Save to GitHub" scope

I'll run "Save to GitHub" today regardless of the above answers. To confirm scope so I know what to expect in the pushed repo:

- Does it export **only** `/app/backend/` and `/app/frontend/`, or also `/app/memory/` and `/app/test_reports/`?
- Are secrets (`.env` files) redacted, kept as-is, or omitted entirely?
- Are attached-asset uploads (image assets in `frontend/assets/`) included?
- Node modules / Python `__pycache__` — auto-excluded, or do I need a `.gitignore`?

## What I'm hoping to hear back

Ideal outcome: **you can quote me a reliability tier that eliminates silent-suspension risk and I don't need to migrate at all.** Second-best outcome: **you confirm the integrations (LLM key / push / Resend) work off-platform so I can point-in-place migrate without a re-review gap on the mobile side.** Worst-case: I plan on a full 2-3 week migration with the App Store review risk and I need timing guidance.

I'm not looking to leave — I'm looking to accept paying users without introducing new failure modes. Whatever gets me there fastest is what I'll take.

Best regards,
Paul
(QuakeAngel — Karen Vincenti / Malta)

---

## After you send

1. Log the send time here so we can track SLA on their reply:
   - Sent at: `_____________________`
   - Reply-by expectation: 3 business days (typical Emergent support turnaround)
2. When they reply, paste the response verbatim below this line so the next agent session has it in context:

   ---

   **Emergent reply (dated ____________)**

   [paste here]

   ---

3. Actions decided from the reply:
   - [ ] Stay on Emergent, sign up for tier: __________
   - [ ] Migrate backend to Fly.io with keys: __________
   - [ ] Keep mobile on Emergent, backend on Fly.io
   - [ ] Full migration (backend + mobile)
