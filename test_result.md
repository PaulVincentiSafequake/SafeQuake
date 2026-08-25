#====================================================================================================
# START - Testing Protocol - DO NOT EDIT OR REMOVE THIS SECTION
#====================================================================================================

# THIS SECTION CONTAINS CRITICAL TESTING INSTRUCTIONS FOR BOTH AGENTS
# BOTH MAIN_AGENT AND TESTING_AGENT MUST PRESERVE THIS ENTIRE BLOCK

# Communication Protocol:
# If the `testing_agent` is available, main agent should delegate all testing tasks to it.
#
# You have access to a file called `test_result.md`. This file contains the complete testing state
# and history, and is the primary means of communication between main and the testing agent.
#
# Main and testing agents must follow this exact format to maintain testing data. 
# The testing data must be entered in yaml format Below is the data structure:
# 
## user_problem_statement: {problem_statement}
## backend:
##   - task: "Task name"
##     implemented: true
##     working: true  # or false or "NA"
##     file: "file_path.py"
##     stuck_count: 0
##     priority: "high"  # or "medium" or "low"
##     needs_retesting: false
##     status_history:
##         -working: true  # or false or "NA"
##         -agent: "main"  # or "testing" or "user"
##         -comment: "Detailed comment about status"
##
## frontend:
##   - task: "Task name"
##     implemented: true
##     working: true  # or false or "NA"
##     file: "file_path.js"
##     stuck_count: 0
##     priority: "high"  # or "medium" or "low"
##     needs_retesting: false
##     status_history:
##         -working: true  # or false or "NA"
##         -agent: "main"  # or "testing" or "user"
##         -comment: "Detailed comment about status"
##
## metadata:
##   created_by: "main_agent"
##   version: "1.0"
##   test_sequence: 0
##   run_ui: false
##
## test_plan:
##   current_focus:
##     - "Task name 1"
##     - "Task name 2"
##   stuck_tasks:
##     - "Task name with persistent issues"
##   test_all: false
##   test_priority: "high_first"  # or "sequential" or "stuck_first"
##
## agent_communication:
##     -agent: "main"  # or "testing" or "user"
##     -message: "Communication message between agents"

# Protocol Guidelines for Main agent
#
# 1. Update Test Result File Before Testing:
#    - Main agent must always update the `test_result.md` file before calling the testing agent
#    - Add implementation details to the status_history
#    - Set `needs_retesting` to true for tasks that need testing
#    - Update the `test_plan` section to guide testing priorities
#    - Add a message to `agent_communication` explaining what you've done
#
# 2. Incorporate User Feedback:
#    - When a user provides feedback that something is or isn't working, add this information to the relevant task's status_history
#    - Update the working status based on user feedback
#    - If a user reports an issue with a task that was marked as working, increment the stuck_count
#    - Whenever user reports issue in the app, if we have testing agent and task_result.md file so find the appropriate task for that and append in status_history of that task to contain the user concern and problem as well 
#
# 3. Track Stuck Tasks:
#    - Monitor which tasks have high stuck_count values or where you are fixing same issue again and again, analyze that when you read task_result.md
#    - For persistent issues, use websearch tool to find solutions
#    - Pay special attention to tasks in the stuck_tasks list
#    - When you fix an issue with a stuck task, don't reset the stuck_count until the testing agent confirms it's working
#
# 4. Provide Context to Testing Agent:
#    - When calling the testing agent, provide clear instructions about:
#      - Which tasks need testing (reference the test_plan)
#      - Any authentication details or configuration needed
#      - Specific test scenarios to focus on
#      - Any known issues or edge cases to verify
#
# 5. Call the testing agent with specific instructions referring to test_result.md
#
# IMPORTANT: Main agent must ALWAYS update test_result.md BEFORE calling the testing agent, as it relies on this file to understand what to test next.

#====================================================================================================
# END - Testing Protocol - DO NOT EDIT OR REMOVE THIS SECTION
#====================================================================================================



#====================================================================================================
# Testing Data - Main Agent and testing sub agent both should log testing data below this section
#====================================================================================================
user_problem_statement: |
  Rescue-code + optional first-name feature. Help on-site responders identify
  which pin corresponds to the physical phone in front of them, especially
  when multiple trapped people are clustered within the same GPS accuracy
  radius. Show a short 5-char code (last 5 chars of device_id, uppercased)
  prominently on the app, ask an optional first name once at first launch,
  fire a persistent lock-screen notification with code+name after Trapped
  submissions, and surface both fields on the dashboard next to device_id.

