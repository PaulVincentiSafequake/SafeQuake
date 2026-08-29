"""2026-08-29 (Paul): "Recentre on Malta" must go to a FIXED home
position showing Malta and Gozo, never a position worked out from
wherever the markers happen to be. Same rule applies to the very
first paint of the dashboard map.

These tests guard the dashboard HTML against three easy regressions:

  1. Someone re-introduces `setView(MALTA_CENTER, MALTA_ZOOM)` as
     the recentre action. A fixed centre+zoom cannot guarantee both
     Malta AND Gozo are visible on every screen size — a portrait
     phone or an ultra-wide monitor can push Gozo off the top or
     shove the view up into southern Sicily. The recentre control
     MUST use `fitBounds` on a hard-coded rectangle.
  2. Someone helpfully adds `map.fitBounds(markerGroup.getBounds())`
     after the data loads. Then a stray test pin, or an alert search
     radius that reaches into Sicily, drags the "home" view off Malta.
  3. Someone re-adds the earlier `setView([...], 13)` initial view —
     which cropped Gozo out on the first paint entirely.
"""
from pathlib import Path

DASH = (Path(__file__).resolve().parents[2]
        / "memory" / "dashboard_build" / "index.html").read_text()


def test_recentre_uses_fitbounds_on_a_fixed_malta_gozo_box():
    # The single source of truth for "home" must exist as a
    # LatLngBounds constant — not a centre+zoom pair that lies
    # about aspect ratios.
    assert "MALTA_GOZO_BOUNDS = L.latLngBounds(" in DASH, (
        "The Recentre button must fitBounds() a fixed Malta+Gozo "
        "rectangle. See dashboard_build/index.html."
    )
    # The recentre function must actually call fitBounds on those
    # bounds — not setView on a marker-derived point.
    assert "map.fitBounds(MALTA_GOZO_BOUNDS" in DASH


def test_recentre_button_calls_the_fixed_home_function_not_setview():
    # The button handler must go via `recentreOnMalta()` (which is
    # the only sanctioned mover of the map to "home"), not
    # `map.setView(MALTA_CENTER, ...)` — that was the pre-fix code
    # and the exact thing that could land in Sicily on a tall
    # viewport.
    assert 'btn.addEventListener("click", function () { recentreOnMalta(); });' in DASH


def test_no_marker_derived_fitbounds_anywhere():
    # A "helpful" auto-fit to whatever markers happen to be on the
    # map at data-load time IS the bug the user reported. Ban it.
    # (Legitimate fitBounds calls target the fixed MALTA_GOZO_BOUNDS
    # constant — those are the only allowed ones.)
    # Strip line comments (`// ...`) before scanning so a "do NOT do
    # this" comment doesn't trip the guard.
    import re
    scrub = re.sub(r"//[^\n]*", "", DASH)
    forbidden = [
        "fitBounds(markers",
        "fitBounds(featureGroup",
        "fitBounds(L.featureGroup",
        ").getBounds())",            # any .getBounds() being handed anywhere
        "extendBounds",
        "map.locate(",               # geolocation-based recentre
    ]
    for token in forbidden:
        assert token not in scrub, (
            f"{token!r} in the dashboard suggests someone made the "
            f"map view derive from marker positions. Home must be a "
            f"FIXED Malta+Gozo bounding box (MALTA_GOZO_BOUNDS)."
        )


def test_initial_map_paints_the_same_fixed_home_box():
    # First paint uses the same rectangle so a fresh visitor sees
    # Malta AND Gozo, not just central Malta at zoom 13.
    assert "L.map(\"map\", { scrollWheelZoom: false }).fitBounds(" in DASH
    # And there must be no leftover of the old setView(..., 13) init.
    assert 'setView([35.8997, 14.5146], 13)' not in DASH


def test_map_container_is_capped_to_the_visible_viewport():
    # 2026-08-29 (Paul, follow-up): "the map is taller than my browser
    # window, so its centre sits below the fold. After pressing
    # Recentre on Malta I still have to scroll down to see Malta."
    #
    # Root cause: `#map-wrap` was `flex: 1 1 60%` in a wrapping flex
    # row, so it grew to match the SIDEBAR's tall intrinsic content
    # (count pills, count notes, off-board, walking wounded list…),
    # pushing the map's geometric centre below the viewport.
    #
    # Fix: `#map-wrap` gets a viewport-relative height AND
    # `align-self: flex-start` so the sidebar's height no longer
    # stretches it. Guard both so the fix cannot silently regress.
    map_wrap_rule = DASH.split("#map-wrap {", 1)[1].split("}", 1)[0]
    assert "align-self: flex-start" in map_wrap_rule, (
        "#map-wrap must set `align-self: flex-start` so the sidebar's "
        "intrinsic height does not stretch the map below the fold."
    )
    # A viewport-relative height cap must exist (100dvh preferred so
    # mobile URL-bar toggling doesn't clip the map, but any of the
    # viewport units is acceptable — just NOT a fixed pixel height).
    assert any(unit in map_wrap_rule for unit in ("100dvh", "100vh", "100svh")), (
        "#map-wrap must be sized against the viewport height so the "
        "map fits inside the visible screen without scrolling."
    )
