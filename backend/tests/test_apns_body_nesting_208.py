"""#208 (v1.0.40 backend, 2026-08-20 — Paul):

Pin the expo-notifications iOS contract for APNs payloads.

Root cause: `EXNotificationSerializer.serializedNotificationData` (see
node_modules/expo-notifications/ios/EXNotifications/Notifications/
EXNotificationSerializer.m lines 80-84) returns
`request.content.userInfo[@"body"]` for remote pushes. Custom routing
keys must therefore live inside a top-level `"body"` object at the
APNs userInfo layer. Putting them as siblings of `aps` — the "standard
APNs" shape — makes `content.data` land in JS as `{}`, which caused:

  - #208: lock-screen tap on a critical alert lands on /quake/unknown
          instead of /alert (Paul's own iPhone, v1.0.40 build 40 probe
          log confirmed rawPayload={} on real earthquake alerts).
  - #174: tremor tap opens a blank screen (same mechanism, preview
          payload).
  - #205: notification carries magnitude but detail screen shows "—"
          (same mechanism — magnitude never reaches content.data, so
          the router never puts it in the query string).

If anyone in future is tempted to "flatten this back" because it
looks like an APNs-standard shape:
  - Yes, top-level custom keys are correct at the APNs layer.
  - No, they are NOT correct for anything routed through
    expo-notifications' JS bridge on iOS.
  - The evidence is EXNotificationSerializer.m and Paul's probe log.
"""
from apns import (
    _build_critical_payload,
    _build_preview_payload,
    _build_recheck_payload,
)


class TestExpoNotificationsIOSBodyNestContract:
    def test_critical_payload_nests_routing_keys_under_body(self):
        p = _build_critical_payload(
            "EARTHQUAKE ALERT",
            "Magnitude 6.4. Are you safe? Tap to check in.",
            "/alert",
            magnitude=6.4, distance_km=12, intensity="VII",
            region="Central Malta", unid="20260820_0000abcd",
        )
        # `aps` stays at the top level — Apple reads it there.
        assert "aps" in p
        # Every routing / event key lives inside `body`.
        assert p["body"]["kind"] == "critical_alert"
        assert p["body"]["action_url"] == "/alert"
        assert p["body"]["magnitude"] == 6.4
        assert p["body"]["distance_km"] == 12
        assert p["body"]["intensity"] == "VII"
        assert p["body"]["region"] == "Central Malta"
        assert p["body"]["unid"] == "20260820_0000abcd"
        # Sibling positions are FORBIDDEN — expo-notifications won't
        # see them on iOS.
        for k in (
            "kind", "action_url", "magnitude", "distance_km",
            "intensity", "region", "unid", "provider",
        ):
            assert k not in p, (
                f"{k!r} at top level: expo-notifications iOS will drop it"
            )

    def test_preview_payload_nests_routing_keys_under_body(self):
        p = _build_preview_payload(
            "Tremor near you",
            "M3.6 210 km away",
            "/quake/abc",
            magnitude=3.6, distance_km=210, depth_km=11,
            unid="abc", region="Sicily",
            latitude=37.5, longitude=15.0,
            provider="EMSC", observed_at="2026-08-20T18:00:00Z",
        )
        assert "aps" in p
        assert p["body"]["kind"] == "emsc_preview"
        assert p["body"]["action_url"] == "/quake/abc"
        assert p["body"]["preview"] is True
        assert p["body"]["magnitude"] == 3.6
        assert p["body"]["distance_km"] == 210
        assert p["body"]["depth_km"] == 11
        assert p["body"]["latitude"] == 37.5
        assert p["body"]["longitude"] == 15.0
        assert p["body"]["region"] == "Sicily"
        assert p["body"]["unid"] == "abc"
        assert p["body"]["provider"] == "EMSC"
        assert p["body"]["observed_at"] == "2026-08-20T18:00:00Z"
        for k in (
            "kind", "action_url", "preview", "magnitude", "distance_km",
            "depth_km", "latitude", "longitude", "region", "unid",
            "provider", "observed_at",
        ):
            assert k not in p, (
                f"{k!r} at top level: expo-notifications iOS will drop it"
            )

    def test_recheck_payload_nests_routing_keys_under_body(self):
        p = _build_recheck_payload(
            "Still ok?",
            "SAME / WORSE / MUCH WORSE",
            check_id="c1",
            device_id="d1",
            ladder_step=2,
            battery_saving=True,
            consecutive_missed=3,
            escalate=True,
        )
        assert "aps" in p
        assert p["body"]["kind"] == "recheck"
        assert p["body"]["action_url"] == "/recheck"
        assert p["body"]["check_id"] == "c1"
        assert p["body"]["device_id"] == "d1"
        assert p["body"]["ladder_step"] == 2
        assert p["body"]["battery_saving"] is True
        assert p["body"]["consecutive_missed"] == 3
        assert p["body"]["escalated_to_critical"] is True
        for k in (
            "kind", "action_url", "check_id", "device_id", "ladder_step",
            "battery_saving", "consecutive_missed", "escalated_to_critical",
        ):
            assert k not in p, (
                f"{k!r} at top level: expo-notifications iOS will drop it"
            )

    def test_aps_stays_at_top_level_all_three_builders(self):
        """Apple reads `aps` from the top of userInfo — moving it under
        `body` would break the visible title/body/sound on the phone."""
        p_crit = _build_critical_payload("t", "b", "/alert")
        p_prev = _build_preview_payload("t", "b", "/quake/x")
        p_rec  = _build_recheck_payload("t", "b", check_id="c", device_id="d")
        for p in (p_crit, p_prev, p_rec):
            assert isinstance(p["aps"], dict)
            assert "alert" in p["aps"]
            # And `aps` is NOT duplicated inside `body`.
            assert "aps" not in p["body"]

    def test_body_is_a_plain_dict_not_a_json_string(self):
        """iOS expo-notifications parses `userInfo["body"]` as a
        dictionary directly, not as a JSON string. (This is the
        opposite of the Android FCM contract, where `body` may arrive
        as a JSON string that gets JSON.parse'd — see
        NotificationSerializer.java lines 65-73.) We build the iOS
        payload for direct APNs delivery, so `body` MUST be a dict."""
        p = _build_critical_payload("t", "b", "/alert", magnitude=6.4)
        assert isinstance(p["body"], dict)
        assert not isinstance(p["body"], str)
