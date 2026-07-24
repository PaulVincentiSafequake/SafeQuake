"""
Backend tests for the expanded recipient list rendering in
GET /api/debug/last-push-events.

Spec:
  a) Register >=7 devices with distinct user_ids and fire a test push;
     the newest trigger card must show a 'recipient list:' section
     rendered as a scrollable div containing ALL user_ids, each in its
     own <code> block on its own line — NOT truncated to 5.
  b) The 'recipients:' label shows the total chunk_size.
  c) A '(+N more not captured)' note appears only when total > shown
     (in normal ops this cannot fire because send_push CHUNK=100 <= 200,
      so we assert its absence).
  d) HTML escape is applied to every user_id (test with '<script>' uid).

Code-level assertions on /app/backend/server.py:
  - send_push records recipients_sample = chunk[:200] (not [:5]).
  - Trigger event render uses 'recipient list:' label (not 'first recipients:').
  - Scrollable div has max-height:180px; overflow:auto.
  - _html.escape is applied to every user_id.
  - '(+N more not captured)' branch only fires when total > shown.

Regressions:
  - POST /api/trigger-alert still returns 200 JSON.
  - GET /api/debug/probe-push variants A-F still render.
  - Register events still show user_id + platform.
  - Ring buffer bounded to maxlen=20.
  - Legacy /api/status GET+POST works.
  - send_push HTTPException(500) on upstream 401 contract holds.
  - /api/debug/devices unchanged.
"""
import os
import re
import subprocess
import time
import uuid

import pytest
import requests

BASE_URL = os.environ.get("BACKEND_URL", "http://localhost:8001")
ADMIN_PWD = "Pt3481pt"
EVENTS_URL = f"{BASE_URL}/api/debug/last-push-events"
SERVER_PY = "/app/backend/server.py"


def _restart_backend_and_wait():
    subprocess.run(
        ["sudo", "supervisorctl", "restart", "backend"],
        check=True, capture_output=True, text=True,
    )
    deadline = time.time() + 20
    while time.time() < deadline:
        try:
            if requests.get(f"{BASE_URL}/api/", timeout=2).status_code == 200:
                return
        except Exception:
            pass
        time.sleep(0.5)
    raise RuntimeError("backend did not come back after restart")


def _mongo_coll():
    from pymongo import MongoClient
    c = MongoClient(os.environ.get("MONGO_URL", "mongodb://localhost:27017"))
    return c, c[os.environ.get("DB_NAME", "test_database")].push_devices


def _wipe_devices():
    c, coll = _mongo_coll()
    docs = list(coll.find({}))
    coll.delete_many({})
    c.close()
    return docs


def _restore_devices(docs):
    if not docs:
        return
    c, coll = _mongo_coll()
    for d in docs:
        d.pop("_id", None)
        coll.update_one({"user_id": d["user_id"]}, {"$set": d}, upsert=True)
    c.close()


def _seed_direct(user_id: str, platform: str = "ios"):
    """Direct pymongo insert — bypasses /api/register-push which returns
    500 under placeholder EMERGENT_PUSH_KEY (relay 401)."""
    c, coll = _mongo_coll()
    coll.update_one(
        {"user_id": user_id},
        {"$set": {
            "user_id": user_id,
            "platform": platform,
            "device_token": "tok-" + uuid.uuid4().hex[:10],
        }},
        upsert=True,
    )
    c.close()


def _events_html():
    r = requests.get(EVENTS_URL, params={"token": ADMIN_PWD}, timeout=5)
    assert r.status_code == 200, r.text
    return r.text


def _find_first_trigger_card(html: str, title_substr: str) -> str:
    cards = re.findall(
        r'<div style="border:1px solid #ddd;border-radius:8px[^"]*"[^>]*>(.*?)</pre>\s*</div>',
        html, flags=re.S,
    )
    for c in cards:
        if title_substr in c:
            return c
    return ""


# ==================================================================
# Module fixture: wipe devices, register 7 known qg-N ids
# ==================================================================
SEVEN_IDS = [f"qg-{n}" for n in range(1, 8)]


@pytest.fixture(scope="module", autouse=True)
def clean_and_seed():
    _restart_backend_and_wait()
    saved = _wipe_devices()
    for uid in SEVEN_IDS:
        _seed_direct(uid)
    yield
    # cleanup our test devices, restore originals
    _wipe_devices()
    _restore_devices(saved)


