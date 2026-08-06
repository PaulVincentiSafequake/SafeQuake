# QuakeAngel Backend Migration — Runbook

**From:** Emergent-hosted FastAPI + Mongo
**To:** Fly.io (`fra` region) + MongoDB Atlas Frankfurt (`eu-central-1`)
**Owner:** Paul
**Prepared:** 2026-08-06

---

## Prerequisites — do these BEFORE cutover day

- [ ] **Support email sent** — copy from `/app/memory/emergent-support-email.md`, send from your address, and wait for reply before starting. Some steps below (specifically the Emergent LLM/push/Resend continuity checks) depend on their answers. If support says integrations don't work off-platform, you'll need to swap in direct-vendor keys before cutover, which is a separate ~half-day of prep.
- [ ] **Save to GitHub** run once in the Emergent dashboard. This is documented and works today — do it now, not on cutover day. Confirm the repo contains `/app/backend`, `/app/frontend`, `/app/memory`, and the new `/app/scripts` and `fly.toml` + `Dockerfile` added during prep.
- [ ] **Fly.io account created** with billing set up. Recommended: prepay 3 months so a lapsed card during a seismic event doesn't recreate the exact problem we're leaving Emergent for. (~$15 for shared-cpu-1x.)
- [ ] **MongoDB Atlas cluster** provisioned in Frankfurt `eu-central-1`, tier M10 or larger (M0 free tier does NOT support VPC peering and has connection limits that make it unsuitable for production). Enable point-in-time recovery. Create a database user with `readWrite` on `quakeguard_prod`.
- [ ] **Custom domain** decided. `api.quakeangel.app` or similar. You'll need DNS access to add a CNAME later.
- [ ] **APNs verified working** on Emergent (once). If it's not working now it won't be working after either — fix first, migrate after.
- [ ] **Baseline continuity snapshot taken** — run these on the current Emergent-hosted backend so you have "before" numbers to compare against:
  ```bash
  curl -H "x-admin-token: $ADMIN_TRIGGER_PASSWORD" \
       https://<current-backend>/api/admin/emsc/continuity \
       > /app/memory/pre-migration-continuity.json
  ```
  Save this file. You will compare against it after cutover.

---

## Cutover — the actual migration

### Step 1: Pre-freeze health check (5 min)

Verify current state is healthy before touching anything. If any of these fail, **abort** and investigate — don't migrate a sick system.

```bash
# On Emergent-hosted backend
curl https://<current-backend>/api/status
# Expect: 200 with poller alive_seconds > 0

curl https://<current-backend>/api/seismic-map/events?window_hours=24
# Expect: 200, count >= 0 (may be 0 if quiet)

# Check MongoDB reachability from inside the pod
# (via Emergent's web terminal or the fastest access path you have)
python -c "
from motor.motor_asyncio import AsyncIOMotorClient
import os, asyncio
async def go():
    c = AsyncIOMotorClient(os.environ['MONGO_URL'])
    for db in await c.list_database_names():
        print(db)
asyncio.run(go())
"
# Expect: your prod DB listed, no auth errors
```

### Step 2: Freeze writes (T-0, ~5 min max downtime)

Two options — pick one:

**Option A: Read-only banner + halt background pollers (recommended)**

- SSH into the current backend pod
- Set an env var `MIGRATION_READ_ONLY=true` and restart the backend
- (This requires a small code change — see PRD todo: add read-only-mode env-var gate to `server.py`. If not yet implemented, use Option B.)

**Option B: Hard stop (simpler, slightly more disruptive)**

- Stop the Emergent backend service via the Emergent dashboard's stop-service button
- Users see connection errors during the cutover window (max 10 minutes)
- **Communicate this to any dashboard operators** — send a message ~30 min ahead so they don't panic when the dashboard goes offline

Either way — **record the freeze time**. This is the beginning of the poll gap that the continuity test will verify is bounded.

```
Freeze started at: __________
```

### Step 3: Export from Emergent-hosted Mongo (T+1min, ~5 min for small DBs)

```bash
# From your laptop or any machine with network access to Emergent's Mongo
export SRC_MONGO_URL="<the current Emergent MONGO_URL>"

python /app/scripts/migrate_mongo.py export \
    --src "$SRC_MONGO_URL" \
    --db quakeguard_prod \
    --out ./migration-dump
```

