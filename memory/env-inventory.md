# QuakeAngel — Environment Variable Inventory for Migration

**Snapshot date:** 2026-08-06
**Purpose:** Track every environment variable currently used by the backend, whether it needs to change during Fly.io migration, and if so what the new value should be.

## Backend `/app/backend/.env`

| Variable | Current source | On Fly.io | Notes |
|---|---|---|---|
| `MONGO_URL` | Emergent-managed Mongo | **CHANGES** → `mongodb+srv://…` from Atlas Frankfurt | The single most important secret to rotate. Set via `fly secrets set MONGO_URL=...`. Do NOT commit to git under any circumstance. |
| `DB_NAME` | `quakeguard_prod` (or whatever the current name is) | **KEEP SAME NAME** | Preserving the DB name simplifies the migration script — no rename step. Set via `fly secrets set DB_NAME=quakeguard_prod`. |
| `JWT_SECRET` | Random ~32 char string | **KEEP SAME VALUE** during migration; rotate as separate hardening step later | Rotating during migration invalidates every logged-in dashboard operator's session. Do rotation as its own change, not bundled with hosting move. Set via `fly secrets set JWT_SECRET=<current-value>`. |
| `ADMIN_TRIGGER_PASSWORD` | (redacted — see backend/.env locally; do NOT commit the value) | **KEEP SAME VALUE** during migration | Same reasoning as JWT_SECRET. This is the X-Admin-Token for legacy admin routes; changing breaks the dashboard mid-migration. Rotate post-migration once dashboard has re-verified. |
| `LEGACY_TOKEN_ENABLED` | `true` at time of snapshot (was `false` earlier per handoff — appears to have been reset) | **KEEP `true` DURING MIGRATION**, flip to `false` AFTER Google auth is verified end-to-end on Fly.io | This is the fallback auth path. If Google auth breaks post-migration, legacy tokens are the escape hatch. Once verified, set `fly secrets set LEGACY_TOKEN_ENABLED=false`. |
| `EMERGENT_PUSH_KEY` | Emergent-provided (placeholder value in dev, real value injected by Emergent Publish flow) | **UNKNOWN — pending support@emergent.sh reply** | If Emergent's push relay works from external hosts, keep the key and change nothing. If not, we need to bypass Emergent push entirely and go direct FCM for Android (adds a real Firebase project). Blocked on question #3 of `/app/memory/emergent-support-email.md`. |
| `GOOGLE_WEB_CLIENT_ID` | Google Cloud Console web client | **KEEP SAME VALUE** but add `https://quakeangel-backend.fly.dev` (and eventual custom domain) to Authorized redirect URIs in Google Cloud Console. | The client ID itself does not change — it's tied to the Google Cloud project, not the hosting platform. |
| `BOOTSTRAP_ADMIN_EMAIL` | Set once via `.env`, only read on first-boot to seed initial admin | **KEEP SAME VALUE** during migration (idempotent — will be a no-op if admin already exists in restored DB) | Once the DB is migrated the seeded admin is already present, so this variable is effectively inert. Fine to set for parity. |

## Frontend `/app/frontend/.env`

⚠️ These are **PROTECTED** and MUST NOT be committed or modified in the frontend `.env` file itself. They are set by Metro/Expo tooling.

| Variable | Current source | On Fly.io migration | Notes |
|---|---|---|---|
| `EXPO_PUBLIC_BACKEND_URL` | Emergent-injected preview URL in dev; production build sees whatever Emergent Publish set | **CHANGES** → `https://quakeangel-backend.fly.dev` (or custom domain like `https://api.quakeangel.app`) | This is the ONE variable that must be updated in the mobile app before it starts talking to Fly.io. Update via Emergent Publish's environment editor before rebuilding the iOS/Android binary. |
| `EXPO_PACKAGER_PROXY_URL` | Emergent Metro proxy | KEEP — unrelated to prod backend | Dev-time only. Do not touch. |
| `EXPO_PACKAGER_HOSTNAME` | Emergent Metro hostname | KEEP — unrelated to prod backend | Dev-time only. Do not touch. |
| `EXPO_TUNNEL_SUBDOMAIN` | Emergent tunnel | KEEP — unrelated to prod backend | Dev-time only. Do not touch. |
| `EXPO_USE_FAST_RESOLVER` | Metro flag | KEEP — Metro tuning | Dev-time only. |
| `METRO_CACHE_ROOT` | Metro cache path | KEEP — filesystem cache | Dev-time only. |