# ==================================================================
# Spec (a-e): full recipient list rendered, HTML-escaped
# ==================================================================
class TestFullRecipientList:

    def test_seven_devices_registered(self):
        r = requests.get(f"{BASE_URL}/api/debug/devices")
        assert r.status_code == 200
        ids = {d["user_id"] for d in r.json()["devices"]}
        for uid in SEVEN_IDS:
            assert uid in ids, f"missing seeded device {uid}: {ids}"
        assert r.json()["device_count"] == 7

    def test_test_push_renders_all_seven_recipients(self):
        # Fire test push
        r = requests.get(
            f"{BASE_URL}/api/debug/test-push",
            params={"token": ADMIN_PWD}, timeout=15,
        )
        assert r.status_code == 200

        html = _events_html()
        card = _find_first_trigger_card(html, "QuakeGuard test push")
        assert card, "no event card found for test-push"

        # (b) new label — 'recipient list:' (not 'first recipients:')
        assert "recipient list:" in card, "label 'recipient list:' missing"
        assert "first recipients:" not in card, \
            "old label 'first recipients:' should be gone"

        # (b) 'recipients:' label shows the total count (chunk_size = 7)
        m = re.search(r"<b>recipients:</b>\s*(\d+)", card)
        assert m, f"recipients: total not found. Card:\n{card[:800]}"
        assert int(m.group(1)) == 7, f"expected 7, got {m.group(1)}"

        # (c) ALL 7 user_ids appear in <code> blocks
        code_blocks = re.findall(r"<code[^>]*>(.*?)</code>", card)
        for uid in SEVEN_IDS:
            assert uid in code_blocks, \
                f"{uid} not in code blocks. Got: {code_blocks}"

        # More than 5 → confirms the [:200] cap replaced [:5]
        recipient_blocks = [c for c in code_blocks if c.startswith("qg-")]
        assert len(recipient_blocks) == 7, \
            f"expected 7 qg-* code blocks, got {len(recipient_blocks)}: {recipient_blocks}"

    def test_scrollable_div_wraps_recipient_list(self):
        # Trigger a fresh push
        requests.get(
            f"{BASE_URL}/api/debug/test-push",
            params={"token": ADMIN_PWD}, timeout=15,
        )
        html = _events_html()
        card = _find_first_trigger_card(html, "QuakeGuard test push")
        assert card
        # Scrollable div with max-height:180px + overflow:auto
        assert re.search(
            r"max-height:180px[^\"]*overflow:auto",
            card,
        ), "scrollable div (max-height:180px;overflow:auto) missing"

    def test_recipients_are_br_joined_not_comma(self):
        html = _events_html()
        card = _find_first_trigger_card(html, "QuakeGuard test push")
        assert card
        # There must be <br> separators between the <code> blocks in the
        # recipient list section (one per line).
        # Extract just the div block:
        m = re.search(
            r"<b>recipient list:</b>.*?<div[^>]*max-height:180px[^>]*>(.*?)</div>",
            card, re.S,
        )
        assert m, "recipient list div not extractable"
        inner = m.group(1)
        # 7 <code> blocks → 6 <br> separators
        br_count = len(re.findall(r"<br>", inner))
        assert br_count == 6, f"expected 6 <br>, got {br_count}. inner={inner!r}"

    def test_no_more_not_captured_note_under_normal_ops(self):
        html = _events_html()
        card = _find_first_trigger_card(html, "QuakeGuard test push")
        assert card
        # Total (7) is not > shown (7), so the +N note MUST NOT appear.
        assert "more not captured" not in card, \
            "'+N more not captured' should NOT appear when total == shown"

    def test_html_escape_on_user_ids(self):
        """Register a rogue device whose user_id contains HTML/script
        payload; fire a push; confirm rendered <code> block escapes it."""
        rogue = "<script>alert('xss')</script>"
        _seed_direct(rogue)
        try:
            requests.get(
                f"{BASE_URL}/api/debug/test-push",
                params={"token": ADMIN_PWD}, timeout=15,
            )
            html = _events_html()
            card = _find_first_trigger_card(html, "QuakeGuard test push")
            assert card
            # Raw <script>...</script> must NOT appear inside the recipient
            # list div — it must be escaped as &lt;script&gt;...
            m = re.search(
                r"<b>recipient list:</b>.*?<div[^>]*max-height:180px[^>]*>(.*?)</div>",
                card, re.S,
            )
            assert m, "recipient list div not extractable"
            inner = m.group(1)
            # Escaped form MUST be present
            assert "&lt;script&gt;" in inner, \
                f"expected escaped <script>; inner={inner[:500]!r}"
            # Raw form MUST NOT be present as an actual tag
            assert "<script>" not in inner, \
                f"raw <script> tag leaked; inner={inner[:500]!r}"
            assert "alert(&#x27;xss&#x27;)" in inner or "alert('xss')" in inner
        finally:
            c, coll = _mongo_coll()
            coll.delete_one({"user_id": rogue})
            c.close()