**Verify** the dump completed:
```bash
ls -lh migration-dump/
cat migration-dump/_manifest.json | jq '.collections | to_entries | map({name: .key, rows: .value.rows})'
```

You should see rows for at least: `apns_configs`, `country_configs`, `dashboard_operators`, `emsc_events`, `emsc_soak_meta`, `emsc_poller_gaps`, `entitlements`, `push_devices`, `user_presence`, `audit_log`. If any of these are missing or have zero rows unexpectedly, **do not proceed** — investigate and re-export.

### Step 4: Restore into Atlas Frankfurt (T+6min, ~5 min)

```bash
export DST_MONGO_URL="mongodb+srv://<atlas-user>:<atlas-pass>@quakeangel.xxxxx.mongodb.net/quakeguard_prod?retryWrites=true&w=majority"

python /app/scripts/migrate_mongo.py restore \
    --dst "$DST_MONGO_URL" \
    --db quakeguard_prod \
    --in ./migration-dump
```

The script prints per-collection row counts and flags any mismatch. **Every collection must report `[OK]`** — if any say `[MISMATCH]` do not proceed.

### Step 5: Deploy the backend to Fly.io (T+11min, ~5 min)

```bash
# One-time setup (skip if already done)
fly auth login
fly launch --no-deploy --name quakeangel-backend --region fra \
    --dockerfile backend/Dockerfile --org personal
# (or `--org <your-org>` if you created a Fly.io organisation)

# Set secrets — DO NOT paste these in a shell that logs to a file
fly secrets set \
    MONGO_URL="$DST_MONGO_URL" \
    DB_NAME="quakeguard_prod" \
    JWT_SECRET="<copy from current .env>" \
    ADMIN_TRIGGER_PASSWORD="<copy from current .env>" \
    LEGACY_TOKEN_ENABLED="true" \
    EMERGENT_PUSH_KEY="<copy from current .env, if support confirms it works off-platform>" \
    GOOGLE_WEB_CLIENT_ID="<copy from current .env>" \
    BOOTSTRAP_ADMIN_EMAIL="<copy from current .env>" \
    --app quakeangel-backend

# Deploy
fly deploy --app quakeangel-backend

# Verify
fly status --app quakeangel-backend
# Expect: 1 machine, state=started, health checks passing

curl https://quakeangel-backend.fly.dev/api/status
# Expect: 200 with same shape as pre-migration
```

### Step 6: Verify continuity (T+16min, ~5 min)

```bash
# Continuity re-check on the Fly.io instance
curl -H "x-admin-token: <ADMIN_TRIGGER_PASSWORD>" \
     https://quakeangel-backend.fly.dev/api/admin/emsc/continuity \
     > /app/memory/post-migration-continuity.json

# Run the automated continuity test
cd /app/backend
SRC_CONTINUITY=/app/memory/pre-migration-continuity.json \
DST_CONTINUITY=/app/memory/post-migration-continuity.json \
python -m pytest tests/test_emsc_continuity_migration.py -v

# Expect: all pass, with the post-migration file showing ONE bounded gap
# (the migration window itself) and soak_started_at UNCHANGED from before.
```

**If soak_started_at changed** the poller thinks this is a fresh soak — someone (either the migration script or a code path in server.py startup) reset the meta doc. This is recoverable but you'll lose the 7+ day continuity claim. STOP and investigate before unfreezing.

### Step 7: Update mobile app's backend URL (T+21min, no downtime beyond mobile app relaunch)

- Log into Emergent dashboard
- Open mobile app deployment settings
- Change `EXPO_PUBLIC_BACKEND_URL` from the current Emergent value to `https://quakeangel-backend.fly.dev`
- Trigger a mobile republish
- Once redeployed, on a test device: close the app fully, reopen, verify:
  - Home screen loads (calls `/api/status`)
  - Notification settings loads (`/api/devices/…/notification-preset`)
  - Seismic map loads (`/api/seismic-map/events`)
  - Entitlement banner works (`/api/entitlement`)
- If ALL good, promote to TestFlight/Play internal testing
- Existing users who don't update the app **will continue calling the old Emergent URL** — see Step 10.

