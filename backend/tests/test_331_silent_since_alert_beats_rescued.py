"""#331 (Paul, 2026-08-29 — verbatim):

  "When a new alert fires, anyone previously marked safe or rescued
   stays hidden on the map until they answer. I saw this on my own
   phone — alert running for a minute, map completely empty, because
   I was still marked rescued from an earlier test. My pin only came
   back once I answered. That's wrong, and it's dangerous. The rule
   is that the moment an alert goes out, every phone we alerted
   shows red straight away. Being previously rescued cannot be an
   exception — those are exactly the people standing in a damaged
   building when an aftershock hits. Please make a new alert reset
   previously safe and rescued people back to red and visible, like
   everyone else, until they answer. Nothing is deleted — their
   earlier rescue stays in the history."

Doctrine encoded here (guarded so a future refactor cannot quietly
regress it back to the state Paul reported):

  1. `people_counts.map_color`  — silent-since-alert ranks ABOVE
     rescued_at. A rescued person with `last_alerted_at` > `updated_at`
     comes back as "red" on the map.

  2. `people_counts.load_board` — the row still counts as
     `ever_needed_help`, so it stays on the working board. Rescue
     history is preserved (rescued_at, rescued_by, pre_rescue_*).

  3. Dashboard `matchesFilter` — a rescued-and-silent row is NOT hidden
     by the "Show rescued" toggle. It follows the RED colour toggle
     like every other alerted-silent phone.

  4. Dashboard `markerVisual` — a rescued-and-silent row is drawn with
     the SILENT-RED visual ("never answered" tag), not the muted green
     ✓ "found" visual, so it reads as an active unanswered alert.

  5. `mapColorFor` legacy fallback (used when a stale backend deploys
     a row without `map_color`) mirrors the same rule.
"""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
import subprocess

import sys
import os
# Match sibling test-file style so `import people_counts` works.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from people_counts import map_color, silent_since_alert, last_known_position

DASH_PATH = (Path(__file__).resolve().parents[2]
             / "memory" / "dashboard_build" / "index.html")
DASH = DASH_PATH.read_text()


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ── 1. Backend: silent-since-alert ranks above rescued_at ─────────────
class TestBackendMapColor:
    def test_rescued_then_alerted_now_shows_red_on_map(self):
        yesterday = _iso(_now() - timedelta(hours=18))
        this_morning = _iso(_now() - timedelta(minutes=15))
        row = {
            "status": "rescued",
            "rescued_at": yesterday,
            "rescued_by": "an operator",
            "pre_rescue_status": "trapped",
            "updated_at": yesterday,
            "last_alerted_at": this_morning,
        }
        # Nothing deleted from the row.
        assert row["rescued_at"] == yesterday
        assert row["rescued_by"] == "an operator"
        assert row["pre_rescue_status"] == "trapped"
        # But the map has them red because we alerted them since.
        assert silent_since_alert(row) is True
        assert map_color(row) == "red"

    def test_safe_then_alerted_still_shows_red_on_map(self):
        """Same doctrine for a 'safe' self-report — a previously safe
        person we alerted overnight comes back as red until they speak."""
        yesterday = _iso(_now() - timedelta(hours=12))
        an_hour_ago = _iso(_now() - timedelta(hours=1))
        row = {
            "status": "safe",
            "updated_at": yesterday,
            "last_alerted_at": an_hour_ago,
        }
        assert silent_since_alert(row) is True
        assert map_color(row) == "red"

    def test_rescued_without_a_new_alert_stays_off_the_live_map(self):
        """The rescue still stands when there has been no subsequent
        alert. Off the map by default; operator's Show-rescued toggle
        brings them back with the rescued visual."""
        yesterday = _iso(_now() - timedelta(hours=18))
        row = {
            "status": "rescued",
            "rescued_at": yesterday,
            "updated_at": yesterday,
            # No last_alerted_at set at all.
        }
        assert silent_since_alert(row) is False
        assert map_color(row) is None

    def test_rescued_where_rescue_happened_after_the_alert_stays_rescued(self):
        """Order matters. If the LAST thing we knew was 'rescued'
        (i.e. someone was rescued AFTER an earlier alert), the rescue
        stands. The bug the user reported is specifically the other
        way round — alert AFTER rescue."""
        earlier_alert = _iso(_now() - timedelta(hours=6))
        rescued_since = _iso(_now() - timedelta(hours=1))
        row = {
            "status": "rescued",
            "rescued_at": rescued_since,
            "updated_at": rescued_since,
            "last_alerted_at": earlier_alert,
        }
        assert silent_since_alert(row) is False
        assert map_color(row) is None

    def test_rescued_silent_since_alert_is_not_last_known(self):
        """Same treatment as any other silent-since-alert row: RED for
        'never answered the new alert', not the 'last-known-position'
        halo."""
        yesterday = _iso(_now() - timedelta(hours=18))
        this_morning = _iso(_now() - timedelta(minutes=15))
        row = {
            "status": "rescued",
            "rescued_at": yesterday,
            "updated_at": yesterday,
            "last_alerted_at": this_morning,
        }
        assert last_known_position(row) is False