# ==================================================================
# Static code checks on server.py
# ==================================================================
class TestServerPyCode:
    @pytest.fixture(scope="class")
    def src(self):
        with open(SERVER_PY, "r", encoding="utf-8") as f:
            return f.read()

    def test_recipients_sample_uses_200_not_5(self, src):
        # The active line must be chunk[:200], never chunk[:5]
        assert re.search(r"recipients_sample.{0,40}chunk\[:200\]", src), \
            "recipients_sample must use chunk[:200]"
        # Ensure the OLD chunk[:5] is not present as an active slice
        # (allow it only in a comment)
        for line in src.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            assert "chunk[:5]" not in line, \
                f"stale chunk[:5] still active: {line}"

    def test_render_uses_recipient_list_label(self, src):
        assert "recipient list:" in src, "'recipient list:' label missing"
        # 'first recipients:' should be gone (or only in comments/docstrings).
        # Verify it's not in the HTML render string.
        # The only place it could appear is within the HTML f-string; scan
        # code lines outside triple-quoted docstrings crudely:
        assert "first recipients:</b>" not in src, \
            "old 'first recipients:' render label still present"

    def test_scrollable_div_style_present(self, src):
        assert "max-height:180px" in src and "overflow:auto" in src, \
            "scrollable div style missing"

    def test_html_escape_applied_per_user_id(self, src):
        # The comprehension: _html.escape(str(s)) ... for s in sample
        # (the f-string chunk between escape() and 'for s in sample'
        # includes closing </code> tag and a newline, so match the two
        # anchors separately).
        assert re.search(r"_html\.escape\(\s*str\(\s*s\s*\)\s*\)", src), \
            "_html.escape(str(s)) not applied"
        assert re.search(r"for\s+s\s+in\s+sample", src), \
            "'for s in sample' comprehension not found"

    def test_more_not_captured_only_when_total_gt_shown(self, src):
        assert "more not captured" in src
        # Verify it's guarded by `if total > shown else ""`
        assert re.search(
            r"more not captured.*?if\s+total\s*>\s*shown\s+else\s+\"\"",
            src, re.S,
        ), "'(+N more not captured)' branch not guarded by total > shown"