### Step 8: DNS cutover for custom domain (T+26min, ~5 min + propagation)

If you're moving to `api.quakeangel.app` (recommended so you're not tied to a `.fly.dev` URL for future moves):

```bash
# In Fly.io
fly certs add api.quakeangel.app --app quakeangel-backend
fly certs show api.quakeangel.app --app quakeangel-backend
# Note the DNS records it tells you to create.

# In your DNS provider (Cloudflare / Route53 / etc):
# Add a CNAME:  api.quakeangel.app  ->  quakeangel-backend.fly.dev
# Or if AAAA/A: use the shared-cpu IPs Fly.io returns.

# Verify SSL is provisioned (~2-5 min)
fly certs show api.quakeangel.app --app quakeangel-backend
# Wait for "Configured: true"

curl https://api.quakeangel.app/api/status
# Expect: 200
```

Once verified, update the mobile app's `EXPO_PUBLIC_BACKEND_URL` to `https://api.quakeangel.app` and republish once more. This is the URL you commit to publicly — Fly.io internal URLs may change if you ever migrate again, custom domains are portable.

### Step 9: Unfreeze / go-live announcement (T+31min)

- Confirm Step 6 continuity test passes
- Confirm Step 7 mobile end-to-end works
- Post go-live message to whoever needs to know (operators, dashboard users, beta testers)

### Step 10: Grace period — keep Emergent backend running (7+ days)

- Do NOT delete the Emergent backend immediately
- Existing mobile app installations continue to point at the old URL until users update
- Keep Emergent alive but stop the EMSC poller (to avoid double-writing to the migrated Mongo — which you no longer control from Emergent anyway since MONGO_URL changed)
- After ~7 days of new-version adoption in TestFlight / Play, or after a forced-update prompt, tear down Emergent:
  ```bash
  # Cancel Emergent subscription / delete the deployment
  ```

---

## Rollback plan

Any of Steps 5-9 can be rolled back:

- **Step 5 (Fly.io deploy fails)**: nothing to roll back. Emergent backend is still running (Step 2 froze writes but you can unfreeze). Go back to pre-migration state.
- **Step 6 (continuity test fails)**: roll back is nuanced. If the Atlas restore is wrong, you re-run Step 4 with `--drop` to clean slate. If soak_started_at was clobbered, restore from the JSONL dump file (`emsc_soak_meta.jsonl`) manually.
- **Step 7 (mobile app broken)**: change `EXPO_PUBLIC_BACKEND_URL` back to Emergent, republish. Mobile users see ~30-60 min of the Emergent-hosted responses again.
- **Step 8 (DNS)**: revert DNS record. Wait for TTL to expire. Mobile calls resume via the Fly.io URL.

The whole thing is designed to be reversible until Step 10 (Emergent teardown). Do not tear down Emergent for at least 7 days after go-live.

---

## Post-migration hardening (over first month, not urgent)

- [ ] Rotate `JWT_SECRET`, `ADMIN_TRIGGER_PASSWORD` (see `/app/memory/env-inventory.md` rotation section)
- [ ] Flip `LEGACY_TOKEN_ENABLED=false` once Google auth is confirmed stable
- [ ] Restrict MongoDB Atlas network access to Fly.io Frankfurt IPs only (currently `0.0.0.0/0` during migration; must lock down)
- [ ] Set up Fly.io machine metrics alerts (Grafana Cloud free tier works)
- [ ] Set up MongoDB Atlas alerts (connection count, storage, slow query)
- [ ] Second Fly.io machine in a different region for HA (optional — see fly.toml design note #4)
- [ ] Consider CloudFront/Cloudflare in front of Fly.io for DDoS protection

---

## Estimated downtime

If the runbook is followed cleanly: **20-30 minutes** of the backend being unavailable to mobile users. During this window:
- Existing users see connection errors when opening the app
- Critical Alerts already-sent (from Apple's servers) will still fire — those are Apple-side, not backend-side
- NEW critical alerts CANNOT be pushed because the poller and admin dashboard are both frozen
- Assume: don't do this during Malta's known seismic-quiet times (nothing above M3 in the last 30 days), and pick a low-activity slot per `/api/seismic-map/events?window_hours=720`
