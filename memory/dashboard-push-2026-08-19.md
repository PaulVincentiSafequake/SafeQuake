# Dashboard push — DONE, 2026-08-19 (Batch 7 Round 1)

**PUSHED** to `PaulVincentiSafequake/SafeQuake`, branch `main`, path
`backend_dashboard/public/index.html`.

- **Commit:** `bcd58b8367a0745416e40633c4cafe0d6eb664d5`
- **URL:** https://github.com/PaulVincentiSafequake/SafeQuake/commit/bcd58b8367a0745416e40633c4cafe0d6eb664d5
- **New file SHA:** `2a67ebe0800faad3b0dd6729fb25866cefabf80d` (262,146 bytes)
- **Previous file SHA:** `2c42228b5a8f71d0e5719aeb0cc5832fc716fc20` (252,178 bytes)
- **Verification:** read the file back from the repo — byte-identical to
  `/app/memory/dashboard_build/index.html`. SHA256 match:
  `5c083cd0a9666182d51007c0a4505b12ec7a5de00dd27d96d2cbaf5a304e337a`

## Live-only edits found on the remote before push

**None this time.** Every line present on the remote but absent from the
local file was the OLD version of code my Round 1 patches legitimately
updated (401-only auth path, colour-only filter buttons, contradictory
"asked 0" sentence, footer-before-admin-panel, `white-space: nowrap`
label, hard-truncate-to-12-chars, `esc()` in histMapLink, raw error
strings, bindPopup without autoPan). No hand-edits by Paul to preserve.

Contrast last push (`c871710073`, 2026-06-18) where the live file had
C3 pin-names that the staged file did not, and a wholesale overwrite
would have silently reverted them.

## Secret check before push

Clean. Two hits from the naïve grep, both intentional:

1. `<label for="qg-rescue-modal-password">` — the client-side password
   INPUT FIELD label for emergency-personnel unlock. Not a literal.
2. A regex inside the notes-scrubber that MATCHES the pattern
   `\b(password|passwd|pwd|passphrase|api_key|secret|token|bearer|credentials?)\b` —
   this is the DEFENSIVE regex that redacts credentials from operator
   notes. Removing it is what would create a leak.

## PAT hygiene

Paul provided the fresh PAT inline in his last message. That token is
now recorded in the chat transcript and MUST be revoked immediately
regardless of whether it was fine-grained + one-day-expiry. GitHub →
Settings → Developer settings → Personal access tokens (fine-grained)
→ revoke.

## What this push contains (dashboard side only)

Round 1 items C1–C6 and the A1 minimum session-expired banner. See
commit message on GitHub for the summary, and Batch 7 Round 1 report
for the full accept-only-if lines per item.

## Order for going live

1. **Publish the backend on Emergent.** The dashboard reads
   `effective_status`, `compute_counts`-driven `/api/public/summary`,
   the backfilled `/api/audit` display_names, and the current-state
   PDF aggregate. Publish this file first and the dashboard uses new
   endpoints against the old backend — endpoints work, but counts
   might match the OLD (buggy) semantics until publish lands.
2. **Wait for Render's auto-deploy** to pick up commit `bcd58b8367`
   (typically 1–3 minutes after push).
3. **Hard-refresh the live dashboard** and verify no console errors.
4. Sign in and check: full-history modal opens (C1), Operators & Access
   panel renders (C2 unblocked by C1's `esc` fix), Stop-reminders button
   only appears when signed in (C3), map key shows shape + colour + word
   (C6), pin labels wrap to two lines instead of truncating (C5).
5. Then build iOS 1.0.28 (`buildNumber: 28`, `versionCode: 28`) for
   Round 2. Round 2 covers B1 (notification routing) through B5, plus
   confirms C7 bundle name.

## No-touch reminders

- Never edit CSS classes prefixed `qg-` without also updating any inline
  style attributes that duplicate them (there are a few in this file).
- Never remove `window.qgEsc`, `window.qgFriendlyError`, or
  `window.qgShowSessionExpiredBanner` — they are Round 1 contracts
  that Round 3 will build on.
- `esc()` / `escapeHtml` are defined SEVEN TIMES across this file
  (four local `esc`, three local `escapeHtml`). Round 3 collapses
  them to `window.qgEsc`. Do not add an eighth definition.
