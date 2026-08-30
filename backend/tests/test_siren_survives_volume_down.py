"""BUG-2026-09-volume-down-kills-siren (Paul, verbatim):

  "Testing a Critical Alert siren: I pressed the volume-down button once
   while it was playing. It didn't lower the volume — it killed the sound
   completely, and turning volume back up didn't bring it back. A new
   alert afterward played its siren normally again, so it's not stuck
   off for good. Can you find out why volume-down fully kills the siren
   instead of turning it down, and fix it so it keeps playing, just
   quieter?"

Doctrine encoded here (guarded so a future refactor cannot quietly
regress it back to the state Paul reported):

  1. `alert.tsx` contains a SIREN RESURRECTION useEffect that watches
     `sirenStatus.playing` and re-plays the siren when the player
     transitions to paused while `shouldPlayRef.current` is still
     true — i.e. the user has NOT tapped I'm Safe / triage / Dismiss
     and no stand-down has arrived.

  2. The resurrection uses a `wasPlayingRef` latch so it does NOT fire
     before the initial play() call lands — otherwise every fresh
     mount would race with itself.

  3. The resurrection is guarded by `shouldPlayRef.current` BOTH at
     effect body entry AND inside the setTimeout callback, so a user
     who silences the siren during the debounce window cannot have
     it resurrected against their will. This is the #31/#50 failure
     shape and must never regress.

  4. The resurrection debounces with setTimeout so the KILL-SWITCH,
     status="sent" safety net, unmount cleanup, and stand-down
     branch all run first. Their effect on `shouldPlayRef.current`
     wins the race.

  5. The resurrection restores loop=true and volume=1.0 on resume.
     Hardware volume-down is the OS's job (controls the media stream
     volume). The player's own settings must not be corrupted.

Manual repro (physical iOS/Android device, cannot be reproduced in
Expo Go's web preview):
  1. On Home, tap Trigger Test Alert.
  2. While the siren is playing, press volume-down once.
  3. BEFORE FIX: sound gone, volume-up does not restore it.
     AFTER FIX: volume lowers; volume-up brings the sound back.
"""
from pathlib import Path
import re

ALERT_PATH = Path(__file__).resolve().parents[2] / "frontend" / "app" / "alert.tsx"
ALERT = ALERT_PATH.read_text()


# ── 1. Resurrection effect exists and is discoverable ─────────────────
class TestResurrectionEffectPresent:
    def test_marker_comment_is_present(self):
        """A future dev searching for the bug should find the effect."""
        assert "SIREN RESURRECTION" in ALERT
        assert "BUG-2026-09-volume-down-kills-siren" in ALERT

    def test_effect_uses_was_playing_latch(self):
        assert "wasPlayingRef" in ALERT
        # Latch is set true the first time playing goes true.
        assert re.search(
            r"if\s*\(\s*sirenStatus\.playing\s*\)\s*\{\s*\n\s*wasPlayingRef\.current\s*=\s*true",
            ALERT,
        ), "wasPlayingRef must latch true when the player reports playing"

    def test_effect_watches_playing_and_calls_play_again(self):
        """The resurrection block must re-call sirenPlayer.play() when the
        player is paused externally."""
        block = ALERT.split("SIREN RESURRECTION")[1].split(
            "FINAL SAFETY NET"
        )[0]
        assert "sirenPlayer.play()" in block
        assert "sirenPlayer.loop = true" in block
        assert "sirenPlayer.volume = 1.0" in block

    def test_effect_depends_on_playing_flag(self):
        """React only re-runs the effect on playing transitions if it's
        in the dependency array. This is what actually detects the
        volume-down-triggered pause."""
        block = ALERT.split("SIREN RESURRECTION")[1].split(
            "FINAL SAFETY NET"
        )[0]
        # Dependency array of that useEffect must contain sirenStatus.playing.
        # Find the last `}, [ ... ]);` in the resurrection block.
        deps = re.findall(r"\},\s*\[(.*?)\]\s*\);", block, flags=re.DOTALL)
        assert deps, "resurrection effect must close with a dependency array"
        # Last dep-array in the block is the one for the resurrection effect.
        assert "sirenStatus.playing" in deps[-1]
        assert "sirenStatus.isLoaded" in deps[-1]


