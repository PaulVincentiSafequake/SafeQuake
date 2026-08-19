"""#207 (Batch 7) — Re-check escalation: once per person per incident.

Paul's rule (2026-08-19):
  > Move re-checks to time-sensitive. Escalate to Critical *only* after
  > 3 unanswered asks during an active incident, and only once per
  > person per incident.

Tests here lock the payload builder's contract:

  1. Default (0 missed, escalate=False) → `time-sensitive`, ordinary sound.
  2. First-time escalation (escalate=True) → `critical`, critical sound.
  3. `escalate=False` with a HIGH `consecutive_missed` (e.g. 7 misses on
     a device that has already been escalated once) → stays `time-sensitive`.
     This is the anti-regression test: past bugs re-derived escalation
     from the count and escalated on every sweep after the threshold.
"""
from __future__ import annotations

from apns import _build_recheck_payload


def _kw(**overrides):
    base = dict(
        title="Are you still OK?",
        body="Has anything changed? Tap to answer — it takes one tap.",
        check_id="abc",
        device_id="dev-1",
    )
    base.update(overrides)
    return base


def test_default_recheck_is_time_sensitive():
    p = _build_recheck_payload(**_kw())
    assert p["aps"]["interruption-level"] == "time-sensitive"
    # sound is a plain string filename, NOT the critical dict — the
    # difference is what tells iOS to breach the silent switch.
    assert p["aps"]["sound"] == "recheck.wav"
    assert p["escalated_to_critical"] is False


def test_explicit_escalation_uses_critical_sound_and_level():
    p = _build_recheck_payload(
        **_kw(consecutive_missed=3, escalate=True),
    )
    assert p["aps"]["interruption-level"] == "critical"
    assert isinstance(p["aps"]["sound"], dict)
    assert p["aps"]["sound"].get("critical") == 1
    assert p["escalated_to_critical"] is True


def test_count_alone_does_not_escalate():
    """#207 anti-regression. A previous cut escalated whenever
    consecutive_missed >= 3 — meaning EVERY sweep after the third miss
    fired at Critical, retraining users to silence the whole app.
    Escalation must come from the caller's `escalate` flag, not from
    the count."""
    # Ten missed checks in a row, but `escalate=False` (caller decided
    # this incident was already escalated once and won't do it again).
    p = _build_recheck_payload(**_kw(consecutive_missed=10, escalate=False))
    assert p["aps"]["interruption-level"] == "time-sensitive"
    assert p["escalated_to_critical"] is False
    assert p["consecutive_missed"] == 10  # still exposed for diagnostics


def test_diagnostic_fields_are_always_present():
    """The tremor-diagnostics panel reads these fields to explain why a
    specific check escalated. They must appear whether or not this send
    escalated."""
    for missed, esc in [(0, False), (3, True), (10, False)]:
        p = _build_recheck_payload(**_kw(consecutive_missed=missed, escalate=esc))
        assert "consecutive_missed" in p
        assert "escalated_to_critical" in p
        assert p["consecutive_missed"] == missed
        assert p["escalated_to_critical"] is esc


def test_kind_and_action_url_unchanged_by_escalation():
    """The tap-routing contract is decoupled from the escalation level.
    A Critical re-check must still route to /recheck, not to the alert
    screen — #208 defence-in-depth relies on it."""
    for esc in (False, True):
        p = _build_recheck_payload(**_kw(escalate=esc))
        assert p["kind"] == "recheck"
        assert p["action_url"] == "/recheck"
        assert p["aps"]["category"] == "RECHECK_V1"
