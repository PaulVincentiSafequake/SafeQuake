"""#207 — automated re-checks are NEVER Critical Alerts.

Paul, 2026-08-24 (live test batch):
  > Remove interruption-level: critical from automated re-check pushes,
  > keep it only for the first real alert.

Earlier cuts of this feature escalated to `critical` after three unanswered
asks (once per person per incident). That is now gone entirely: `critical`
belongs to the earthquake alert and to nothing else. A repeating
full-volume push that ignores the silent switch teaches people to mute the
app, and it is the fastest way to lose the Critical Alert entitlement that
the real siren depends on.

Tests here lock the payload builder's contract:

  1. Default → `time-sensitive`, ordinary sound.
  2. `escalate=True` → STILL `time-sensitive`, STILL an ordinary sound.
     The flag only marks the send for the diagnostics panel.
  3. A high `consecutive_missed` never changes the level either.
  4. The earthquake alert keeps `critical` — that is the one exception.
"""
from __future__ import annotations

from apns import _build_critical_payload, _build_recheck_payload


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
    assert p["body"]["escalated"] is False
    assert p["body"]["escalated_to_critical"] is False


def test_escalate_flag_never_produces_a_critical_alert():
    """#207. The caller may still say "this one is an escalation" — the
    diagnostics panel and the audit trail want to know — but the push that
    goes out is indistinguishable in loudness from any other re-check."""
    p = _build_recheck_payload(**_kw(consecutive_missed=3, escalate=True))
    assert p["aps"]["interruption-level"] == "time-sensitive"
    assert p["aps"]["sound"] == "recheck.wav"
    assert not isinstance(p["aps"]["sound"], dict)
    assert p["body"]["escalated"] is True
    assert p["body"]["escalated_to_critical"] is False


def test_high_missed_count_never_escalates_the_level():
    for missed in (0, 3, 10, 99):
        for esc in (False, True):
            p = _build_recheck_payload(**_kw(consecutive_missed=missed, escalate=esc))
            assert p["aps"]["interruption-level"] == "time-sensitive"
            assert p["body"]["consecutive_missed"] == missed


def test_no_recheck_payload_ever_carries_critical_sound_flag():
    """Belt and braces: whatever combination of arguments, nothing in the
    re-check path may emit `aps.sound.critical`."""
    for kwargs in ({}, {"escalate": True}, {"escalate": True, "consecutive_missed": 7},
                   {"battery_saving": True, "escalate": True}, {"ladder_step": 4}):
        p = _build_recheck_payload(**_kw(**kwargs))
        sound = p["aps"]["sound"]
        assert not isinstance(sound, dict), sound
        assert p["aps"]["interruption-level"] != "critical"


def test_diagnostic_fields_are_always_present():
    """The tremor-diagnostics panel reads these fields to explain a
    specific check. They must appear on every send.

    v1.0.40 fix (#208 root cause): custom keys live in the `body` nested
    dict so expo-notifications iOS surfaces them in content.data."""
    for missed, esc in [(0, False), (3, True), (10, False)]:
        p = _build_recheck_payload(**_kw(consecutive_missed=missed, escalate=esc))
        assert p["body"]["consecutive_missed"] == missed
        assert p["body"]["escalated"] is esc
        assert p["body"]["escalated_to_critical"] is False


def test_kind_and_action_url_unchanged():
    """The tap-routing contract: a re-check must route to /recheck, never
    to the alert screen — #208 defence-in-depth relies on it."""
    for esc in (False, True):
        p = _build_recheck_payload(**_kw(escalate=esc))
        assert p["body"]["kind"] == "recheck"
        assert p["body"]["action_url"] == "/recheck"
        assert p["aps"]["category"] == "RECHECK_V1"


def test_the_earthquake_alert_keeps_critical():
    """#207 removed `critical` from re-checks ONLY. The alert itself is the
    one push that is allowed to ignore the silent switch."""
    p = _build_critical_payload("Earthquake", "Drop, cover, hold on.", "/alert")
    assert p["aps"]["interruption-level"] == "critical"
    assert p["aps"]["sound"]["critical"] == 1