# ── 2. Dashboard filter and visual override rescued when silent ──────
class TestDashboardBehavesTheSameWay:
    def test_matches_filter_treats_rescued_and_silent_as_red_toggle(self):
        """The 'Show rescued' toggle must NOT hide a rescued row that
        has been re-alerted; that row follows the RED toggle."""
        # Find the matchesFilter body and check the exact predicate.
        body = _extract_function_body("matchesFilter")
        # The rescued short-circuit is now conditional on NOT being
        # silent-since-alert.
        assert re.search(
            r"u\.status\s*===\s*[\"']rescued[\"']\s*&&\s*!u\.silent_since_alert",
            body,
        ), (
            "matchesFilter still returns showRescuedOnMap for every "
            "rescued row — a rescued+silent phone will stay hidden "
            "behind the Show-rescued toggle. Bug #331 has recurred."
        )

    def test_marker_visual_uses_silent_red_for_rescued_and_silent(self):
        """A rescued+silent row must render with the RED silent-red
        visual, not the green ✓ 'found' visual."""
        body = _extract_function_body("markerVisual")
        # The rescued short-circuit is now conditional on NOT being
        # silent-since-alert.
        assert re.search(
            r"u\.status\s*===\s*[\"']rescued[\"']\s*&&\s*!u\.silent_since_alert",
            body,
        ), (
            "markerVisual still returns the rescued visual for every "
            "rescued row — a rescued+silent phone will render as the "
            "green ✓ 'found' shape instead of the RED silent-red pin. "
            "Bug #331 has recurred."
        )

    def test_map_color_for_legacy_fallback_prefers_silent_over_rescued(self):
        """Legacy fallback path in mapColorFor (used only when a stale
        backend without map_color is running) must obey the same
        doctrine: silent-since-alert wins over rescued."""
        body = _extract_function_body("mapColorFor")
        # The u.status === "rescued" arm must come AFTER the
        # u.silent_since_alert arm.
        silent_idx = body.find("u.silent_since_alert")
        rescued_idx = body.find('u.status === "rescued"')
        assert silent_idx != -1 and rescued_idx != -1
        assert silent_idx < rescued_idx, (
            "mapColorFor legacy fallback still checks rescued BEFORE "
            "silent-since-alert. Under a stale backend the map will "
            "regress to the #331 behaviour."
        )


