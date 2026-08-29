"""2026-08-29 (Paul): "real buttons on the dashboard look identical
to plain text — 'Show me who' is one example. An operator cannot
tell what is clickable."

These guards check that every disclosure and inline anchor an
operator can click renders with an obvious button treatment (a
bordered pill, a chevron, hover feedback, a focus ring) rather than
as bold body text with a tiny native `▸` triangle.

We check for the presence of the two shared CSS treatments and
their application to the four families that used to look like text:

  * `.qg-alarm-story summary`   → "What happened"        (alarm cards)
  * `.qg-alarm-names summary`   → "Show me who (N)"      (alarm cards)
  * `.qg-disclosure-btn` on the reference-tier admin `<details>`
    (tremor panel head, admin-tools panel head, registered-devices
    panel head, and the "Show me who" list in an export receipt)
  * `.qg-inline-btn` on the "Refresh now" anchor in the preview
    panel meta line
"""
from pathlib import Path

DASH = (Path(__file__).resolve().parents[2]
        / "memory" / "dashboard_build" / "index.html").read_text()


# ── 1. Alarm-card disclosures are pill buttons ────────────────────────
def test_alarm_story_and_names_are_styled_as_buttons_not_plain_text():
    # Both disclosures live under a single joined rule so they
    # can't drift apart in future edits.
    assert ".qg-alarm-story summary,\n  .qg-alarm-names summary {" in DASH
    # The joined rule must contain the button hallmarks: a border,
    # a background fill, padding, a min tap-height, and a chevron.
    block = DASH.split(".qg-alarm-story summary,\n  .qg-alarm-names summary {", 1)[1].split("}", 1)[0]
    assert "border" in block and "background" in block
    assert "padding" in block and "min-height" in block
    # The default `▸` triangle from <summary> is hidden, and a
    # custom chevron is added that rotates on open.
    assert "::-webkit-details-marker" in DASH
    assert '.qg-alarm-story[open] > summary::before' in DASH
    assert '.qg-alarm-names[open] > summary::before' in DASH


# ── 2. Reference-tier disclosures use .qg-disclosure-btn ──────────────
def test_reference_tier_disclosures_use_the_shared_button_class():
    # The three admin/reference panels — the tremor panel head, the
    # admin-tools panel head, and the registered-devices panel head
    # — all wear `.qg-disclosure-btn` now, not an inline heading
    # style. The class itself must exist with border+chevron rules.
    assert "summary.qg-disclosure-btn," in DASH
    assert "details.qg-disclosure-btn > summary {" in DASH
    # Chevron indicator + rotate-on-open, so the affordance is
    # visually the same for every panel using the class.
    assert 'content: "▸";' in DASH
    assert "details.qg-disclosure-btn[open] > summary::before" in DASH
    # And the three heads use it. Each summary encloses a specific
    # human-readable label; find the summary block by its enclosing
    # <details id="..."> and check the class on its <summary>.
    for panel_id, needle in (
        ("qg-tremor-panel",  "Tremor notifications"),
        ("qg-admintools",    "Admin testing tools"),
        ("qg-devices-panel", "Registered devices"),
    ):
        start = DASH.index(f'<details id="{panel_id}"')
        block = DASH[start:DASH.index("</summary>", start)]
        assert needle in block, f"expected label {needle!r} inside {panel_id}"
        assert "qg-disclosure-btn" in block, (
            f"'{needle}' summary is not using .qg-disclosure-btn — "
            "it will read as bold body text, not a button."
        )


# ── 3. "Show me who" in the export receipt is styled too ──────────────
def test_export_show_me_who_uses_the_shared_button_class():
    # The "Show me who" summary embedded in the export sheet used
    # to be a bare <details><summary>...</summary></details> — pure
    # text with a native triangle. It must now carry the class.
    assert '<details class="qg-disclosure-btn"' in DASH
    # And specifically the export-receipt "Show me who" variant
    # (the whoList template) is one of them.
    assert ('whoList = \'<details class="qg-disclosure-btn"'
            in DASH), (
        'The "Show me who" summary in the export sheet must use '
        '.qg-disclosure-btn so it does not read as plain text.'
    )
    # Sanity: still contains the words the operator reads.
    who_line = [ln for ln in DASH.splitlines()
                if 'whoList =' in ln and 'details' in ln][0]
    assert 'Show me who' in who_line


# ── 4. Inline anchor-as-button gets .qg-inline-btn ────────────────────
def test_refresh_now_is_a_pill_button_not_a_hyperlink():
    # The "Refresh now" anchor in the preview panel meta line used
    # to read as an underlined hyperlink between plain text. It
    # now carries `.qg-inline-btn`, the pill treatment that also
    # covers any future inline-action anchor.
    assert 'a.qg-inline-btn' in DASH
    assert 'class="qg-inline-btn" data-action="refresh"' in DASH
