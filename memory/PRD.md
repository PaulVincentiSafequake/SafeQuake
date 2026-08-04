# QuakeGuard — Earthquake Safety App

## Problem
A React Native / Expo mobile app that helps users practice earthquake preparedness and report themselves safe after a shake event.

## Screens
1. **Home (`/`)** — Hero banner "QUAKEGUARD", system-active status pill, 4 safety-protocol cards (Drop, Cover, Hold On, After), info blurb, sticky "Trigger Test Alert" CTA.
2. **Alert (`/alert`)** — Full-screen red gradient emergency state, pulsing warning icon (react-native-reanimated), LIVE ALERT timer, magnitude / distance / intensity metrics, primary green "I'm Safe" CTA, secondary "Dismiss alert" link.

## Key Behaviour
- Tapping **Trigger Test Alert** (`trigger-alert-btn`) heavy-haptic → navigates to `/alert`.
- Tapping **I'm Safe** (`im-safe-btn`):
  - Requests foreground location permission → captures lat/lng/accuracy (8s timeout).
  - Reads battery level + state via `expo-battery`.
  - Generates a stable `deviceId` on first use, cached in AsyncStorage.
  - POSTs JSON to `https://safequake.onrender.com/api/status`:
    ```json
    {
      "deviceId": "qg-...",
      "status": "safe",
      "client_name": "quakeguard-mobile",
      "timestamp": "ISO-8601",
      "location": { "latitude": .., "longitude": .., "accuracy": .., "error": null },
      "battery": { "level": 0..1, "state": "charging|full|unplugged|unknown" }
    }
    ```
  - On HTTP 200 → success toast "Report received. Stay safe." → auto-navigates home after 1.2s.
  - On error → warning toast with retry hint; button remains actionable.
- **Dismiss alert** returns home without POSTing.

## Design
- Personality 7 (Dark-First Tactical) — obsidian bg `#0F1115`, signal red `#FF3B30` accents, success green `#34C759` for the safe CTA.
- Deprecated `expo-av`/`Alert` avoided; toasts and pressables only.

## Stack
- Expo Router (file-based) · expo-location · expo-battery · expo-haptics · expo-linear-gradient · expo-image · react-native-reanimated · AsyncStorage · @expo/vector-icons (Ionicons).

## Permissions Declared
- iOS: `NSLocationWhenInUseUsageDescription` = "Share your location to report you are safe"
- Android: `ACCESS_FINE_LOCATION`, `ACCESS_COARSE_LOCATION`

## Business Enhancement Idea
Add an "Emergency Contacts" screen so tapping **I'm Safe** also fires an SMS/push to designated family members — pushes retention and daily-utility habit-forming beyond emergency-only use.

## Diagnostic: /api/cors-debug
- Public read-only endpoint that returns the deployed CORS allowlist, regex, and a "deploy fingerprint" (sha256-prefix + mtime + process start time of `server.py`).
- Echoes the caller's `Origin` header and reports `allowed` + `allow_reason` (`exact_match` | `regex_match` | `not_allowlisted` | `no_origin_header`).
- Purpose: tell code-vs-deploy drift apart instantly. If the dashboard is empty and this endpoint says `allowed=false` for the dashboard's origin, the fix is a redeploy — not a code change.