backend:
  - task: "Add display_name field + short_code derivation to /api/status, /api/devices, /api/audit"
    implemented: true
    working: true
    file: "backend/server.py"
    stuck_count: 0
    priority: "high"
    needs_retesting: false
    status_history:
      - working: true
        agent: "main"
        comment: |
          Added StatusInPayload.display_name (Optional[str], max_length=200).
          Added _sanitize_display_name: trims whitespace, strips ASCII control
          chars (keeps unicode), caps at 40 chars post-clean; empty/whitespace
          → None. Added _short_code helper: last 5 chars uppercased, returns
          None for IDs shorter than 3 chars. _normalize_status_payload now
          persists sanitized display_name into device_status and status_events.
          /api/devices clean() and /api/audit base dict now expose short_code
          and display_name. mark-rescued and unmark-rescued also carry
          display_name forward on the audit event they insert.
          Manually verified via curl: Paul → short_code=4OLBG; unicode "José"
          preserved through control-char strip; 50-char string capped to 40.
      - working: true
        agent: "testing"
        comment: |
          Iteration 25 — 20/20 backend tests PASS (100%). Full sanitization
          matrix (10 cases) verified: omitted/null/empty/whitespace→None,
          verbatim + trim + unicode preservation (José, 京子) + control char
          stripping + 40-char cap. /api/devices and /api/audit both expose
          short_code + display_name; existing fields unchanged (backwards
          compat OK). mark-rescued / unmark-rescued carry display_name
          forward. Regression pass: /api/cors-debug shape unchanged;
          /api/trigger-alert 401/200 auth works. Test rows cleaned up.
          Regression suite: backend/tests/test_display_name_iteration_25.py.

metadata:
  created_by: "main_agent"
  version: "1.1"
  test_sequence: 1
  run_ui: false

test_plan:
  current_focus:
    - "Backend: /api/status accepts display_name, sanitizes correctly (unicode kept, control chars stripped, 40-char cap, null/empty → None)"
    - "Backend: /api/devices returns short_code (last 5 chars uppercased) and display_name for every device"
    - "Backend: /api/audit returns short_code and display_name on every status/rescued/rescue_reverted event"
    - "Backend: /api/mark-rescued carries display_name forward into the status_events audit row"
    - "Backend: unchanged endpoints (register-push, trigger-alert, cors-debug) still respond correctly — no regression"
  stuck_tasks: []
  test_all: false
  test_priority: "high_first"

agent_communication:
  - agent: "main"
    message: |
      Please run backend tests focused on the rescue-code + display_name
      feature. Key scenarios:
      1. POST /api/status with display_name=null/omitted → device_status.display_name is None; /api/devices response has display_name=None and short_code correctly derived.
      2. POST /api/status with display_name="Paul" → persisted as "Paul"; /api/devices returns display_name="Paul" and short_code = last 5 chars of device_id uppercased.
      3. POST /api/status with unicode display_name (e.g. "José", "Aiko", "京子") → preserved verbatim after sanitization.
      4. POST /api/status with display_name containing ASCII control chars (\x00-\x1F, \x7F) → control chars stripped, letters preserved.
      5. POST /api/status with display_name > 40 chars → capped to 40 chars in storage.
      6. POST /api/status with display_name="   " (whitespace only) → stored as None.
      7. GET /api/audit → every event of kind status/rescued/rescue_reverted has short_code and display_name fields.
      8. POST /api/mark-rescued for a device with display_name="X" → status_events row for that rescue has display_name="X" and /api/audit returns display_name="X" on the rescued event.
      9. Regression: /api/cors-debug still returns the right shape (allowed_origins, allow_reason, deploy_fingerprint).
      10. Regression: /api/trigger-alert admin auth still 401s wrong token and 200s correct one (ADMIN_TRIGGER_PASSWORD=REDACTED_SEE_ENV).

      Credentials: ADMIN_TRIGGER_PASSWORD = "REDACTED_SEE_ENV" (from backend/.env).
      Backend URL for testing: http://localhost:8001
      No frontend testing needed this pass — I already verified the pill,
      first-launch modal, tap-to-edit, and end-to-end name persistence via
      screenshot flow. The persistent lock-screen notification requires a
      real EAS build (Expo Go / web preview don't fire local notifications
      the same way) — that's a known limitation, not a test target.

      Please clean up any test rows you create (delete device_status +
      status_events with device_id starting with "test-" or "qg-test-").

