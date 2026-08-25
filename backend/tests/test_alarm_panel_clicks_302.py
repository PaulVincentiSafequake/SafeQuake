"""#302 — the alarm panel's buttons must actually be clickable.

Paul, 2026-08-25 (production):
  > None of the alarm-panel buttons respond to clicks — not Acknowledge,
  > Test the sound, What happened, or Silence the sound. All show a pointer
  > cursor on hover, but clicking does nothing, no console error…
  > "Call alert off" does work correctly.
  > New finding: the flashing red border is still visible even after
  > signing out completely, to an unauthorised visitor.

Two separate faults, both mine, both from the batch he was testing:

1. THE SWALLOWED CLICK. The audio-arming listeners added for #285 run on
   `pointerdown`, and their callback re-drew the alarm panel. So the button
   under the operator's finger was destroyed and rebuilt between
   pointerdown and pointerup — and a browser only fires `click` when both
   landed on the same element. No click was ever generated: pointer
   cursor, no action, no error. Buttons in the fixed top bar were
   untouched, which is exactly the pattern he described. Reproduced in a
   real browser: `pointerdown` on the button, no `click` after it.

   Fixed twice over, because either alone would have been enough and this
   must never come back:
     * arming notifies ONCE and always on a later tick, never inside the
       pointer sequence;
     * the panel only writes to the DOM when the content has actually
       changed — the same rule the activity feed has followed since
       2026-08-12.

2. THE FLASHING LEAK. `refresh()` returns early when signed out, before it
   ever reaches the alarms, so the panel and the whole-window flashing
   were left exactly as they were when the session ended — a red alarm
   flashing at somebody with no right to see it. Paul's question,
   answered: a signed-out visitor sees no alarm at all.

These tests read the dashboard source, which is how this repo already
locks the phone's wording (`test_triage_egress.py`). A DOM test needs a
browser; a source test catches the exact regression and runs in
milliseconds.
"""
import os
import re

DASH = os.path.join(os.path.dirname(__file__), "..", "..",
                    "memory", "dashboard_build", "index.html")


def _src() -> str:
    with open(DASH, encoding="utf-8") as f:
        return f.read()


# ── 1. The swallowed click ────────────────────────────────────────────
def test_arming_never_redraws_inside_the_pointer_sequence():
    src = _src()
    block = src[src.index("function fireArmed()"):src.index("function startKeepAlive")]
    assert "armedNotified" in block, "arming must notify once, not on every click"
    assert "setTimeout" in block, (
        "the redraw must be deferred off the pointer sequence — this is the "
        "whole of #302"
    )


def test_the_alarm_panel_only_writes_to_the_dom_when_something_changed():
    """A rebuild of an unchanged panel destroys the very button an operator
    is pressing. Every write goes through the change check."""
    src = _src()
    start = src.index("function renderAlarms()")
    end = src.index("function wireAlarmButtons()")
    body = src[start:end]
    assert "setHtmlIfChanged(rowsEl" in body
    assert "setHtmlIfChanged(bulkEl" in body
    assert "setHtmlIfChanged(soundEl" in body
    # No raw innerHTML assignment may creep back into the panel renderer.
    raw = re.findall(r"^\s*(\w+)\.innerHTML\s*=", body, re.M)
    assert raw == [], f"raw innerHTML writes are back in renderAlarms: {raw}"


def test_a_hand_changed_button_label_is_restored():
    """"Acknowledging…" / "Playing…" are written by hand over the top of
    the cached HTML. Without forgetting the cache the next render sees no
    difference and the button stays stuck on that label."""
    src = _src()
    assert "function forgetHtml(" in src
    for btn in ("qg-annun-bulk", "qg-annun-sound"):
        assert f'forgetHtml(document.getElementById("{btn}"))' in src, btn


def test_every_panel_button_has_a_direct_listener_as_well():
    """Everything used to hang off one listener on `document`, so anything
    that stopped a click reaching the document killed all of them at once
    with no error to find."""
    src = _src()
    assert "function wireAlarmButtons()" in src
    assert "wireAlarmButtons();" in src
    assert "function handleAlarmClick(" in src


def test_the_board_checks_its_own_buttons_are_not_covered():
    """Paul had to diagnose a dead control by hand. The board should have
    told him."""
    src = _src()
    assert "function checkNothingIsCoveringTheButtons()" in src
    assert "elementFromPoint" in src
    assert "qg-annun-blocked" in src


# ── 2. The flashing overlay ───────────────────────────────────────────
def test_the_flashing_overlay_cannot_cover_anything():
    """It is four thin strips at the edges of the window and one tag at the
    bottom — there is no element over the middle of the screen at all, so
    it cannot swallow a click even if a browser ignores pointer-events."""
    src = _src()
    css = src[src.index(".qg-alarm-visual {"):src.index("/* #286 — one action")]
    assert "pointer-events: none !important" in css
    assert ".qg-alarm-visual * { pointer-events: none !important; }" in src
    for bar in ("qg-av-top", "qg-av-bottom", "qg-av-left", "qg-av-right"):
        assert bar in css, bar
    # The old single full-viewport box is gone.
    assert "qg-av-frame" not in src
    # And it no longer sits above the fixed top bars.
    assert "z-index: 2147483000" not in src


def test_a_signed_out_visitor_never_sees_an_alarm():
    src = _src()
    assert "function clearAlarmsForSignedOut()" in src
    # Cleared on the auth change itself, not only on the next poll…
    assert "if (!user) clearAlarmsForSignedOut();" in src
    # …and on a one-second tick, whatever happens to the polling.
    assert ("if (!isSignedIn() && document.body.classList.contains(\"qg-alarm-unacked\"))"
            in src)


def test_the_panel_reports_when_it_last_actually_sounded():
    """#302: "sound doesn't play despite #285's fix." The board now states
    when it last played the alarm, so the next test gives an answer instead
    of an argument."""
    src = _src()
    assert "Last sounded at" in src
    assert "It has not had to sound yet." in src
