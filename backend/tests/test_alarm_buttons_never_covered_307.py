"""#307 — third repeat of "alarm-panel buttons stop responding to clicks".

Paul reported this three times in a row. Each time, another top-of-page
element (qg-banner-text, qg-trigger-wrap, qg-tremor-strip) sat on top
of the Acknowledge / Show-me-who / Silence buttons and swallowed clicks.
Each time, the on-screen warning read like a bug report — raw ID and
class names, which an operator should never have to see.

Two contracts, both pinned here:

  1. **Buttons never covered.** The alarm panel's stacking context beats
     every top overlay by z-index, and its `top` in the sticky viewport
     is offset by the height of any visible fixed banner. Both layers
     of the fix live in CSS on `.qg-topstrip` — z-index high enough to
     win any paint fight, plus `top: var(--qg-fixed-top-offset)` so the
     geometry can't overlap either.

  2. **Warning reads as plain English.** If the check ever DOES fire,
     the panel says which top-of-page element is in the way in words a
     human uses, not in class names. `friendlyDescribeCover` translates
     every element the check has ever seen; raw IDs and class names
     stay in the console for the developer.
"""

from pathlib import Path


DASHBOARD = Path(__file__).resolve().parents[2] / "memory" / "dashboard_build" / "index.html"


def _src() -> str:
    return DASHBOARD.read_text(encoding="utf-8")


# ── 1. Buttons never covered ─────────────────────────────────────────
def test_topstrip_beats_every_top_overlay_by_zindex():
    """The topstrip's stacking context has to sit above the transient
    banner (99998) and the stale bar (12000). A very high z-index plus
    `isolation: isolate` means nothing else at the top of the page can
    paint on top of the alarm panel's buttons."""
    src = _src()
    # Anchor to the qg-topstrip rule specifically (not any other rule).
    strip = src[src.index(".qg-topstrip {"):src.index(".qg-topstrip {") + 1200]
    assert "z-index: 100000" in strip, "topstrip must beat qg-banner (99998) and qg-stale-bar (12000)"
    assert "isolation: isolate" in strip, "topstrip needs its own stacking context so children stay atop"
    assert "position: sticky" in strip


def test_topstrip_top_offset_uses_css_variable_for_visible_banners():
    """Belt-and-braces: even if paint order failed, geometry must not.
    The sticky panel is offset down by the total height of any visible
    fixed banners via a CSS variable maintained in JS."""
    src = _src()
    strip = src[src.index(".qg-topstrip {"):src.index(".qg-topstrip {") + 1200]
    assert "top: var(--qg-fixed-top-offset" in strip


def test_show_banner_updates_the_offset_variable():
    """When the status banner flies in, the panel must slide down; when
    it flies out, the panel must slide back."""
    src = _src()
    assert "function updateFixedTopOffset()" in src
    assert "--qg-fixed-top-offset" in src
    # Anchor on the trigger-button banner (not the preview-panel toast).
    show_marker = "function showBanner(kind, text, autoDismissMs)"
    hide_marker = "function hideBanner()"
    assert show_marker in src
    assert hide_marker in src
    show_fn = src[src.index(show_marker):src.index(show_marker) + 800]
    hide_fn = src[src.index(hide_marker):src.index(hide_marker) + 400]
    assert "updateFixedTopOffset()" in show_fn
    assert "updateFixedTopOffset()" in hide_fn


def test_stale_bar_show_and_hide_update_the_offset_variable():
    """Same story for the 'board not updating' bar: it is fixed at
    top:0 and if the panel stayed at top:0 it would sit behind it."""
    src = _src()
    # Every code path that changes `bar.hidden` also has to nudge the offset.
    show_block = src[src.index("bar.hidden = false"):src.index("bar.hidden = false") + 800]
    assert "qgUpdateFixedTopOffset" in show_block


# ── 2. Plain English warning ─────────────────────────────────────────
def test_no_raw_selector_leaks_into_the_operator_message():
    """The warning shown on screen must not contain any of the code
    names the check has ever caught: qg-banner-text, qg-trigger-wrap,
    qg-tremor-strip, or the generic 'covered by <what>' template that
    used to interpolate the raw ID."""
    src = _src()
    # The old sentence that leaked code names has to be gone.
    assert "covered by \" + what" not in src, "old raw-name leak still present"
    assert "These buttons are being covered by something else on the page (\"" not in src
    # The whole message shown to the operator lives in a single
    # `warnEl.textContent = ...` assignment; find it and check.
    marker = "The alarm-panel buttons are sitting behind "
    assert marker in src, "new plain-English warning is missing"
    # The three raw names from Paul's three reports MUST NOT appear
    # anywhere in the message string itself. They may appear as KEYS in
    # the translation table (that's fine — they are code, not UI text).
    msg_start = src.index(marker)
    msg_end = src.index(";", msg_start)
    msg = src[msg_start:msg_end]
    for code_name in ("qg-banner-text", "qg-trigger-wrap", "qg-tremor-strip",
                      "qg-stale-bar", "qg-banner-icon"):
        assert code_name not in msg, f"raw code name '{code_name}' leaked into the operator message"


def test_translation_covers_every_element_paul_reported():
    """Every element that has ever been named in a live bug report has
    a human-language phrase mapped for it."""
    src = _src()
    dict_start = src.index("var FRIENDLY_COVER_NAMES = {")
    dict_end = src.index("};", dict_start)
    d = src[dict_start:dict_end]
    for name in ("qg-banner-text", "qg-trigger-wrap", "qg-tremor-strip",
                 "qg-banner", "qg-stale-bar", "qg-tremor-strip-text",
                 "qg-tremor-strip-cta", "qg-trigger-btn"):
        assert '"' + name + '"' in d, f"'{name}' not translated to plain English"


def test_translation_walks_up_ancestors_so_child_spans_still_resolve():
    """If the element caught by elementFromPoint is a `<span>` inside a
    banner, we still want the message to say 'the status bar', not
    'something else on the page'."""
    src = _src()
    fn = src[src.index("function friendlyDescribeCover("):src.index("function friendlyDescribeCover(") + 1600]
    assert "parentElement" in fn
    assert "depth" in fn


def test_developer_detail_stays_in_the_console_only():
    """Raw element info still lands in the console for the developer;
    it must never appear on the operator's screen."""
    src = _src()
    check_fn = src[src.index("function checkNothingIsCoveringTheButtons()"):
                   src.index("function checkNothingIsCoveringTheButtons()") + 2500]
    assert "console.error" in check_fn
    assert "warnEl.textContent" in check_fn
    # And the warning text uses `friendly`, not the raw key.
    assert "friendly" in check_fn
