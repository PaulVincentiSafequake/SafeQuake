"""#342 — Regression tests for the alarm-panel card layout fix.

Bug (2026-09-05, Paul, live re-test):
    "the note box sits under the red header, so its first line is unreadable
    and it also hides 'Send a team — IMMEDIATE, cannot move.' This is the
    fourth time content has been clipped behind a fixed element."

Root cause: `.qg-alarm` used a horizontal flexbox with `align-self: center`
on the Acknowledge button. On a tall body (a since line + a no-loc note +
an expanded story), the button hovered next to the action line, and the
amber note visually sat between the header and the action. Fixed by
switching `.qg-alarm` to a grid where the button sits on its OWN row
below the body — no vertical centring, no second axis for drift.

These tests grep the deployed dashboard file for the exact CSS rules
the fix relies on, so a regression that reverts any single one shows
up here immediately.
"""
import re
from pathlib import Path

DASH = Path("/app/memory/dashboard_build/index.html")


def read_dash() -> str:
    return DASH.read_text(encoding="utf-8")


def _rule_block(css: str, selector: str) -> str:
    """Return the { ... } body of the first CSS rule matching *selector*.

    Selector is matched literally against a start-of-rule position; the
    body is captured up to the first balanced closing brace so nested
    at-rules inside media queries do not confuse the extractor.
    """
    # Escape regex metacharacters in selector, tolerate whitespace before {.
    pat = re.compile(
        r"(?:^|\n)\s*" + re.escape(selector) + r"\s*\{([^{}]*)\}",
        re.DOTALL,
    )
    m = pat.search(css)
    assert m, f"could not find CSS rule for selector {selector!r}"
    return m.group(1)


def test_qg_alarm_is_grid_not_flex() -> None:
    """`.qg-alarm` must use grid so the button can live on its own row."""
    body = _rule_block(read_dash(), ".qg-alarm")
    assert "display: grid" in body, (
        ".qg-alarm must be display:grid to keep the button on its own row"
    )
    # A stray leftover `display: flex` would silently re-introduce the
    # bug — assert it is gone.
    assert "display: flex" not in body, ".qg-alarm must not be display:flex any more"
    # The grid template must place shape (22px) + body (1fr) on row 1.
    assert "grid-template-columns: 22px 1fr" in body, (
        ".qg-alarm needs a 2-column grid: shape + body"
    )
    assert "grid-template-rows: auto auto" in body, (
        ".qg-alarm needs 2 rows: content row + action row"
    )


def test_qg_alarm_ack_on_own_row_full_width() -> None:
    """`.qg-alarm-ack` must sit on row 2, span full width, no vertical centring."""
    body = _rule_block(read_dash(), ".qg-alarm-ack")
    assert "grid-row: 2" in body, "Acknowledge must be on grid row 2 (below body)"
    assert "grid-column: 2" in body, "Acknowledge must span the body column"
    assert "width: 100%" in body, "Acknowledge must span full width of the body column"
    # `align-self: center` on a horizontal flex was the root of the bug
    # (button vertically centres on a tall body and reads as covering
    # the middle content). Assert it is gone.
    assert "align-self: center" not in body, (
        "Acknowledge must not use align-self: center any more — that was the #342 root cause"
    )
    # Touch-target guidance: iOS/Android both require ≥44px.
    m = re.search(r"min-height:\s*(\d+)px", body)
    assert m and int(m.group(1)) >= 44, "Acknowledge tap target must be ≥ 44px tall"


def test_qg_alarm_body_min_width_zero() -> None:
    """A grid item that holds text needs `min-width:0` to allow wrap."""
    body = _rule_block(read_dash(), ".qg-alarm-body")
    assert "min-width: 0" in body, (
        ".qg-alarm-body must have min-width:0 so long headlines wrap inside the grid column"
    )
    assert "grid-column: 2" in body
    assert "grid-row: 1" in body


def test_qg_alarm_no_loc_is_block_full_width() -> None:
    """The amber No-Saved-Location note must be its own line, always."""
    body = _rule_block(read_dash(), ".qg-alarm-no-loc")
    assert "display: block" in body, (
        ".qg-alarm-no-loc must be display:block so it can never share a line "
        "with an adjacent inline element"
    )
    assert "width: 100%" in body
    assert "box-sizing: border-box" in body


def test_qg_card_no_loc_is_block_full_width() -> None:
    """The sidebar-triage No-Saved-Location note gets the same treatment."""
    body = _rule_block(read_dash(), ".qg-card-no-loc")
    # It used to be display:inline-block, which is the risk mode.
    assert "display: block" in body
    assert "display: inline-block" not in body, (
        ".qg-card-no-loc must not be inline-block any more — that was in the #342 sweep"
    )
    assert "width: 100%" in body


def test_annun_rows_has_no_inner_scroll() -> None:
    """The double-scroll (topstrip AND rows) was part of the clip family."""
    body = _rule_block(read_dash(), "#qg-annun-rows")
    assert "overflow-y" not in body, (
        "#qg-annun-rows must not have its own overflow-y — the outer topstrip scrolls"
    )
    assert "max-height" not in body, (
        "#qg-annun-rows must not cap its own height — the outer topstrip caps it (#295)"
    )
    # It still lays out as a vertical stack.
    assert "display: flex" in body
    assert "flex-direction: column" in body


def test_topstrip_still_capped_at_50vh() -> None:
    """The outer scroll (#295 doctrine) must remain — cards must never push the map off screen."""
    body = _rule_block(read_dash(), ".qg-topstrip")
    assert "max-height: 50vh" in body
    assert "overflow-y: auto" in body
    assert "position: sticky" in body


def test_coverage_detector_checks_lines_not_only_buttons() -> None:
    """`checkNothingIsCoveringTheButtons` must sample the action + note + headline."""
    text = read_dash()
    # The detector must reference every critical selector.
    assert 'addAll(".qg-alarm-ack")' in text, (
        "detector must sample per-card Acknowledge buttons"
    )
    assert 'addAll(".qg-alarm-action")' in text, (
        'detector must sample the "Send a team" action line'
    )
    assert 'addAll(".qg-alarm-no-loc")' in text, (
        'detector must sample the amber "No saved location" note'
    )
    assert 'addAll(".qg-alarm-headline")' in text, (
        "detector must sample the alarm headline (who this alarm is about)"
    )


def test_alarm_scroll_margin_top_for_scroll_into_view() -> None:
    """Belt-and-braces: `.qg-alarm` sets scroll-margin-top so scroll-into-view lands cleanly."""
    body = _rule_block(read_dash(), ".qg-alarm")
    assert "scroll-margin-top" in body, (
        ".qg-alarm should reserve scroll-margin-top so future scrollIntoView() calls "
        "don't leave the card's top row grazing the top of the scroll container"
    )