# ── 2. Guards that prevent regressing #31/#50 ─────────────────────────
class TestResurrectionCannotResurrectSilencedSiren:
    """A siren that a user deliberately silenced (I'm Safe / triage /
    Dismiss / stand-down) must stay silent. This is Paul's absolute
    rule (#31/#50). Volume-down is a different case — an OS event, not
    a user decision — and we only resume in that case."""

    def test_effect_body_gates_on_should_play_ref(self):
        block = ALERT.split("SIREN RESURRECTION")[1].split(
            "FINAL SAFETY NET"
        )[0]
        # There must be an early-return `if (!shouldPlayRef.current) return;`
        # at effect-body entry, before the setTimeout.
        pre_timeout = block.split("setTimeout", 1)[0]
        assert "if (!shouldPlayRef.current) return;" in pre_timeout

    def test_timeout_callback_re_checks_should_play_ref(self):
        """The KILL-SWITCH, status='sent' safety net, unmount cleanup and
        stand-down branch all flip shouldPlayRef.current to false. If the
        resurrection callback did not re-check the ref inside the timeout,
        a silenced siren would come back after the debounce window."""
        block = ALERT.split("SIREN RESURRECTION")[1].split(
            "FINAL SAFETY NET"
        )[0]
        post_timeout = block.split("setTimeout", 1)[1]
        # The re-check must appear BEFORE the sirenPlayer.play() call.
        recheck_idx = post_timeout.find("if (!shouldPlayRef.current) return;")
        play_idx = post_timeout.find("sirenPlayer.play()")
        assert recheck_idx != -1, "setTimeout callback must re-check shouldPlayRef.current"
        assert play_idx != -1
        assert recheck_idx < play_idx, (
            "shouldPlayRef re-check must run BEFORE sirenPlayer.play() so a "
            "user who silenced the siren during the debounce cannot have it "
            "brought back"
        )

    def test_setTimeout_returns_cleanup(self):
        """React cleans up on effect re-run; the timeout must be cleared
        so a rapid playing→paused→playing sequence doesn't leak calls."""
        block = ALERT.split("SIREN RESURRECTION")[1].split(
            "FINAL SAFETY NET"
        )[0]
        assert "clearTimeout(t)" in block


# ── 3. Kill-switch, unmount, sent-status, stand-down still win ────────
class TestExistingSilencePathsStillFlipTheRef:
    """Each of the four paths that a user can use to silence the siren
    must still flip shouldPlayRef.current = false. Locking these here
    keeps the guarantee that no resurrection can ever fight a user's
    decision."""

    def test_unmount_cleanup_flips_ref(self):
        # useEffect cleanup: shouldPlayRef.current = false; sirenPlayer.pause();
        assert re.search(
            r"return\s*\(\)\s*=>\s*\{\s*\n\s*shouldPlayRef\.current\s*=\s*false",
            ALERT,
        ), "unmount cleanup must flip shouldPlayRef.current to false"

    def test_sent_status_flips_ref(self):
        block = ALERT.split("FINAL SAFETY NET")[1].split("}, [status, sirenPlayer]);")[0]
        assert "shouldPlayRef.current = false" in block

    def test_stand_down_flips_ref(self):
        # Stand-down branch inside the alert-bus subscriber.
        block = ALERT.split("event.stood_down")[1].split("setStoodDown")[0]
        assert "shouldPlayRef.current = false" in block

    def test_stop_siren_helper_flips_ref(self):
        # stopSiren() is called by every triage/safe/dismiss/trapped path.
        block = ALERT.split("const stopSiren = ()", 1)[1].split("\n  };\n", 1)[0]
        assert "shouldPlayRef.current = false" in block


# ── 4. Volume-down semantics are not spoofed inside the player ────────
class TestVolumeDownStaysAnOsConcern:
    """Paul: 'keep it playing, just quieter'. That means we do NOT try to
    intercept hardware volume keys or fiddle with sirenPlayer.volume in
    response to them — the OS controls the media stream volume, and our
    player just keeps playing so turning volume back up restores the
    sound."""

    def test_no_hardware_volume_key_listeners(self):
        """No RemoteCommandCenter / hardware volume listeners in alert.tsx."""
        forbidden = [
            "hardwareVolumeButton",
            "VolumeManager",
            "MPRemoteCommandCenter",
            "addVolumeListener",
        ]
        for f in forbidden:
            assert f not in ALERT, (
                f"alert.tsx must not intercept hardware volume keys ({f}); "
                "the OS owns media stream volume"
            )

    def test_resurrection_restores_volume_to_one(self):
        """After an external pause, resume MUST restore player.volume = 1.0.
        If a prior effect had set it to 0 (belt-and-braces on silence),
        resurrection would otherwise resume silently."""
        block = ALERT.split("SIREN RESURRECTION")[1].split(
            "FINAL SAFETY NET"
        )[0]
        assert "sirenPlayer.volume = 1.0" in block


# ── 5. Audio session config is still correct (#13 regression guard) ───
class TestAudioSessionStillSilentModeOverride:
    def test_playsInSilentMode_true(self):
        """The siren must ignore the ringer switch, and the resurrection
        must not silently disable that override."""
        assert "playsInSilentMode: true" in ALERT