## Not in .env — stored in MongoDB and migrating with the DB dump

These are secrets our backend uses but that are NOT in environment variables — they live in Mongo collections and will migrate automatically with the DB dump:

| Where it lives | What it is | Migration behavior |
|---|---|---|
| `apns_configs` collection | Apple APNs signing key `.p8`, `key_id`, `team_id`, `bundle_id` (encrypted at rest via `boto3`/Fernet) | Migrates as part of `mongodump`. On the destination side the same key/team_id are read from the doc and used to sign — no re-upload needed. |
| `country_configs` collection | Malta lat/lon center, poll radius, preview mode config | Migrates as part of `mongodump`. |
| `push_devices` collection | Device push tokens (APNs + FCM) | Migrates as part of `mongodump`. These are the tokens the backend uses to reach users' phones — losing them = every user re-registers on next app launch. **Critical to preserve.** |
| `dashboard_operators` collection | Google-authenticated operators + their `sub` claim + role | Migrates as part of `mongodump`. Operators keep their access as long as `GOOGLE_WEB_CLIENT_ID` is unchanged. |
| `entitlements` collection | Just-shipped subscription state machine | Migrates as part of `mongodump`. Empty in production right now (no Apple ASN2 events yet). |
| `emsc_soak_meta`, `emsc_poller_gaps` | The continuity tracking metadata that proves our poller has been up | **CRITICAL** — see `/app/backend/tests/test_emsc_continuity_migration.py` for the verification that soak_started_at is preserved and only one bounded-length gap is recorded from the migration window itself. |

## What Fly.io itself provides

Fly.io automatically sets these — **do not** try to override them:

| Fly.io env var | Purpose |
|---|---|
| `PORT` | The port Fly.io expects the app to listen on. Our Dockerfile defaults to 8001 and Fly.io honors that via `[env] PORT=8001` in fly.toml. |
| `FLY_APP_NAME` | The app slug (`quakeangel-backend`). Available if we want to log it. |
| `FLY_REGION` | The region the current machine is running in (`fra`). Useful for cross-region debug. |
| `FLY_MACHINE_ID` | Unique machine ID. Useful for log correlation. |
| `FLY_PUBLIC_IP` | Public IP the machine is reachable at (usually only relevant for TCP services, not HTTP). |
| `FLY_IMAGE_REF` | Docker image reference — useful for confirming which build is live. |

## Verification checklist post-migration

After `fly secrets set` and the first `fly deploy`, verify via:

```bash
fly secrets list --app quakeangel-backend
# Should show: MONGO_URL, DB_NAME, JWT_SECRET, ADMIN_TRIGGER_PASSWORD,
#              LEGACY_TOKEN_ENABLED, EMERGENT_PUSH_KEY (if applicable),
#              GOOGLE_WEB_CLIENT_ID, BOOTSTRAP_ADMIN_EMAIL

fly ssh console --app quakeangel-backend
# Then inside the shell:
#   env | grep -E "MONGO_URL|DB_NAME|JWT_SECRET|GOOGLE_WEB_CLIENT_ID"
# Values should be present but Fly.io masks secrets in output.

curl https://quakeangel-backend.fly.dev/api/status
# Should return 200 with the same shape as the current Emergent-hosted backend.
```

## Rotation post-migration (once traffic is confirmed healthy)

Not urgent for the migration itself, but worth doing within the first month on Fly.io:

- [ ] Rotate `JWT_SECRET` (forces every operator to re-authenticate)
- [ ] Rotate `ADMIN_TRIGGER_PASSWORD` (invalidates any leaked X-Admin-Token)
- [ ] Set `LEGACY_TOKEN_ENABLED=false` (removes the fallback auth path once Google is verified stable)
- [ ] Add MongoDB Atlas IP allowlist entry for Fly.io Frankfurt region only (removes any other IP's access to the database)
