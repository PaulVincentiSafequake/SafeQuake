"""Direct APNs (Apple Push Notification service) delivery for iOS Critical
Alerts. Bypasses the SuprSend relay so we can send true critical-alert
payloads (aps.sound.critical=1, interruption-level=critical) that override
silent switch / DND / Focus modes.

This module is used ONLY by /api/trigger-alert for iOS recipients. All other
push paths (registration, Android, local reminders) are untouched.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Optional

import httpx
import jwt
from motor.motor_asyncio import AsyncIOMotorDatabase

# ---------- Constants ----------
APNS_PROD_HOST = "https://api.push.apple.com"
APNS_SANDBOX_HOST = "https://api.sandbox.push.apple.com"
JWT_ALGORITHM = "ES256"
JWT_TTL_SECONDS = 50 * 60  # Apple caps at 60 min; refresh at 50
APNS_TIMEOUT_SECONDS = 8.0
CONCURRENCY = 30
SECRETS_COLLECTION = "secrets"
SECRETS_DOC_ID = "apns_config"


# ---------- Data models ----------
@dataclass
class ApnsConfig:
    key_id: str
    team_id: str
    bundle_id: str
    private_key_pem: str  # -----BEGIN PRIVATE KEY-----\n...

    def fingerprint(self) -> str:
        pk = self.private_key_pem.strip()
        head = pk[:40].replace("\n", " ")
        return f"{self.key_id} ({self.team_id}) · {head}…"


@dataclass
class ApnsResult:
    user_id: str
    token_fingerprint: str
    environment: str  # "production" | "sandbox" | "n/a"
    status_code: Optional[int]
    apns_id: Optional[str]
    apns_unique_id: Optional[str]
    reason: Optional[str]  # BadDeviceToken, Unregistered, TopicDisallowed, ...
    delivered: bool
    duration_ms: int
    error: Optional[str] = None

    def as_dict(self) -> dict:
        return {
            "user_id": self.user_id,
            "token_fingerprint": self.token_fingerprint,
            "environment": self.environment,
            "status_code": self.status_code,
            "apns_id": self.apns_id,
            "apns_unique_id": self.apns_unique_id,
            "reason": self.reason,
            "delivered": self.delivered,
            "duration_ms": self.duration_ms,
            "error": self.error,
        }


# ---------- Config persistence (MongoDB-backed) ----------
async def save_apns_config(
    db: AsyncIOMotorDatabase,
    key_id: str,
    team_id: str,
    bundle_id: str,
    private_key_pem: str,
) -> None:
    """Persist the APNs auth key + metadata into MongoDB. The PEM is stored
    base64-encoded so it survives any JSON/env round-trips cleanly."""
    if not private_key_pem or "PRIVATE KEY" not in private_key_pem:
        raise ValueError("private_key_pem does not look like a valid PEM")
    encoded = base64.b64encode(private_key_pem.encode("utf-8")).decode("ascii")
    from datetime import datetime, timezone
    await db[SECRETS_COLLECTION].update_one(
        {"_id": SECRETS_DOC_ID},
        {"$set": {
            "_id": SECRETS_DOC_ID,
            "key_id": key_id.strip(),
            "team_id": team_id.strip(),
            "bundle_id": bundle_id.strip(),
            "private_key_pem_b64": encoded,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }},
        upsert=True,
    )
    # Reset the in-process JWT cache so the next send uses the fresh key.
    _JWT_CACHE.clear()


async def load_apns_config(db: AsyncIOMotorDatabase) -> Optional[ApnsConfig]:
    doc = await db[SECRETS_COLLECTION].find_one({"_id": SECRETS_DOC_ID})
    if not doc:
        return None
    try:
        pem = base64.b64decode(doc["private_key_pem_b64"]).decode("utf-8")
    except Exception as e:  # noqa: BLE001
        logging.warning(f"Failed to decode APNs key from DB: {e}")
        return None
    return ApnsConfig(
        key_id=doc.get("key_id", ""),
        team_id=doc.get("team_id", ""),
        bundle_id=doc.get("bundle_id", ""),
        private_key_pem=pem,
    )


async def apns_config_status(db: AsyncIOMotorDatabase) -> dict:
    doc = await db[SECRETS_COLLECTION].find_one({"_id": SECRETS_DOC_ID})
    if not doc:
        return {"configured": False}
    return {
        "configured": True,
        "key_id": doc.get("key_id"),
        "team_id": doc.get("team_id"),
        "bundle_id": doc.get("bundle_id"),
        "updated_at": doc.get("updated_at"),
    }


# ---------- JWT signer with in-process cache ----------
_JWT_CACHE: dict = {}


def _build_jwt(cfg: ApnsConfig) -> str:
    now = int(time.time())
    cached = _JWT_CACHE.get(cfg.key_id)
    if cached and cached["exp"] > now + 60:
        return cached["token"]
    token = jwt.encode(
        payload={"iss": cfg.team_id, "iat": now},
        key=cfg.private_key_pem,
        algorithm=JWT_ALGORITHM,
        headers={"kid": cfg.key_id, "alg": JWT_ALGORITHM, "typ": "JWT"},
    )
    _JWT_CACHE[cfg.key_id] = {"token": token, "exp": now + JWT_TTL_SECONDS}
    return token


# ---------- HTTP/2 client (lazy singletons) ----------
_prod_client: Optional[httpx.AsyncClient] = None
_sandbox_client: Optional[httpx.AsyncClient] = None


def _get_client(environment: str) -> httpx.AsyncClient:
    global _prod_client, _sandbox_client
    if environment == "sandbox":
        if _sandbox_client is None:
            _sandbox_client = httpx.AsyncClient(
                base_url=APNS_SANDBOX_HOST,
                http2=True,
                timeout=APNS_TIMEOUT_SECONDS,
            )
        return _sandbox_client
    if _prod_client is None:
        _prod_client = httpx.AsyncClient(
            base_url=APNS_PROD_HOST,
            http2=True,
            timeout=APNS_TIMEOUT_SECONDS,
        )
    return _prod_client


async def aclose() -> None:
    global _prod_client, _sandbox_client
    if _prod_client is not None:
        await _prod_client.aclose()
        _prod_client = None
    if _sandbox_client is not None:
        await _sandbox_client.aclose()
        _sandbox_client = None


# ---------- Utilities ----------
def _fingerprint(token: str) -> str:
    if not token:
        return ""
    if len(token) <= 16:
        return token
    return f"{token[:8]}…{token[-8:]}"


# Notification-actions category id for INFORMATIONAL tremor notices only
# (batch 5, B9). Must match the category registered by the mobile app in
# app/_layout.tsx. The critical-alert payload never carries a category.
TREMOR_CATEGORY_ID = "TREMOR_INFO"


def _build_critical_payload(
    title: str,
    body: str,
    action_url: str,
    magnitude: Optional[float] = None,
    distance_km: Optional[float] = None,
    intensity: Optional[str] = None,
    depth_km: Optional[float] = None,
    region: Optional[str] = None,
    unid: Optional[str] = None,
    provider: Optional[str] = None,
) -> dict:
    """APNs payload for a true iOS Critical Alert.

    Payload MUST include `kind: "critical_alert"` so the mobile-app tap
    handler can distinguish real alerts from previews and route
    appropriately. See _build_preview_payload for the corresponding
    preview-mode payload. The fail-safe on the mobile side treats a
    missing `kind` as INFORMATIONAL (not critical), so real critical
    alerts MUST always carry this field.

    Event-specific fields (magnitude, distance_km, intensity, depth_km,
    region, unid, provider) are embedded at the top level so the /alert
    screen can render the actual event data rather than showing stale
    or hardcoded defaults. Fields left as None simply don't appear in
    the payload — the mobile app renders "—" when a field is absent.

    IMPORTANT: `sound.name` must reference a file actually bundled inside the
    iOS app's `Library/Sounds/` directory — one of `.caf`, `.aiff`, or `.wav`,
    ≤ 30 seconds. Using `"default"` inside a `critical: 1` dict is
    inconsistently honoured by iOS: the push is accepted (APNs returns 200)
    but is often silently downgraded to a regular alert (no screen wake, no
    override of silent/DND/Focus). We bundle `siren.caf` via the
    expo-notifications plugin `sounds` array in app.json — Expo copies it
    into `Library/Sounds/` at build time, so `name: "siren.caf"` resolves
    on device and iOS honours the critical-alert semantics.
    """
    payload: dict = {
        "aps": {
            "alert": {"title": title, "body": body},
            "sound": {"critical": 1, "name": "siren.caf", "volume": 1.0},
            "interruption-level": "critical",
            "relevance-score": 1,
        },
        # kind is REQUIRED — the mobile tap handler routes by this field.
        # Missing kind → informational fallback (never siren).
        "kind": "critical_alert",
        "action_url": action_url,
    }
    # Event-specific fields — only include when non-None so the mobile
    # renderer can distinguish "unknown" (missing key) from "known-and-zero".
    if magnitude is not None:   payload["magnitude"] = magnitude
    if distance_km is not None: payload["distance_km"] = distance_km
    if intensity is not None:   payload["intensity"] = intensity
    if depth_km is not None:    payload["depth_km"] = depth_km
    if region is not None:      payload["region"] = region
    if unid is not None:        payload["unid"] = unid
    if provider is not None:    payload["provider"] = provider
    return payload


def _build_preview_payload(
    title: str,
    body: str,
    action_url: str,
    magnitude: Optional[float] = None,
    distance_km: Optional[float] = None,
    depth_km: Optional[float] = None,
    latitude: Optional[float] = None,
    longitude: Optional[float] = None,
    region: Optional[str] = None,
    unid: Optional[str] = None,
    provider: Optional[str] = None,
    observed_at: Optional[str] = None,   # ISO-8601 string
) -> dict:
    """APNs payload for a REGULAR alert — deliberately NOT critical.

    Includes `kind: "emsc_preview"` so the mobile tap handler routes
    to the informational detail screen, NEVER to /alert with the siren.
    This bug (BUG-2026-08-06-preview-tap-siren) is exactly what the
    non-critical send path was supposed to prevent — but the send-side
    constraint doesn't cover the tap-handler journey. Fixed here at the
    payload level so mobile has an unambiguous signal.

    Used exclusively by EMSC preview mode. See emsc/preview.py for the
    non-negotiable constraints on this path.

    Delivery semantics:
      - `interruption-level: "active"` — normal banner + sound, respects
        the user's silent/DND/Focus preferences (unlike critical alerts).
      - `sound: "default"` — the operator's ringtone / default alert
        sound. NOT the critical siren.
      - `apns-priority: 5` (set on the header) — power-efficient delivery.

    Event details are embedded at the top level so the /quake/[unid]
    detail screen can render specifics without a second network round-trip.
    """
    payload: dict = {
        "aps": {
            "alert": {"title": title, "body": body},
            "sound": "default",
            "interruption-level": "active",
            # Notification-actions category (batch 5, B9). Carries the
            # "See location on map" / "Close" buttons registered on the
            # mobile side as TREMOR_INFO. INFORMATIONAL NOTICES ONLY —
            # _build_critical_payload deliberately sets no category at
            # all, so nothing can ever compete for attention with
            # I'M SAFE / I'M TRAPPED during a real event. The two paths
            # cannot share configuration because they don't share an id.
            "category": TREMOR_CATEGORY_ID,
        },
        # kind is REQUIRED — mobile tap handler distinguishes preview
        # from real alert. Missing kind → informational fallback.
        "kind": "emsc_preview",
        "action_url": action_url,
        "preview": True,
    }
    if magnitude is not None:   payload["magnitude"] = magnitude
    if distance_km is not None: payload["distance_km"] = distance_km
    if depth_km is not None:    payload["depth_km"] = depth_km
    if latitude is not None:    payload["latitude"] = latitude
    if longitude is not None:   payload["longitude"] = longitude
    if region is not None:      payload["region"] = region
    if unid is not None:        payload["unid"] = unid
    if provider is not None:    payload["provider"] = provider
    if observed_at is not None: payload["observed_at"] = observed_at
    return payload


# ---------- Single-token send with sandbox fallback ----------
async def _send_one(
    cfg: ApnsConfig,
    user_id: str,
    device_token: str,
    payload: dict,
    idempotency_key: str,
    environment: str = "production",
    apns_priority: str = "10",   # "10" = immediate delivery (critical alerts); "5" = power-efficient (regular pushes)
    push_type: str = "alert",    # "alert" | "background" (silent, data-only)
    apns_expiration: str = "0",  # "0" = one delivery attempt only
) -> ApnsResult:
    """Send one push and return the diagnostic result. On 400 BadDeviceToken
    against production, transparently retries once against sandbox and
    reports which environment worked (invaluable for token-type mismatch
    diagnosis).

    `apns_priority` defaults to "10" for backward compatibility with the
    critical-alert path. Preview-mode pushes pass "5" so Apple can batch
    or slightly delay delivery to conserve device battery — the correct
    priority for non-urgent diagnostic notifications, and required by
    Apple's guidelines for non-critical pushes."""
    started = time.monotonic()
    fp = _fingerprint(device_token)
    apns_id = str(uuid.uuid4())
    try:
        client = _get_client(environment)
        headers = {
            "authorization": f"bearer {_build_jwt(cfg)}",
            "apns-topic": cfg.bundle_id,
            "apns-push-type": push_type,
            "apns-priority": apns_priority,
            "apns-expiration": apns_expiration,
            "apns-id": apns_id,
            "apns-collapse-id": idempotency_key[:64],
        }
        resp = await client.post(
            f"/3/device/{device_token}",
            headers=headers,
            content=json.dumps(payload, separators=(",", ":")),
        )
        duration_ms = int((time.monotonic() - started) * 1000)
        reason: Optional[str] = None
        if resp.status_code != 200 and resp.content:
            try:
                reason = resp.json().get("reason")
            except Exception:  # noqa: BLE001
                reason = None

        # BadDeviceToken can mean "this is a sandbox token" when we hit prod.
        if (
            resp.status_code == 400
            and reason == "BadDeviceToken"
            and environment == "production"
        ):
            fallback = await _send_one(
                cfg, user_id, device_token, payload, idempotency_key,
                environment="sandbox",
                apns_priority=apns_priority,
                push_type=push_type,
                apns_expiration=apns_expiration,
            )
            return fallback

        return ApnsResult(
            user_id=user_id,
            token_fingerprint=fp,
            environment=environment,
            status_code=resp.status_code,
            apns_id=resp.headers.get("apns-id") or apns_id,
            apns_unique_id=resp.headers.get("apns-unique-id"),
            reason=reason,
            delivered=(resp.status_code == 200),
            duration_ms=duration_ms,
        )
    except Exception as e:  # noqa: BLE001
        duration_ms = int((time.monotonic() - started) * 1000)
        logging.warning(f"APNs send failed for {user_id}: {e}")
        return ApnsResult(
            user_id=user_id,
            token_fingerprint=fp,
            environment=environment,
            status_code=None,
            apns_id=apns_id,
            apns_unique_id=None,
            reason=None,
            delivered=False,
            duration_ms=duration_ms,
            error=str(e),
        )


