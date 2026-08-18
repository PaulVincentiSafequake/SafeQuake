"""Regression guard: a casualty's coordinates must never leave to a third party.

2026-06-18 (Paul's batch-6 verification): audit rows and the per-person history
rendered a "📍 map" link to google.com/maps/place/<lat>,<lon>. Every operator
click disclosed the exact position of a trapped person to Google inside a URL.

The link is now an internal recentre of our own Leaflet map. This test exists
so the pattern cannot come back by copy-paste. If an external routing link is
ever genuinely wanted it must be a separate, explicitly-labelled action that
also writes an audit row — at which point this test needs updating with a
written reason, not silently.
"""
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

# Files that render coordinates to a human.
RENDERING_FILES = [
    ROOT / "backend" / "server.py",
    ROOT / "memory" / "dashboard_build" / "index.html",
    ROOT / "memory" / "dashboard-audit-log.snippet.html",
]

# Any third-party map service that would receive the coordinates in a URL.
FORBIDDEN = ["maps/place", "maps.google", "maps.apple.com", "openstreetmap.org/?mlat"]


@pytest.mark.parametrize("path", RENDERING_FILES, ids=lambda p: p.name)
def test_no_outbound_map_links_with_coordinates(path):
    assert path.exists(), f"{path} moved — update this guard"
    for lineno, line in enumerate(path.read_text().splitlines(), start=1):
        stripped = line.strip()
        # Comments may name the pattern (they explain the fix).
        if stripped.startswith(("#", "//", "*", "/*", "<!--")):
            continue
        for pattern in FORBIDDEN:
            assert pattern not in line, (
                f"{path.name}:{lineno} sends coordinates to a third party "
                f"({pattern}). Recentre the internal map instead."
            )
