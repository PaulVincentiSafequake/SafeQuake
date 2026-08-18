"""Emergent (SuprSend) push relay client — the Android/fallback push path.

Extracted from server.py on 2026-06-18 — behaviour unchanged. iOS critical
alerts go direct to APNs (see apns.py); this relay carries Android pushes and
the browser diagnostics self-test.
"""
from __future__ import annotations

import logging
import os
from typing import List, Optional

import httpx
from fastapi import HTTPException

# Emergent Push relay
PUSH_BASE_URL = "https://integrations.emergentagent.com"
PUSH_KEY = os.environ.get("EMERGENT_PUSH_KEY", "placeholder")
push_client = httpx.AsyncClient(
    base_url=PUSH_BASE_URL,
    headers={"X-Push-Key": PUSH_KEY},
    timeout=10.0,
)



async def send_push(
    recipients: List[str],
    data: dict,
    idempotency_key: Optional[str] = None,
) -> List[dict]:
    """Send a push in chunks via the Emergent (SuprSend) relay. Returns a list
    of per-chunk diagnostic events so callers can log/inspect what the relay
    (and downstream APNs/FCM) actually responded with. Never raises for 4xx
    at the relay — those are captured into the event list with ok=false."""
    events: List[dict] = []
    if not recipients:
        return events
    if "title" not in data or "message" not in data:
        raise ValueError("data must include title and message")
    CHUNK = 100
    for i in range(0, len(recipients), CHUNK):
        chunk = recipients[i:i + CHUNK]
        payload = {"recipients": chunk, "data": data}
        if idempotency_key:
            payload["$idempotency_key"] = f"{idempotency_key}-{i // CHUNK}"

        event: dict = {
            "chunk_index": i // CHUNK,
            "chunk_size": len(chunk),
            "recipients_sample": chunk[:20],
            "recipients_total": len(chunk),
            "ok": False,
            "status_code": None,
            "body": None,
            "error": None,
        }
        try:
            resp = await push_client.post("/api/v1/push/trigger", json=payload)
            event["status_code"] = resp.status_code
            # Capture body regardless of status so we can see relay-level errors
            # like "invalid device token" or "APNs Unregistered".
            try:
                event["body"] = resp.json()
            except Exception:
                event["body"] = resp.text[:2000]
            event["ok"] = 200 <= resp.status_code < 300
            if resp.status_code == 401:
                event["error"] = "EMERGENT_PUSH_KEY missing or invalid"
                raise HTTPException(500, "EMERGENT_PUSH_KEY missing or invalid")
            if resp.status_code >= 500:
                event["error"] = f"Push provider {resp.status_code}"
                raise HTTPException(502, "Push provider unavailable")
            if not event["ok"]:
                event["error"] = f"Relay HTTP {resp.status_code}"
                logging.warning(
                    f"Push trigger relay {resp.status_code}: {str(event['body'])[:500]}"
                )
        except HTTPException:
            events.append(event)
            raise
        except Exception as e:
            event["error"] = str(e)
            logging.warning(f"Push trigger failed (non-blocking): {e}")
        events.append(event)
    return events