# ── 3. Wording change (#332) — the popup line reads as REPORTED ──────
class TestGroupSizeWording:
    """Verbatim from Paul (2026-02-XX, second pass): 'They said 4 people
    are here. There may be more we do not know about.' The line must
    not say 'people at this address' (ambiguous — sounds like something
    we know) and must never say those people are trapped (we only asked
    how many are there, not how many are hurt). Superseded the earlier
    'App user said N people are here including them' wording — 'App
    user' reads like a category noun rather than the person, and
    'including them' is redundant because the mobile app asks
    'including you, how many people are here?', so the answer already
    includes the answerer."""

    def test_qg_group_line_uses_the_new_wording(self):
        body = _extract_function_body("window.qgGroupLine")
        # The exact user-authored sentence, one instance per plural
        # branch (2..4 and 5_plus).
        assert (
            'They said " + n + " people are here. '
            in body
        ), "qgGroupLine does not use the new n-people wording"
        assert (
            'They said 5 or more people are here. '
            in body
        ), "qgGroupLine does not use the new 5+ wording"
        assert "There may be more we do not know about." in body, (
            "qgGroupLine is missing the second sentence about "
            "coverage — the whole point of the wording change."
        )

    def test_qg_group_line_no_longer_says_people_at_this_address(self):
        """The old wording is ambiguous — it reads like a claim we can
        make (an address we know). The new wording is scoped as what
        the app user told us. This test guards the removal."""
        body = _extract_function_body("window.qgGroupLine")
        assert "people at this address" not in body
        assert "Just this person" not in body

    def test_qg_group_line_no_longer_says_app_user_or_including_them(self):
        """Second-pass wording (2026-02-XX): the "App user said …
        including them" phrasing is replaced by "They said … . The
        old strings must be gone so a later drift back cannot go
        unnoticed."""
        body = _extract_function_body("window.qgGroupLine")
        # Strip // comments before scanning so a comment that EXPLAINS
        # the ban does not trip it.
        scrub = re.sub(r"//[^\n]*", "", body)
        assert "App user said" not in scrub, (
            "qgGroupLine has regressed to the old 'App user said' "
            "wording — Paul asked for 'They said' on the pin popup."
        )
        assert "including them" not in scrub, (
            "qgGroupLine still carries the redundant 'including them' "
            "clause — the mobile app already frames the question as "
            "'including you', so the answer already includes them."
        )

    def test_qg_group_line_never_implies_trapped(self):
        """Group size is a question about how many are here, not how
        many are hurt. The line must not mention 'trapped', 'injured',
        'hurt' or similar. Strip `// ...` comments before scanning so
        a comment that EXPLAINS the ban does not trip it."""
        body = _extract_function_body("window.qgGroupLine")
        scrub = re.sub(r"//[^\n]*", "", body)
        for banned in ("trapped", "injured", "hurt", "casualt"):
            assert banned not in scrub.lower(), (
                f"qgGroupLine now mentions {banned!r} — the group-size "
                "line must only report count, never severity."
            )

    def test_qg_group_line_behavioural_wording_via_node(self):
        import shutil
        node = shutil.which("node")
        if not node:
            import pytest
            pytest.skip("node not available — structural tests cover this")
        body = _extract_function_body("window.qgGroupLine")
        js = (
            "const fn = function (n) {" + body + "};"
            "const cases = ["
            "  [null, 'Group size not given'],"
            "  [1, /only person here/],"
            "  [2, /^They said 2 people are here\\. There may be more we do not know about\\.$/],"
            "  [3, /^They said 3 people are here\\. There may be more we do not know about\\.$/],"
            "  [4, /^They said 4 people are here\\. There may be more we do not know about\\.$/],"
            "  [5, /^They said 5 or more people are here\\. There may be more we do not know about\\.$/],"
            "];"
            "for (const [inp, want] of cases) {"
            "  const got = fn(inp);"
            "  const ok = want instanceof RegExp ? want.test(got) : got === want;"
            "  if (!ok) { console.error('BAD', JSON.stringify(inp), '->', got); process.exit(1); }"
            "  if (typeof got === 'string' && /trapped|injured|hurt|App user|including them/i.test(got)) {"
            "    console.error('LEAKED FORBIDDEN WORD:', got); process.exit(1);"
            "  }"
            "}"
            "console.log('ok');"
        )
        r = subprocess.run([node, "-e", js], capture_output=True, text=True, timeout=10)
        assert r.returncode == 0, (
            f"qgGroupLine wording mismatch: stdout={r.stdout!r} "
            f"stderr={r.stderr!r}"
        )


# ── Helpers ───────────────────────────────────────────────────────────
def _extract_function_body(name: str) -> str:
    """Return the { ... } body of a top-level JS `function name(...)` or
    `var/window.name = function(...)` declaration inside DASH. The
    match is deliberately anchor-heavy so it can't collide with a
    later inline function of the same name."""
    # Try both `function NAME(` and `NAME = function(` forms.
    patterns = [
        rf"function\s+{re.escape(name)}\s*\([^)]*\)\s*\{{",
        rf"{re.escape(name)}\s*=\s*function\s*\([^)]*\)\s*\{{",
    ]
    for p in patterns:
        m = re.search(p, DASH)
        if m:
            # Walk braces from the opening brace to find the match.
            i = m.end() - 1  # position of the '{'
            depth = 0
            start = i + 1
            while i < len(DASH):
                ch = DASH[i]
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        return DASH[start:i]
                i += 1
    raise AssertionError(f"Could not find function body for {name!r}")