# ---------- Public API ----------
async def send_critical_alerts(
    db: AsyncIOMotorDatabase,
    devices: list[dict],  # each: {user_id, device_token}
    title: str,
    body: str,
    action_url: str,
    idempotency_key: str,
    # Event-specific fields — forwarded to the payload so the /alert
    # screen renders REAL values instead of hardcoded defaults. Any
    # field left as None simply doesn't appear in the payload; the
    # mobile app renders "—" for missing fields (never a stale value).
    magnitude: Optional[float] = None,
    distance_km: Optional[float] = None,
    intensity: Optional[str] = None,
    depth_km: Optional[float] = None,
    region: Optional[str] = None,
    unid: Optional[str] = None,
    provider: Optional[str] = None,
) -> dict:
    """Send a critical-alert push to every iOS device in `devices`.

    Returns a dict with:
      - `payload`: the exact JSON body that was POSTed to Apple's APNs (same
        for every recipient in this batch — captured so it's stored in
        push_events for later verification of e.g. sound.name).
      - `events`: per-recipient serializable event dicts.
    """
    if not devices:
        return {"payload": None, "events": []}

    cfg = await load_apns_config(db)
    if cfg is None:
        # Not configured yet — record a single stub event so the UI shows
        # why nothing was sent.
        return {
            "payload": None,
            "events": [
                {
                    "user_id": d.get("user_id") or "",
                    "token_fingerprint": _fingerprint(d.get("device_token") or ""),
                    "environment": "n/a",
                    "status_code": None,
                    "apns_id": None,
                    "apns_unique_id": None,
                    "reason": "APNS_NOT_CONFIGURED",
                    "delivered": False,
                    "duration_ms": 0,
                    "error": "APNs auth key not uploaded. Visit /api/admin/apns-key?token=<pwd>",
                }
                for d in devices
            ],
        }

    payload = _build_critical_payload(
        title, body, action_url,
        magnitude=magnitude,
        distance_km=distance_km,
        intensity=intensity,
        depth_km=depth_km,
        region=region,
        unid=unid,
        provider=provider,
    )
    sem = asyncio.Semaphore(CONCURRENCY)

    async def _guarded(d: dict) -> ApnsResult:
        async with sem:
            return await _send_one(
                cfg,
                user_id=d.get("user_id") or "",
                device_token=d.get("device_token") or "",
                payload=payload,
                idempotency_key=idempotency_key,
            )

    results = await asyncio.gather(*(_guarded(d) for d in devices))
    return {
        "payload": payload,
        "events": [r.as_dict() for r in results],
    }