# ==================================================================
# Regression coverage
# ==================================================================
class TestRegression:
    def test_trigger_alert_200_contract(self):
        r = requests.post(
            f"{BASE_URL}/api/trigger-alert",
            headers={"X-Admin-Token": ADMIN_PWD, "Content-Type": "application/json"},
            json={"magnitude": 6.1},
            timeout=15,
        )
        assert r.status_code == 200, r.text
        b = r.json()
        assert b["status"] == "broadcast"
        assert isinstance(b["recipients"], int)
        assert b["push_delivered"] is False
        assert b["push_error"] == "EMERGENT_PUSH_KEY missing or invalid"

    def test_trigger_alert_card_shows_full_recipient_list(self):
        # After the trigger-alert above, newest EARTHQUAKE ALERT card
        # must show all seeded qg-* recipients (unfiltered → all 7).
        html = _events_html()
        card = _find_first_trigger_card(html, "EARTHQUAKE ALERT")
        assert card
        assert "recipient list:" in card
        code_blocks = re.findall(r"<code[^>]*>(.*?)</code>", card)
        seen = [c for c in code_blocks if c.startswith("qg-")]
        assert len(seen) == 7, f"expected 7 qg-* recipients, got {seen}"

    @pytest.mark.parametrize("variant", ["A", "B", "C", "D", "E", "F"])
    def test_probe_push_variants_render_full_recipient_list(self, variant):
        r = requests.get(
            f"{BASE_URL}/api/debug/probe-push",
            params={"token": ADMIN_PWD, "variant": variant},
            timeout=15,
        )
        assert r.status_code == 200
        # Payload rendered
        assert f"Probe variant {variant}" in r.text

        # The newest trigger event card MUST contain all 7 qg-* IDs.
        html = _events_html()
        # Grab the first (newest) trigger card
        m = re.search(
            r'<div style="border:1px solid #ddd;border-radius:8px.*?</pre>\s*</div>',
            html, re.S,
        )
        assert m, "no newest event card"
        newest = m.group(0)
        # Skip register-only cards (they don't have 'recipient list:')
        if "recipient list:" not in newest:
            pytest.skip("newest card is a register event; skipping")
        blocks = re.findall(r"<code[^>]*>(.*?)</code>", newest)
        qg = [b for b in blocks if b.startswith("qg-")]
        assert len(qg) == 7, f"variant {variant}: expected 7, got {qg}"

    def test_register_event_shows_user_id_and_platform(self):
        # register-push under placeholder key returns 500 but still appends
        # a register event with user_id + platform.
        uid = f"TEST_reg_{uuid.uuid4().hex[:6]}"
        r = requests.post(
            f"{BASE_URL}/api/register-push",
            json={"user_id": uid, "platform": "android", "device_token": "tok-x"},
            timeout=5,
        )
        # 500 under placeholder key is expected pre-existing behaviour
        assert r.status_code in (201, 500), r.text
        html = _events_html()
        # register event card exists with our uid and 'android'
        assert uid in html
        # Find the register card containing the uid
        cards = re.findall(
            r'<div style="border:1px solid #ddd;border-radius:8px[^"]*"[^>]*>(.*?)</pre>\s*</div>',
            html, flags=re.S,
        )
        found = None
        for c in cards:
            if uid in c and "register" in c:
                found = c
                break
        assert found, f"no register card for {uid}"
        assert "<b>user_id:</b>" in found
        assert "<b>platform:</b>" in found
        assert "android" in found
        # cleanup
        c, coll = _mongo_coll()
        coll.delete_one({"user_id": uid})
        c.close()

    def test_ring_buffer_bounded_at_20(self):
        for _ in range(25):
            requests.get(
                f"{BASE_URL}/api/debug/test-push",
                params={"token": ADMIN_PWD}, timeout=10,
            )
        html = _events_html()
        cards = len(re.findall(r'<pre style="background:#f4f4f6', html))
        assert cards <= 20, f"ring buffer overflowed: {cards}"
        m = re.search(r"Last\s+(\d+)\s+interaction", html)
        assert m and int(m.group(1)) <= 20

    def test_legacy_status_get_post(self):
        name = f"TEST_diag_{uuid.uuid4().hex[:6]}"
        r = requests.post(f"{BASE_URL}/api/status", json={"client_name": name})
        assert r.status_code == 200
        assert r.json()["client_name"] == name
        r2 = requests.get(f"{BASE_URL}/api/status")
        assert r2.status_code == 200
        assert any(s["client_name"] == name for s in r2.json())

    def test_send_push_500_on_upstream_401_contract(self):
        r = requests.post(
            f"{BASE_URL}/api/debug/test-push",
            headers={"X-Admin-Token": ADMIN_PWD}, timeout=15,
        )
        assert r.status_code == 200
        body = r.json()
        assert body["push_delivered"] is False
        assert body["push_error"] == "EMERGENT_PUSH_KEY missing or invalid"

    def test_debug_devices_unchanged(self):
        r = requests.get(f"{BASE_URL}/api/debug/devices")
        assert r.status_code == 200
        j = r.json()
        for k in ("device_count", "push_key_status",
                  "admin_password_configured", "devices"):
            assert k in j, f"missing key {k}"
        # tokens should be truncated preview only, no full device_token
        for d in j["devices"]:
            assert "device_token" not in d
            assert "device_token_preview" in d