## Iteration 34 — GDPR map links, UTC timestamps, server.py split, C1 phase 1 (2026-06-18)

  - agent: "main"
    message: |
      Landed, in the order Paul asked for: (1) GDPR — casualty coordinates no
      longer leave to Google in a URL (dashboard audit rows + per-person history
      recentre our own Leaflet map; the server-rendered /api/admin/audit-log page
      prints coordinates as text rounded to 5 dp). (2) UTC — naive timestamps
      were being parsed as local time by JS, showing events two hours early on a
      Malta phone; fixed at source and defensively on the phone. (3) server.py
      split 6,057 -> 2,307 lines across deps.py, push_relay.py,
      reports_export.py, routes_auth_users.py, routes_emsc_admin.py,
      routes_diagnostics.py, routes_recheck.py — route surface asserted
      byte-identical (71 routes, same methods) before and after. (4) C1 phase 1:
      automatic re-check ladder (backend/recheckin.py, 35 unit tests) with
      lock-screen answers that submit without unlocking.

  - agent: "testing"
    message: |
      43 live-endpoint tests written (tests/test_iteration_34_regression_and_c1.py),
      42 passed. Found one real 500: /api/admin/audit-log called get_audit_log()
      without the `request` argument it gained when /api/audit went behind
      operator auth on 2026-08-13 — a direct Python call, so FastAPI never
      filled it in.

  - agent: "main"
    message: |
      Fixed (server.py audit_log_browser now takes and forwards `request`). The
      page returns 200 and contains zero google.com/maps links. Full run after
      the fix: 376 passed, 6 skipped, 0 failed across 17 suites. Also deleted
      leftover qg-test-i34-* rows — one of them had short code 88B1, which
      tripped the "no B1/B2 jargon in PDFs" guard as a false positive.
      Stale legacy suites still failing for environmental reasons only (not
      regressions): test_admin_gate / test_cleanup_iteration_19 (KeyError
      EXPO_PUBLIC_BACKEND_URL in this fork), test_critical_alerts (asserts app
      version 1.0.8), test_debug_endpoints + push/probe suites (endpoints
      removed or moved behind auth in earlier sessions).

## #307 — legacy backend test suite cleanup (2026-08-23)

  - agent: "main"
    message: |
      143 failing / 858 passing -> 910 passing, 7 skipped (env-gated), 0
      failing, stable across three consecutive full runs. NO product code
      changed — every failure was a test asserting a world we had
      deliberately changed.

      Deleted 7 files that only tested the removed /api/debug/* endpoints
      (~100 failures). The "must stay 404" guards survive in
      test_critical_alerts.py and test_purge_browser.py.

      New tests/conftest.py does three things worth knowing about:
        * ONE event loop for the whole session (Motor pins itself to the
          first loop it sees; TestClient makes a new one per request, which
          is why tests passed alone and failed together). Use the
          `run_async` fixture instead of asyncio.run() for anything that
          touches the db.
        * `clear_register_rate_limit` — the per-IP /register-push limit is
          20/hour, generous for a phone, not for a test suite.
        * `stand_down_after` — MANDATORY for any test that sends a real
          alert. A live incident holds records on the working board, so a
          stray trigger breaks unrelated board tests, including on the next
          run (the state is in Mongo).
        * loads backend/.env once, so no test file carries a copy of
          ADMIN_TRIGGER_PASSWORD any more.

      Rewritten to current doctrine: device rows are seeded straight into
      Mongo (since #266 /register-push refuses to file a row the push
      provider rejected, which is every registration in this environment);
      trigger-alert calls carry the #245 confirmation phrase; POST
      /api/status uses the device check-in payload, not legacy client_name;
      the critical-alert payload assertions point at apns.py and
      push_relay.py where that code now lives.

      Two brittle tests fixed at the cause, not by relaxing them: the audit
      CSV returns the newest 500 rows in a window and a suite run writes
      several hundred events (window narrowed to 10 min), and the B1/B2
      table-vs-narrative check compares CURRENT state against WINDOW state
      (needs a wide window, now 29 days). Also the "no B1/B2 jargon" guard
      was matching the B1 inside random short codes like FB1FC — now a
      word-boundary match, which is the same false positive noted in
      iteration 34.

## 2026-08-24 — Paul's live test day, batch 1 (#296 #207 #283 #291 #289/#290 #297)

  - agent: "main"
    message: |
      983 backend tests passing, 0 failing. Every fix below was verified
      against the running preview backend, not just unit-tested.

      #296/#207 — repeating CRITICAL "Are you safe?". The ladder is LOCAL
      to the phone (8 notifications, 90s apart). Stand-down now sends the
      silent cancel push itself (works on v1.0.44 phones already in the
      field), the phone cancels on any stand-down path, and only the FIRST
      reminder is critical. See tests/test_reminders_cancelled_on_stand_down_296.py.

      #283 — one source for every count. people_counts._tally() is the only
      producer; /api/devices returns both populations; the dashboard's
      computeCounts() is DELETED and a test fails if arithmetic reappears in
      the pill mapper. NOTE for future work: if you add a number to the
      board, add it to Counts, not to the JavaScript.

      #291 — "Phone went dark" is gone from every user-facing surface. A
      broadcast alert no longer reads as having asked someone personally
      (that is why rescued people were showing as dark after a test alert).
      States and counts unchanged on purpose.

      #289/#290 — the phone now sends the report on the severity tap, and
      the follow-up sheet answer is an update. egress="not_answered" is a
      new value in both StatusInPayload and the Egress type, and it keeps a
      person OFF the walking wounded list. Board list rule and server count
      rule are kept identical by test.

      #297 — a test-looking authority name is refused on save and never
      printed if already saved. Word-boundary matching ("Attest Rescue" is
      a real name).

      Dashboard pushed to PaulVincentiSafequake/SafeQuake main (f60da31).
      NOT verifiable here: notification behaviour and haptics need a real
      build on a device.