# ---------- Preview mode (non-critical) send ----------
async def send_preview_alerts(
    db: AsyncIOMotorDatabase,
    devices: list[dict],  # each: {user_id, device_token}
    title: str,
    body: str,
    action_url: str,
    idempotency_key: str,
    # Event-specific fields forwarded to payload so /quake/[unid] detail
    # screen can render the actual event data.
    magnitude: Optional[float] = None,
    distance_km: Optional[float] = None,
    depth_km: Optional[float] = None,
    latitude: Optional[float] = None,
    longitude: Optional[float] = None,
    region: Optional[str] = None,
    unid: Optional[str] = None,
    provider: Optional[str] = None,
    observed_at: Optional[str] = None,
) -> dict:
    """Send a REGULAR (non-critical) alert to every iOS device in `devices`.

    Used exclusively by EMSC preview mode. See _build_preview_payload for
    the design rationale on why this MUST remain non-critical — using the
    critical-alerts entitlement for anything non-critical risks Apple
    revoking it, which would be product-ending.

    Signature intentionally mirrors send_critical_alerts so the two are
    easily swappable in test code, but the network priority and payload
    shape differ: apns-priority=5, sound=default, interruption=active.
    """
    if not devices:
        return {"payload": None, "events": []}

    cfg = await load_apns_config(db)
    if cfg is None:
        return {
            "payload": None,
            "events": [
                {
                    "user_id": d.get("user_id") or "",
                    "token_fingerprint": _fingerprint(d.get("device_token") or ""),
                    "environment": "n/a",
                    "status_code": None,
                    "apns_id": None,
                    "apns_unique_id": None,
                    "reason": "APNS_NOT_CONFIGURED",
                    "delivered": False,
                    "duration_ms": 0,
                    "error": "APNs auth key not uploaded.",
                }
                for d in devices
            ],
        }

    payload = _build_preview_payload(
        title, body, action_url,
        magnitude=magnitude,
        distance_km=distance_km,
        depth_km=depth_km,
        latitude=latitude,
        longitude=longitude,
        region=region,
        unid=unid,
        provider=provider,
        observed_at=observed_at,
    )
    sem = asyncio.Semaphore(CONCURRENCY)

    async def _guarded(d: dict) -> ApnsResult:
        async with sem:
            return await _send_one(
                cfg,
                user_id=d.get("user_id") or "",
                device_token=d.get("device_token") or "",
                payload=payload,
                idempotency_key=idempotency_key,
                apns_priority="5",   # regular / power-efficient delivery
            )

    results = await asyncio.gather(*(_guarded(d) for d in devices))
    return {
        "payload": payload,
        "events": [r.as_dict() for r in results],
    }


