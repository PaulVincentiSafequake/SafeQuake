# Dashboard push — Batch 7 (queued, awaiting PAT)

Prepared: 2026-08-19

## Files changed (dashboard only)

- `backend_dashboard/public/index.html` — mirrored from
  `/app/memory/dashboard_build/index.html`.

## What's in this push

### #230 — Stale-session banner is no longer dismissible
The dismiss button was removed. In its place is a "Sign in again"
button that either re-runs the sign-in flow or reloads the page.
Rationale: the banner is the dashboard's only live signal that the
numbers on screen have stopped updating. If an operator can dismiss
it, they can be reading hour-old numbers thinking they're live. Now
the only way to make the banner go away is to actually sign back in.
`aria-live="assertive"` added so assistive tech announces it over
whatever the operator is reading.

### #231a — Grammar fix on the re-check status block
"1 person currently need help" is now "1 person currently needs
help", using the same singular/plural helper pattern the reports
use. Under stress a reader trips on the mismatch before they read
the number.

## What's still queued but NOT in this push

- #225 — Tremor-diagnostics production data. Pushed in the previous
  batch, but the endpoint reads production DB which this environment
  can only see the preview DB of. Prod endpoint verification is
  Paul-side.
- #227 — Map filter behaviour. Verified the current code already
  removes filtered markers from the Leaflet layer (`map.removeLayer`
  at line 4979) rather than dimming them, so this is effectively
  already correct. If Paul still sees dimmed markers on the live
  dashboard, that means the older build on Render doesn't yet have
  this. Push resolves it.

## How to push (when PAT arrives)

```bash
cd /tmp && rm -rf sq-tmp && \
  git clone --depth 1 https://x-access-token:$PAT@github.com/PaulVincentiSafequake/SafeQuake sq-tmp && \
  cd sq-tmp && \
  cp /app/memory/dashboard_build/index.html backend_dashboard/public/index.html && \
  git add backend_dashboard/public/index.html && \
  git -c user.email=agent@quakeangel.app -c user.name="Quake Angel Agent" \
    commit -m "Batch 7 dashboard: #230 non-dismissible stale banner + #231 grammar" && \
  git push origin main
```

Render will redeploy within ~2 minutes.
