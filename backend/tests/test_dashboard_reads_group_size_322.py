"""#322 (2026-08-29 — Paul):

  "On build 1.0.47, alert running, device QQ43D. I tapped 'I need help',
   chose 'seriously injured / can't move', then answered the group size
   question — 5 the first time, 4 the second. The app confirms it.
   The alert screen shows 'Reported: you are trapped/pinned' and
   'Reported: you and 3 others here.' with a Change link. But on the
   dashboard, the map pin has no number badge, and its popup says
   'Group size not given'. Same on the person's card. So the app has
   captured it and is showing it back to me, but it is not reaching the
   board. Please find out exactly where it is lost."

Investigation summary (kept as a test-file docstring so the next reader
can reconstruct the diagnosis without re-tracing the code):

  1. Follow-up sent?     YES — `chooseGroupSize` calls `submitCheckIn(..., true, size)`
                         which enqueues a fresh POST /api/status with
                         `group_size: "<bucket>"` on the offline queue.
  2. Rejected server-side? NO — StatusInPayload.group_size accepts the
                         bucket strings (`^(just_me|2|3|4|5_plus)$`),
                         they are stored on `device_status.group_size`
                         and appended to `status_events`.
  3. Missing from API? NO — /api/devices passes it straight through:
                         `"group_size": r.get("group_size")`.
  4. Dashboard reads it? **NO — this was the bug.**
                         The dashboard's ingest at line ~7325 tested
                         `typeof d.group_size === "number" && d.group_size >= 1`,
                         so every real bucket (`"2"`, `"4"`, `"5_plus"`)
                         was silently coerced to `null`, which is why
                         the popup said "Group size not given" and the
                         pin carried no badge.

Fix: convert the wire bucket to the numeric form the renderer wants
in the ingest, in ONE place. These guards check the exact mapping:

  just_me → 1  (no badge — "Just this person" in the line)
  "2"     → 2  ("2" badge, "2 people at this address")
  "3"     → 3  ("3" badge, "3 people at this address")
  "4"     → 4  ("4" badge, "4 people at this address")
  5_plus  → 5  ("5+" badge, "5 or more people at this address")
  null / unknown → null (no badge, "Group size not given")
"""
from pathlib import Path
import re
import subprocess

DASH_PATH = (Path(__file__).resolve().parents[2]
             / "memory" / "dashboard_build" / "index.html")
DASH = DASH_PATH.read_text()


def _extract_group_size_ingest_block() -> str:
    """Return the source of the `groupSize:` normalizer inside the ingest.
    Fails loudly if the shape has drifted so a well-meaning refactor
    can't silently un-normalize the field again."""
    m = re.search(r"groupSize:\s*\(function\s*\(raw\)\s*\{([^}]+)\}\)\(d\.group_size\)", DASH)
    assert m, (
        "The `groupSize:` field on the ingest normalizer must be a "
        "function that turns the wire bucket into a number. Bug #322 "
        "recurred: dashboard is not reading `d.group_size` correctly."
    )
    return m.group(1)


def test_dashboard_ingest_maps_every_wire_bucket_to_the_expected_number():
    """The renderer wants a number; the wire is a string bucket. The
    ingest normalizer must translate every bucket the backend can send."""
    block = _extract_group_size_ingest_block()
    # All five bucket strings must appear as branches so no bucket
    # falls silently to null.
    for bucket in ('"just_me"', '"5_plus"', '"2"', '"3"', '"4"'):
        assert bucket in block, (
            f"The dashboard ingest normalizer does not handle "
            f"{bucket} — bug #322 will recur for that bucket."
        )
    # The numeric targets that the renderer's qgGroupBadge/qgGroupLine
    # decide against — they use 1, 5 and 2..4 (via parseInt).
    assert "return 1" in block and "return 5" in block
    assert "parseInt(raw, 10)" in block


def test_dashboard_ingest_still_tolerates_a_pre_normalised_number():
    """Kept for safety: if any legacy caller ever fans in the numeric
    form (e.g. a fixture, a snapshot import, a future backend rewrite),
    we pass it through instead of throwing it away."""
    block = _extract_group_size_ingest_block()
    assert 'typeof raw === "number"' in block
    assert "return raw" in block


def test_dashboard_ingest_returns_null_for_missing_or_unknown_values():
    """Nothing bad in, nothing bad out. `null`/`undefined`/anything not
    in the allow-list must land as null so the popup honestly says
    'Group size not given' rather than pretending to know."""
    block = _extract_group_size_ingest_block()
    # Explicit null / undefined guard at the top of the function.
    assert "raw == null" in block
    # A final fall-through to null so an unknown string isn't silently
    # coerced to some accidental number.
    tail = block.rsplit("return", 1)[-1]
    assert "null" in tail


# ── Run the shared normalizer against every bucket via Node.js so the
#   test is not just structural. If Node isn't present in the runner
#   (unlikely — @shopify/flash-list ships it) we skip the behavioural
#   check and rely on the structural ones above.
def test_normalizer_actually_maps_every_bucket_to_its_number():
    import shutil
    node = shutil.which("node")
    if not node:
        import pytest
        pytest.skip("node not available — structural tests cover this")

    block = _extract_group_size_ingest_block()
    # Reconstruct the callable exactly as the dashboard would.
    js = (
        "const fn = function (raw) {"
        + block
        + "};"
        + "const cases = [[null, null], [undefined, null], "
        + "['just_me', 1], ['2', 2], ['3', 3], ['4', 4], ['5_plus', 5], "
        + "['garbage', null], [7, 7], [-1, null]];"
        + "for (const [inp, want] of cases) {"
        + "  const got = fn(inp);"
        + "  if (got !== want) { "
        + "    console.error('MISMATCH', JSON.stringify(inp), 'got', got, 'want', want); "
        + "    process.exit(1); "
        + "  }"
        + "}"
        + "console.log('ok');"
    )
    r = subprocess.run([node, "-e", js], capture_output=True, text=True, timeout=10)
    assert r.returncode == 0, (
        f"Group-size normalizer behavioural mismatch: "
        f"stdout={r.stdout!r} stderr={r.stderr!r}"
    )