# ---------- Silent (data-only) send: reminder kill switch ----------
async def send_silent_cancel_reminders(
    db: AsyncIOMotorDatabase,
    devices: list[dict],  # each: {user_id, device_token}
    idempotency_key: str,
    reason: Optional[str] = None,
) -> dict:
    """Send a SILENT background push telling the app to cancel every pending
    check-in reminder (batch 5, B1 — the operator's false-alarm kill switch).

    Why silent matters: the whole point is to stop unwanted noise. Using an
    alert push to say "stop making noise" would add a ninth notification to
    the eight we're cancelling. So:
      - payload carries `content-available: 1` and NO alert / sound / badge;
      - `apns-push-type: background` and `apns-priority: 5`, which Apple
        requires for silent pushes (an alert-type push with no alert body is
        rejected, and a priority-10 background push is rejected too);
      - `apns-expiration` is set ~10 minutes out so a phone that is briefly
        offline still gets the cancel when it reconnects — reminders run for
        11½ minutes, so a late delivery is still useful.

    The app handles it in a registered background task (see
    CANCEL_REMINDERS_TASK in app/_layout.tsx) so it works with the app
    backgrounded or killed, not just in the foreground.
    """
    if not devices:
        return {"payload": None, "events": []}

    cfg = await load_apns_config(db)
    if cfg is None:
        return {
            "payload": None,
            "events": [
                {
                    "user_id": d.get("user_id") or "",
                    "token_fingerprint": _fingerprint(d.get("device_token") or ""),
                    "environment": "n/a",
                    "status_code": None,
                    "apns_id": None,
                    "apns_unique_id": None,
                    "reason": "APNS_NOT_CONFIGURED",
                    "delivered": False,
                    "duration_ms": 0,
                    "error": "APNs auth key not uploaded.",
                }
                for d in devices
            ],
        }

    payload: dict = {
        "aps": {"content-available": 1},
        "kind": "cancel_reminders",
    }
    if reason:
        payload["reason"] = reason

    expiration = str(int(time.time()) + 600)
    sem = asyncio.Semaphore(CONCURRENCY)

    async def _guarded(d: dict) -> ApnsResult:
        async with sem:
            return await _send_one(
                cfg,
                user_id=d.get("user_id") or "",
                device_token=d.get("device_token") or "",
                payload=payload,
                idempotency_key=idempotency_key,
                apns_priority="5",
                push_type="background",
                apns_expiration=expiration,
            )

    results = await asyncio.gather(*(_guarded(d) for d in devices))
    return {
        "payload": payload,
        "events": [r.as_dict() for r in results],
    }
