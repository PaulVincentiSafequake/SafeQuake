"""#307 — four repeats of "alarm-panel buttons stop responding to clicks".

Paul reported this four times in a row. Each time, another top-of-page
element (qg-banner-text, qg-trigger-wrap, qg-tremor-strip, qg-rescue-toast)
sat on top of the Acknowledge / Show-me-who / Silence buttons and swallowed
clicks. Each time, the on-screen warning read like a bug report — raw ID
and class names, which an operator should never have to see.

The fourth report changed the shape of the fix: instead of naming one
more element, the sticky alarm panel now offsets itself below the *whole
family* of currently visible fixed-top bars in one sweep. Any future top
bar added to the dashboard is handled automatically the moment it renders.

Two contracts, both pinned here:

  1. **Buttons never covered — for the whole family at once.** The panel's
     stacking context beats every top overlay by z-index, and its `top` in
     the sticky viewport is offset by the height of *every* visible
     position:fixed top bar (computed generically from the DOM, not from a
     hard-coded list). Both layers of the fix live on `.qg-topstrip`.

  2. **Warning reads as plain English.** If the check ever DOES fire, the
     panel says which top-of-page element is in the way in words a human
     uses, not in class names. `friendlyDescribeCover` translates every
     element the check has ever seen; raw IDs and class names stay in the
     console for the developer.
"""

from pathlib import Path


DASHBOARD = Path(__file__).resolve().parents[2] / "memory" / "dashboard_build" / "index.html"


def _src() -> str:
    return DASHBOARD.read_text(encoding="utf-8")


# ── 1. Buttons never covered ─────────────────────────────────────────
def test_topstrip_beats_every_top_overlay_by_zindex():
    """The topstrip's stacking context has to sit above the transient
    banner (99998), the rescue toast (99998) and the stale bar (12000).
    A very high z-index plus `isolation: isolate` means nothing else at
    the top of the page can paint on top of the alarm panel's buttons."""
    src = _src()
    strip = src[src.index(".qg-topstrip {"):src.index(".qg-topstrip {") + 1200]
    assert "z-index: 100000" in strip, "topstrip must beat qg-banner (99998), qg-rescue-toast (99998), qg-stale-bar (12000)"
    assert "isolation: isolate" in strip, "topstrip needs its own stacking context so children stay atop"
    assert "position: sticky" in strip


def test_topstrip_top_offset_uses_css_variable_for_visible_banners():
    """Belt-and-braces: even if paint order failed, geometry must not.
    The sticky panel is offset down by the total height of any visible
    fixed banners via a CSS variable maintained in JS."""
    src = _src()
    strip = src[src.index(".qg-topstrip {"):src.index(".qg-topstrip {") + 1200]
    assert "top: var(--qg-fixed-top-offset" in strip


def test_offset_scan_is_generic_not_a_hard_coded_list():
    """The fourth #307 report proved that naming top bars one at a time
    is whack-a-mole. The offset function must now scan the DOM for
    *every* visible position:fixed element pinned to the top of the
    viewport — no hard-coded list of IDs."""
    src = _src()
    marker = "function updateFixedTopOffset()"
    fn = src[src.index(marker):src.index(marker) + 3500]
    # It must iterate over body descendants and read computed style.
    assert "getElementsByTagName" in fn or "querySelectorAll" in fn, \
        "offset scan must walk the DOM, not a fixed ID list"
    assert "getComputedStyle" in fn, "must ask the browser what position each element has"
    assert 'cs.position !== "fixed"' in fn or "position === 'fixed'" in fn or \
        "position !== 'fixed'" in fn, "must filter by computed position:fixed"
    # It must skip the alarm panel itself and its descendants.
    assert ".qg-topstrip" in fn and "panel.contains" in fn, \
        "must exclude the topstrip and its own children from the scan"
    # It must skip full-viewport backdrops (open modals) so those don't
    # slam the offset to full window height.
    assert "innerHeight" in fn and "0.9" in fn, "must exclude full-screen backdrops"
    # It must NOT be hard-coded to just qg-banner + qg-stale-bar as
    # the old fix was — that list is exactly what got Paul on the
    # fourth iteration.
    assert 'getElementById("qg-banner")' not in fn, \
        "no more hard-coded id list — must scan generically"
    assert 'getElementById("qg-stale-bar")' not in fn, \
        "no more hard-coded id list — must scan generically"


