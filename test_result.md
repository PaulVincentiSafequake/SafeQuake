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
      10. Regression: /api/trigger-alert admin auth still 401s wrong token and 200s correct one (ADMIN_TRIGGER_PASSWORD=Pt3481pt).

      Credentials: ADMIN_TRIGGER_PASSWORD = "Pt3481pt" (from backend/.env).
      Backend URL for testing: http://localhost:8001
      No frontend testing needed this pass — I already verified the pill,
      first-launch modal, tap-to-edit, and end-to-end name persistence via
      screenshot flow. The persistent lock-screen notification requires a
      real EAS build (Expo Go / web preview don't fire local notifications
      the same way) — that's a known limitation, not a test target.

      Please clean up any test rows you create (delete device_status +
      status_events with device_id starting with "test-" or "qg-test-").