def test_mutation_observer_catches_new_top_bars_automatically():
    """Future top-of-page bars added to the dashboard must be picked up
    without any code change here. A MutationObserver watching body-wide
    class/hidden/style changes re-runs the offset scan so a newly-shown
    fixed bar reserves its space the moment it appears."""
    src = _src()
    assert "MutationObserver" in src, "must observe DOM mutations for new top bars"
    obs_start = src.index("watchForNewTopBars")
    obs_block = src[obs_start:obs_start + 1500]
    assert "attributeFilter" in obs_block
    assert "class" in obs_block and "hidden" in obs_block and "style" in obs_block, \
        "must react to class / hidden / style changes on any element"
    assert "updateFixedTopOffset" in obs_block, "the observer must trigger the offset scan"
    # transitionend / animationend cover slide-in animations that end
    # without an attribute change on the animated element itself.
    assert "transitionend" in obs_block and "animationend" in obs_block


def test_show_banner_updates_the_offset_variable():
    """When the status banner flies in, the panel must slide down; when
    it flies out, the panel must slide back."""
    src = _src()
    assert "function updateFixedTopOffset()" in src
    assert "--qg-fixed-top-offset" in src
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
    show_block = src[src.index("bar.hidden = false"):src.index("bar.hidden = false") + 800]
    assert "qgUpdateFixedTopOffset" in show_block


def test_rescue_toast_show_and_hide_update_the_offset_variable():
    """The fourth #307 report. `showToast` and `hideToast` in the
    rescue module also drop a fixed-top bar in and out; they must
    nudge the offset the same way `showBanner` / `hideBanner` do."""
    src = _src()
    show_marker = "function showToast(kind, text, autoDismissMs)"
    hide_marker = "function hideToast()"
    assert show_marker in src
    assert hide_marker in src
    show_fn = src[src.index(show_marker):src.index(show_marker) + 1000]
    hide_fn = src[src.index(hide_marker):src.index(hide_marker) + 400]
    assert "qgUpdateFixedTopOffset" in show_fn, \
        "rescue toast must trigger the offset scan when it slides in"
    assert "qgUpdateFixedTopOffset" in hide_fn, \
        "rescue toast must trigger the offset scan when it slides out"


# ── 2. Plain English warning ─────────────────────────────────────────
def test_no_raw_selector_leaks_into_the_operator_message():
    """The warning shown on screen must not contain any of the code
    names the check has ever caught, or the generic 'covered by <what>'
    template that used to interpolate the raw ID."""
    src = _src()
    assert "covered by \" + what" not in src, "old raw-name leak still present"
    assert "These buttons are being covered by something else on the page (\"" not in src
    marker = "The alarm-panel buttons are sitting behind "
    assert marker in src, "new plain-English warning is missing"
    msg_start = src.index(marker)
    msg_end = src.index(";", msg_start)
    msg = src[msg_start:msg_end]
    for code_name in ("qg-banner-text", "qg-trigger-wrap", "qg-tremor-strip",
                      "qg-stale-bar", "qg-banner-icon", "qg-rescue-toast"):
        assert code_name not in msg, f"raw code name '{code_name}' leaked into the operator message"


def test_translation_covers_every_element_paul_reported():
    """Every element that has ever been named in a live bug report has
    a human-language phrase mapped for it — including the fourth one
    (`qg-rescue-toast`)."""
    src = _src()
    dict_start = src.index("var FRIENDLY_COVER_NAMES = {")
    dict_end = src.index("};", dict_start)
    d = src[dict_start:dict_end]
    for name in ("qg-banner-text", "qg-trigger-wrap", "qg-tremor-strip",
                 "qg-banner", "qg-stale-bar", "qg-tremor-strip-text",
                 "qg-tremor-strip-cta", "qg-trigger-btn", "qg-rescue-toast"):
        assert '"' + name + '"' in d, f"'{name}' not translated to plain English"


def test_translation_walks_up_ancestors_so_child_spans_still_resolve():
    """If the element caught by elementFromPoint is a `<span>` inside a
    banner, we still want the message to say 'the status bar', not
    'something else on the page'. This is the same walk that resolves a
    click on `.msg` inside `#qg-rescue-toast` to the toast's phrase."""
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
    assert "friendly" in check_fn
