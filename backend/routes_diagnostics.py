"""Maintenance and diagnostics endpoints (mostly operator-facing HTML).

Extracted from server.py on 2026-06-18 — behaviour unchanged.

  * mark/unmark a check-in as a test entry, list them, purge them
  * purge leftover synthetic devices (JSON + a browser page)
  * last push-relay responses, registered devices, last registrations
  * self-test push, route pre-flight check, APNs key status

These pages exist because the account owner is the operator: anything that
would otherwise need a curl command gets a one-click surface (the operational
standard locked 2026-08-06 in memory/PRD.md).
"""
from __future__ import annotations

import html as _html
import logging
import uuid
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from apns import apns_config_status
from auth import require_role, resolve_principal
from deps import ADMIN_TRIGGER_PASSWORD, db, is_test_device as _is_test_device
from push_relay import send_push

router = APIRouter()
api_router = router   # endpoints below keep their original decorators verbatim

# ---------- Maintenance: purge leftover test / diagnostic rows ----------
class MarkTestBody(BaseModel):
    is_test: bool = True


@api_router.post("/admin/devices/{device_id}/mark-test")
async def mark_device_as_test(device_id: str, body: MarkTestBody, request: Request):
    """Tag (or untag) one device as a test entry — #146.

    Why an operator needs this: our own test check-ins come from a real
    phone with a real device_id, so no naming pattern can spot them. Once
    tagged, the row still exists (nothing is deleted, the audit trail is
    intact) but the dashboard's trapped list hides it by default.

    Reversible: post {"is_test": false} to put it back. Both directions
    are written to the audit log, because hiding a person from a rescue
    list is a decision someone must be able to account for.
    """
    principal = await resolve_principal(
        request, request.headers.get("x-admin-token"), ADMIN_TRIGGER_PASSWORD, db
    )
    require_role(principal, "admin", "operator")

    res = await db.device_status.update_one(
        {"device_id": device_id},
        {"$set": {"synthetic": bool(body.is_test)}},
    )
    if res.matched_count == 0:
        raise HTTPException(404, "No such device")

    now = datetime.now(timezone.utc).isoformat()
    await db.emsc_audit_log.insert_one({
        "timestamp": now,
        "event_type": "device_marked_test" if body.is_test else "device_unmarked_test",
        "device_id": device_id,
        "context": {"by": principal.get("email"), "is_test": bool(body.is_test)},
    })
    return {"ok": True, "device_id": device_id, "is_test": bool(body.is_test)}


@api_router.get("/admin/test-entries")
async def list_test_entries(request: Request):
    """Preview what a purge would remove, across ALL three collections."""
    principal = await resolve_principal(
        request, request.headers.get("x-admin-token"), ADMIN_TRIGGER_PASSWORD, db
    )
    require_role(principal, "admin", "operator")

    rows = await db.device_status.find({}, {"_id": 0}).to_list(5000)
    test_rows = [r for r in rows if _is_test_device(r)]
    ids = [r.get("device_id") for r in test_rows]
    return {
        "count": len(test_rows),
        "device_status": len(test_rows),
        "status_events": await db.status_events.count_documents({"device_id": {"$in": ids}}) if ids else 0,
        "push_devices": await db.push_devices.count_documents({"user_id": {"$in": ids}}) if ids else 0,
        "devices": [
            {
                "device_id": r.get("device_id"),
                "status": r.get("status"),
                "severity": r.get("severity"),
                "display_name": r.get("display_name"),
                "updated_at": r.get("updated_at"),
                "flagged_by_operator": r.get("synthetic") is True,
            }
            for r in test_rows
        ],
    }


@api_router.post("/admin/purge-test-entries")
async def purge_test_entries(request: Request):
    """Delete every tagged/recognised test entry from the live surfaces.

    Unlike the older /admin/purge-test-devices (which only ever touched
    `push_devices`, and so left the trapped list exactly as cluttered as
    before — the actual cause of #146), this clears `device_status`,
    `status_events` and `push_devices` together.

    ADMIN ONLY, and audited with the full id list: this deletes rows from
    what is, for real people, a legal record. Operators can hide entries
    (mark-test); only an admin can destroy them.
    """
    principal = await resolve_principal(
        request, request.headers.get("x-admin-token"), ADMIN_TRIGGER_PASSWORD, db
    )
    require_role(principal, "admin")

    rows = await db.device_status.find({}, {"_id": 0, "device_id": 1, "synthetic": 1}).to_list(5000)
    ids = [r.get("device_id") for r in rows if _is_test_device(r)]
    if not ids:
        return {"ok": True, "deleted": {"device_status": 0, "status_events": 0, "push_devices": 0}}

    d1 = await db.device_status.delete_many({"device_id": {"$in": ids}})
    d2 = await db.status_events.delete_many({"device_id": {"$in": ids}})
    d3 = await db.push_devices.delete_many({"user_id": {"$in": ids}})

    now = datetime.now(timezone.utc).isoformat()
    await db.emsc_audit_log.insert_one({
        "timestamp": now,
        "event_type": "test_entries_purged",
        "context": {
            "by": principal.get("email"),
            "device_ids": ids,
            "deleted": {
                "device_status": d1.deleted_count,
                "status_events": d2.deleted_count,
                "push_devices": d3.deleted_count,
            },
        },
    })
    return {
        "ok": True,
        "purged_device_ids": ids,
        "deleted": {
            "device_status": d1.deleted_count,
            "status_events": d2.deleted_count,
            "push_devices": d3.deleted_count,
        },
    }


async def _run_purge_test_devices() -> dict:
    result = await db.push_devices.delete_many({
        "$or": [
            {"user_id": {"$regex": "^TEST_"}},
            {"user_id": {"$regex": "^test-"}},
            {"user_id": {"$regex": "^diag-"}},
            {"user_id": "dashboard"},
        ]
    })
    remaining = await db.push_devices.count_documents({})
    return {"deleted": result.deleted_count, "remaining": remaining}

@api_router.post("/admin/purge-test-devices")
async def purge_test_devices(
    x_admin_token: Optional[str] = Header(default=None, alias="X-Admin-Token"),
):
    """Programmatic variant. Password-protected via X-Admin-Token header."""
    if not ADMIN_TRIGGER_PASSWORD:
        raise HTTPException(500, "ADMIN_TRIGGER_PASSWORD not configured on server")
    if x_admin_token != ADMIN_TRIGGER_PASSWORD:
        raise HTTPException(401, "Invalid or missing X-Admin-Token")
    return await _run_purge_test_devices()

@api_router.get("/admin/purge-test-devices", response_class=HTMLResponse)
async def purge_test_devices_browser(
    token: str = Query(default=""),
    confirm: str = Query(default=""),
):
    """Browser-openable variant. Two-step to prevent accidents when the URL
    is shared:
      /api/admin/purge-test-devices?token=<pwd>              → preview page
      /api/admin/purge-test-devices?token=<pwd>&confirm=yes  → actually delete
    """
    if not ADMIN_TRIGGER_PASSWORD:
        return HTMLResponse("<h2>Server error</h2>", status_code=500)
    if token != ADMIN_TRIGGER_PASSWORD:
        return HTMLResponse(
            "<h2 style='color:#c21818'>Wrong password.</h2>"
            "<p>Append <code>?token=&lt;password&gt;</code>.</p>",
            status_code=401,
        )

    # Preview matching rows without deleting
    filt = {
        "$or": [
            {"user_id": {"$regex": "^TEST_"}},
            {"user_id": {"$regex": "^test-"}},
            {"user_id": {"$regex": "^diag-"}},
            {"user_id": "dashboard"},
        ]
    }
    matches = await db.push_devices.find(
        filt, {"_id": 0, "user_id": 1, "platform": 1}
    ).to_list(500)
    total_before = await db.push_devices.count_documents({})

    if confirm != "yes":
        rows_html = "".join(
            f"<li><code>{_html.escape(str(m.get('user_id') or ''))}</code> "
            f"<small style='color:#888'>({_html.escape(str(m.get('platform') or '?'))})</small></li>"
            for m in matches
        ) or "<li style='color:#666;font-style:italic'>Nothing matching to delete.</li>"
        return HTMLResponse(f"""<!doctype html><html><head>
<title>Purge test devices — preview</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="referrer" content="no-referrer">
<style>body{{font-family:-apple-system,Segoe UI,sans-serif;padding:24px;max-width:640px;margin:0 auto;background:#fafafa}}
.card{{border:1px solid #ddd;border-radius:12px;padding:20px;background:#fff}}
h1{{font-size:20px;margin:0 0 8px}}
ul{{padding-left:20px;max-height:280px;overflow:auto;background:#f4f4f6;border-radius:6px;padding:12px 12px 12px 32px}}
.btn{{display:inline-block;background:#C21818;color:#fff;padding:12px 20px;border-radius:8px;
       text-decoration:none;font-weight:700;font-size:14px;margin-top:16px}}
.btn:active{{opacity:.85}}
small{{color:#888}}</style>
</head><body>
<div class="card">
<h1>Preview: {len(matches)} test row(s) will be deleted</h1>
<p><small>Matches user_ids starting with <code>TEST_</code>, <code>test-</code>, <code>diag-</code>, or exactly <code>dashboard</code>. Currently {total_before} total device rows in the DB.</small></p>
<ul>{rows_html}</ul>
<a class="btn" href="?token={_html.escape(token)}&amp;confirm=yes">Confirm delete {len(matches)} row(s)</a>
<p><small style="margin-top:14px;display:block">Tap the red button to actually purge. Just opening this URL does nothing destructive — you have to confirm.</small></p>
</div></body></html>""")

    result = await _run_purge_test_devices()
    return HTMLResponse(f"""<!doctype html><html><head>
<title>Purged</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="referrer" content="no-referrer">
<style>body{{font-family:-apple-system,Segoe UI,sans-serif;padding:24px;max-width:640px;margin:0 auto;background:#fafafa}}
.card{{border:1px solid #ddd;border-radius:12px;padding:20px;background:#fff}}
h1{{font-size:20px;margin:0 0 8px}}
.badge{{display:inline-block;padding:4px 10px;border-radius:999px;color:#fff;font-size:12px;font-weight:700;background:#1F8A3A}}
.kv{{margin-top:14px;font-size:14px;line-height:1.7}}
.kv b{{display:inline-block;min-width:110px;color:#666;font-weight:600}}</style>
</head><body>
<div class="card">
<h1>Purged</h1>
<span class="badge">done</span>
<div class="kv">
<div><b>Deleted:</b> {result['deleted']}</div>
<div><b>Remaining:</b> {result['remaining']} real device row(s)</div>
</div>
</div></body></html>""")

# ---------- Diagnostics: view last N push relay responses ----------
import json as _json

@api_router.get("/admin/last-push-events", response_class=HTMLResponse)
async def last_push_events_browser(
    token: str = Query(default=""),
    limit: int = Query(default=10, ge=1, le=50),
):
    """Browser-viewable diagnostic. Renders the most recent /api/trigger-alert
    attempts with the full raw SuprSend response body per chunk, so we can
    tell whether APNs accepted or rejected each push."""
    if not ADMIN_TRIGGER_PASSWORD:
        return HTMLResponse("<h2>Server error</h2>", status_code=500)
    if token != ADMIN_TRIGGER_PASSWORD:
        return HTMLResponse(
            "<h2 style='color:#c21818'>Wrong password.</h2>"
            "<p>Append <code>?token=&lt;password&gt;</code>.</p>",
            status_code=401,
        )

    events = await db.push_events.find(
        {}, {"_id": 0}
    ).sort("created_at", -1).to_list(limit)

    def render_event(ev: dict) -> str:
        delivered = ev.get("push_delivered")
        badge_color = "#1F8A3A" if delivered else "#C21818"
        badge_text = "delivered" if delivered else "failed"

        # ---- iOS APNs per-recipient rows ----
        apns_rows = ""
        for a in ev.get("apns_events") or []:
            ok = a.get("delivered")
            code_color = "#1F8A3A" if ok else "#C21818"
            reason = a.get("reason") or a.get("error") or ""
            env_badge = a.get("environment") or "?"
            env_color = "#1F8A3A" if env_badge == "production" else ("#F0A500" if env_badge == "sandbox" else "#888")
            apns_rows += f"""
<tr>
<td style="font-family:ui-monospace,Menlo,monospace;font-size:11px">{_html.escape(str(a.get('user_id') or ''))}</td>
<td style="font-family:ui-monospace,Menlo,monospace;font-size:11px">{_html.escape(str(a.get('token_fingerprint') or ''))}</td>
<td><span style="background:{env_color};color:#fff;padding:2px 8px;border-radius:999px;font-size:11px">{_html.escape(env_badge)}</span></td>
<td style="color:{code_color};font-weight:700">{_html.escape(str(a.get('status_code') or '—'))}</td>
<td style="font-size:11px;color:#c21818">{_html.escape(str(reason))}</td>
<td style="font-size:11px;color:#666">{_html.escape(str(a.get('duration_ms') or ''))}ms</td>
</tr>"""
        apns_block = ""
        if apns_rows:
            # Render the exact JSON payload that was POSTed to Apple's APNs.
            # This lets us verify sound.name / interruption-level / critical
            # were actually set at wire time, not just intended in code.
            payload = ev.get("apns_payload")
            payload_html = ""
            if payload:
                payload_pretty = _html.escape(_json.dumps(payload, indent=2))
                aps = payload.get("aps") if isinstance(payload, dict) else None
                sound = aps.get("sound") if isinstance(aps, dict) else None
                is_critical_sound = (
                    isinstance(sound, dict) and sound.get("critical") == 1
                )
                interruption = aps.get("interruption-level") if isinstance(aps, dict) else None
                sound_name = sound.get("name") if isinstance(sound, dict) else None
                summary_color = (
                    "#1F8A3A"
                    if is_critical_sound
                    and interruption == "critical"
                    and sound_name
                    and sound_name != "default"
                    else "#C21818"
                )
                payload_html = f"""
<div style="margin-top:10px">
  <div style="font-size:12px;color:#666;font-weight:700;text-transform:uppercase;letter-spacing:.05em;margin-bottom:6px">APNs request payload</div>
  <div style="display:flex;gap:8px;flex-wrap:wrap;font-size:12px;margin-bottom:6px">
    <span style="background:{summary_color};color:#fff;padding:2px 8px;border-radius:999px;font-weight:700">sound.critical: {_html.escape(str(sound.get('critical') if isinstance(sound, dict) else '—'))}</span>
    <span style="background:{summary_color};color:#fff;padding:2px 8px;border-radius:999px;font-weight:700">sound.name: {_html.escape(str(sound_name or '—'))}</span>
    <span style="background:{summary_color};color:#fff;padding:2px 8px;border-radius:999px;font-weight:700">interruption-level: {_html.escape(str(interruption or '—'))}</span>
  </div>
  <pre style="background:#0e1116;color:#d5dae0;padding:10px;border-radius:6px;font-size:11px;overflow:auto;max-height:280px;white-space:pre-wrap;word-break:break-word">{payload_pretty}</pre>
</div>"""
            else:
                payload_html = '<div style="margin-top:8px;font-size:12px;color:#c21818">⚠️ apns_payload not recorded for this event (pre-payload-capture backend).</div>'

            apns_block = f"""
<div style="margin-top:12px">
  <div style="font-size:12px;color:#666;font-weight:700;text-transform:uppercase;letter-spacing:.05em;margin-bottom:6px">iOS (direct APNs)</div>
  <table style="width:100%;border-collapse:collapse;font-size:12px">
    <thead><tr style="background:#fafafa">
      <th style="text-align:left;padding:6px 8px;border-bottom:1px solid #eee">user_id</th>
      <th style="text-align:left;padding:6px 8px;border-bottom:1px solid #eee">token fp</th>
      <th style="text-align:left;padding:6px 8px;border-bottom:1px solid #eee">env</th>
      <th style="text-align:left;padding:6px 8px;border-bottom:1px solid #eee">HTTP</th>
      <th style="text-align:left;padding:6px 8px;border-bottom:1px solid #eee">reason</th>
      <th style="text-align:left;padding:6px 8px;border-bottom:1px solid #eee">time</th>
    </tr></thead>
    <tbody>{apns_rows}</tbody>
  </table>
  {payload_html}
</div>"""

        # ---- Android SuprSend chunk rows (legacy) ----
        chunks_html = ""
        for ch in ev.get("chunks") or []:
            ok = ch.get("ok")
            ok_color = "#1F8A3A" if ok else "#C21818"
            body_pretty = _html.escape(
                _json.dumps(ch.get("body"), indent=2, default=str)
                if isinstance(ch.get("body"), (dict, list))
                else str(ch.get("body") or "")
            )
            sample = ", ".join(ch.get("recipients_sample") or [])
            if len(ch.get("recipients_sample") or []) < (ch.get("chunk_size") or 0):
                sample += f" …(+{(ch.get('chunk_size') or 0) - len(ch.get('recipients_sample') or [])} more)"
            chunks_html += f"""
<div style="border:1px solid #eee;border-radius:8px;padding:12px;margin-top:10px;background:#fbfbfd">
  <div><b>Android chunk {_html.escape(str(ch.get('chunk_index')))}</b> — status
    <span style="color:{ok_color};font-weight:700">{_html.escape(str(ch.get('status_code')))}</span>
    · {_html.escape(str(ch.get('chunk_size')))} recipient(s)
    {"· error: <code>" + _html.escape(str(ch.get('error'))) + "</code>" if ch.get("error") else ""}
  </div>
  <div style="font-size:12px;color:#666;margin-top:4px"><b>recipients:</b> {_html.escape(sample)}</div>
  <pre style="background:#0e1116;color:#d5dae0;padding:10px;border-radius:6px;font-size:11px;overflow:auto;max-height:280px;white-space:pre-wrap;word-break:break-word;margin-top:8px">{body_pretty}</pre>
</div>"""

        counts = ""
        if ev.get("ios_count") is not None or ev.get("android_count") is not None:
            counts = f" · iOS: {ev.get('ios_count') or 0} · Android: {ev.get('android_count') or 0}"

        return f"""
<div class="card" style="margin-top:14px">
  <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
    <span class="badge" style="background:{badge_color}">{badge_text}</span>
    <span style="font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px;color:#666">{_html.escape(str(ev.get('idempotency_key') or ''))}</span>
  </div>
  <div class="kv" style="margin-top:8px">
    <div><b>When:</b> {_html.escape(str(ev.get('created_at') or ''))}</div>
    <div><b>Triggered by:</b> <code>{_html.escape(str(ev.get('triggered_by') or 'dashboard'))}</code></div>
    <div><b>Magnitude:</b> {_html.escape(str(ev.get('magnitude') or ''))}</div>
    <div><b>Recipients:</b> {ev.get('recipients_total')}{counts}</div>
    {f'<div><b>Error:</b> <code style="color:#c21818">{_html.escape(str(ev.get("push_error")))}</code></div>' if ev.get('push_error') else ''}
  </div>
  {apns_block}
  {chunks_html or ('<div style="color:#666;font-size:12px;margin-top:8px">No Android chunks (all iOS).</div>' if apns_rows else '<div style="color:#666;font-size:12px;margin-top:8px">No chunk events recorded.</div>')}
</div>"""

    if not events:
        body_html = "<div class='card'><p style='color:#666'>No push events recorded yet. Trigger an alert first.</p></div>"
    else:
        body_html = "".join(render_event(ev) for ev in events)

    return HTMLResponse(f"""<!doctype html><html><head>
<title>Quake Angel — last push events</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="referrer" content="no-referrer">
<style>body{{font-family:-apple-system,Segoe UI,sans-serif;padding:24px;max-width:760px;margin:0 auto;background:#f4f4f7}}
.card{{border:1px solid #ddd;border-radius:12px;padding:16px 18px;background:#fff}}
h1{{font-size:20px;margin:0 0 6px}}
.badge{{display:inline-block;padding:4px 10px;border-radius:999px;color:#fff;font-size:12px;font-weight:700}}
.kv{{font-size:14px;line-height:1.7}}
.kv b{{display:inline-block;min-width:110px;color:#666;font-weight:600}}
code{{background:#f4f4f6;padding:1px 6px;border-radius:4px;font-size:12px}}</style>
</head><body>
<div class="card">
  <h1>Last {len(events)} push event(s)</h1>
  <p style="margin:0;color:#666;font-size:13px">Most recent first. Raw SuprSend/APNs response per chunk is shown below each event.</p>
</div>
{body_html}
</body></html>""")

# ---------- Diagnostics: devices, registrations, self-test push ----------
def _fingerprint(token: Optional[str]) -> str:
    if not token:
        return ""
    n = len(token)
    if n <= 16:
        return _html.escape(token)
    return f"{_html.escape(token[:8])}…{_html.escape(token[-8:])}"


@api_router.get("/admin/devices", response_class=HTMLResponse)
async def devices_browser(token: str = Query(default="")):
    """List every registered device with token metadata for diagnosis."""
    if not ADMIN_TRIGGER_PASSWORD:
        return HTMLResponse("<h2>Server error</h2>", status_code=500)
    if token != ADMIN_TRIGGER_PASSWORD:
        return HTMLResponse(
            "<h2 style='color:#c21818'>Wrong password.</h2>"
            "<p>Append <code>?token=&lt;password&gt;</code>.</p>",
            status_code=401,
        )
    rows = await db.push_devices.find({}, {"_id": 0}).sort("updated_at", -1).to_list(1000)
    total = len(rows)

    def render(r: dict) -> str:
        tok = r.get("device_token") or ""
        fp = _fingerprint(tok)
        return f"""<tr>
<td style="font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px">{_html.escape(r.get('user_id') or '')}</td>
<td>{_html.escape((r.get('platform') or '').upper())}</td>
<td style="font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px">{fp}</td>
<td>{len(tok)}</td>
<td style="font-size:11px;color:#666">{_html.escape(r.get('created_at') or '')}</td>
<td style="font-size:11px;color:#666">{_html.escape(r.get('updated_at') or '')}</td>
</tr>"""

    body_html = "".join(render(r) for r in rows) or (
        "<tr><td colspan='6' style='padding:16px;color:#666'>No devices registered.</td></tr>"
    )
    return HTMLResponse(f"""<!doctype html><html><head>
<title>Quake Angel — registered devices</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="referrer" content="no-referrer">
<style>
body{{font-family:-apple-system,Segoe UI,sans-serif;padding:20px;max-width:1000px;margin:0 auto;background:#f4f4f7}}
h1{{font-size:20px;margin:0 0 6px}}
.card{{border:1px solid #ddd;border-radius:12px;padding:16px 18px;background:#fff;margin-bottom:14px}}
table{{width:100%;border-collapse:collapse;background:#fff;border:1px solid #ddd;border-radius:8px;overflow:hidden}}
th,td{{padding:8px 10px;text-align:left;border-bottom:1px solid #eee;vertical-align:top}}
th{{background:#fafafa;font-size:12px;color:#666;font-weight:600;text-transform:uppercase;letter-spacing:.03em}}
tr:last-child td{{border-bottom:0}}
</style></head><body>
<div class="card">
  <h1>{total} registered device row(s)</h1>
  <p style="margin:0;color:#666;font-size:13px">Sorted by most recently updated. Token fingerprint = first 8 + last 8 chars. Valid iOS APNs tokens should be ~64 hex chars.</p>
</div>
<table>
<thead><tr><th>user_id</th><th>platform</th><th>token fingerprint</th><th>len</th><th>created</th><th>updated</th></tr></thead>
<tbody>{body_html}</tbody>
</table>
</body></html>""")


@api_router.get("/admin/last-registrations", response_class=HTMLResponse)
async def last_registrations_browser(
    token: str = Query(default=""),
    limit: int = Query(default=20, ge=1, le=100),
):
    """Show the last N /api/register-push calls with the raw SuprSend response."""
    if not ADMIN_TRIGGER_PASSWORD:
        return HTMLResponse("<h2>Server error</h2>", status_code=500)
    if token != ADMIN_TRIGGER_PASSWORD:
        return HTMLResponse(
            "<h2 style='color:#c21818'>Wrong password.</h2>"
            "<p>Append <code>?token=&lt;password&gt;</code>.</p>",
            status_code=401,
        )
    import json as _json
    logs = await db.push_registrations_log.find(
        {}, {"_id": 0}
    ).sort("created_at", -1).to_list(limit)

    def render(row: dict) -> str:
        ok = row.get("relay_status") and 200 <= (row.get("relay_status") or 0) < 300
        badge_color = "#1F8A3A" if ok else "#C21818"
        badge_text = f"HTTP {row.get('relay_status') or '—'}"
        body_pretty = _html.escape(
            _json.dumps(row.get("relay_body"), indent=2, default=str)
            if isinstance(row.get("relay_body"), (dict, list))
            else str(row.get("relay_body") or "")
        )
        return f"""
<div class="card">
  <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">
    <span class="badge" style="background:{badge_color}">{badge_text}</span>
    <span style="font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px;color:#666">{_html.escape(row.get('user_id') or '')}</span>
    <span style="color:#999;font-size:12px">· {_html.escape((row.get('platform') or '').upper())}</span>
  </div>
  <div class="kv" style="margin-top:8px">
    <div><b>When:</b> {_html.escape(row.get('created_at') or '')}</div>
    <div><b>Token fp:</b> <code>{_fingerprint(None) if not row.get('token_fingerprint') else _html.escape(row.get('token_fingerprint') or '')}</code> (len: {row.get('token_length')})</div>
    {f'<div><b>Error:</b> <code style="color:#c21818">{_html.escape(str(row.get("relay_error")))}</code></div>' if row.get('relay_error') else ''}
  </div>
  <pre style="background:#0e1116;color:#d5dae0;padding:10px;border-radius:6px;font-size:11px;overflow:auto;max-height:280px;white-space:pre-wrap;word-break:break-word;margin-top:8px">{body_pretty or '(empty)'}</pre>
</div>"""

    body_html = "".join(render(r) for r in logs) or (
        "<div class='card'><p style='color:#666'>No registration logs yet. Reopen the app to trigger a re-register.</p></div>"
    )
    return HTMLResponse(f"""<!doctype html><html><head>
<title>Quake Angel — last registrations</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="referrer" content="no-referrer">
<style>body{{font-family:-apple-system,Segoe UI,sans-serif;padding:20px;max-width:760px;margin:0 auto;background:#f4f4f7}}
.card{{border:1px solid #ddd;border-radius:12px;padding:14px 16px;background:#fff;margin-bottom:14px}}
h1{{font-size:20px;margin:0 0 6px}}
.badge{{display:inline-block;padding:4px 10px;border-radius:999px;color:#fff;font-size:12px;font-weight:700}}
.kv{{font-size:14px;line-height:1.7}}
.kv b{{display:inline-block;min-width:100px;color:#666;font-weight:600}}
code{{background:#f4f4f6;padding:1px 6px;border-radius:4px;font-size:12px}}</style>
</head><body>
<div class="card">
  <h1>Last {len(logs)} registration(s)</h1>
  <p style="margin:0;color:#666;font-size:13px">Raw SuprSend response body captured per call.</p>
</div>
{body_html}
</body></html>""")


class SelfTestPushBody(BaseModel):
    user_id: str


@api_router.post("/admin/self-test-push")
async def self_test_push(
    body: SelfTestPushBody,
    x_admin_token: Optional[str] = Header(default=None, alias="X-Admin-Token"),
    token: str = Query(default=""),
):
    """Send a push to exactly one user_id. Auth via X-Admin-Token header OR
    ?token= query for browser convenience. Returns per-chunk relay events."""
    if not ADMIN_TRIGGER_PASSWORD:
        raise HTTPException(500, "ADMIN_TRIGGER_PASSWORD not configured")
    provided = x_admin_token or token
    if provided != ADMIN_TRIGGER_PASSWORD:
        raise HTTPException(401, "Invalid admin token")

    target = (body.user_id or "").strip()
    if not target:
        raise HTTPException(400, "user_id is required")
    device = await db.push_devices.find_one({"user_id": target}, {"_id": 0})
    if not device:
        raise HTTPException(404, f"No device row found for user_id={target}")

    idem = f"selftest-{uuid.uuid4()}"
    events: List[dict] = []
    push_delivered = True
    push_error: Optional[str] = None
    try:
        events = await send_push(
            recipients=[target],
            data={
                "title": "Quake Angel self-test",
                "message": "If you see this, APNs delivery to this device is working.",
                "action_url": "/",
            },
            idempotency_key=idem,
        )
    except HTTPException as e:
        push_delivered = False
        push_error = e.detail
    except Exception as e:
        push_delivered = False
        push_error = str(e)
    if events and not any(ev.get("ok") for ev in events):
        push_delivered = False
        if not push_error:
            first_err = next((ev.get("error") for ev in events if ev.get("error")), None)
            push_error = first_err or "All chunks failed at push relay"

    try:
        await db.push_events.insert_one({
            "idempotency_key": idem,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "triggered_by": "self-test",
            "magnitude": None,
            "recipients_total": 1,
            "recipients_sample": [target],
            "push_delivered": push_delivered,
            "push_error": push_error,
            "chunks": events,
        })
    except Exception as e:
        logging.warning(f"Failed to persist self-test push_events: {e}")

    return {
        "status": "sent",
        "user_id": target,
        "device_platform": device.get("platform"),
        "device_token_fingerprint": _fingerprint(device.get("device_token")),
        "device_token_length": len(device.get("device_token") or ""),
        "push_delivered": push_delivered,
        "push_error": push_error,
        "idempotency_key": idem,
        "chunks": events,
    }


@api_router.get("/admin/self-test-push", response_class=HTMLResponse)
async def self_test_push_browser(
    token: str = Query(default=""),
    user_id: str = Query(default=""),
):
    """Browser form for firing a single-recipient test push."""
    if not ADMIN_TRIGGER_PASSWORD:
        return HTMLResponse("<h2>Server error</h2>", status_code=500)
    if token != ADMIN_TRIGGER_PASSWORD:
        return HTMLResponse(
            "<h2 style='color:#c21818'>Wrong password.</h2>"
            "<p>Append <code>?token=&lt;password&gt;</code>.</p>",
            status_code=401,
        )

    import json as _json
    result_html = ""
    if user_id.strip():
        try:
            result = await self_test_push(
                SelfTestPushBody(user_id=user_id.strip()),
                x_admin_token=ADMIN_TRIGGER_PASSWORD,
                token=ADMIN_TRIGGER_PASSWORD,
            )
            body_str = _html.escape(_json.dumps(result, indent=2, default=str))
            result_html = f'<div class="card"><h3 style="margin-top:0">Result</h3><pre style="background:#0e1116;color:#d5dae0;padding:10px;border-radius:6px;font-size:11px;overflow:auto;max-height:400px;white-space:pre-wrap;word-break:break-word">{body_str}</pre></div>'
        except HTTPException as e:
            result_html = f'<div class="card" style="border-color:#c21818"><h3 style="margin-top:0;color:#c21818">Error {e.status_code}</h3><p>{_html.escape(str(e.detail))}</p></div>'

    return HTMLResponse(f"""<!doctype html><html><head>
<title>Quake Angel — self-test push</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="referrer" content="no-referrer">
<style>body{{font-family:-apple-system,Segoe UI,sans-serif;padding:20px;max-width:760px;margin:0 auto;background:#f4f4f7}}
.card{{border:1px solid #ddd;border-radius:12px;padding:16px;background:#fff;margin-bottom:14px}}
h1{{font-size:20px;margin:0 0 6px}}
input{{width:100%;padding:10px;border:1px solid #ccc;border-radius:8px;font-size:14px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace}}
button{{background:#c21818;color:#fff;border:0;padding:10px 20px;border-radius:8px;font-size:14px;font-weight:600;cursor:pointer;margin-top:10px}}
</style></head><body>
<div class="card">
  <h1>Self-test push</h1>
  <p style="margin:0 0 12px;color:#666;font-size:13px">Send a single-recipient push to identify whether a specific device row reaches its device. Grab the exact user_id from the Diagnostics screen in the app, or from <a href="/api/admin/devices?token={_html.escape(token)}">/admin/devices</a>.</p>
  <form method="GET" action="/api/admin/self-test-push">
    <input type="hidden" name="token" value="{_html.escape(token)}">
    <label style="font-size:12px;color:#666">user_id</label>
    <input type="text" name="user_id" value="{_html.escape(user_id)}" placeholder="qg-xxxxxxxx" autocapitalize="off" autocorrect="off">
    <button type="submit">Send test push</button>
  </form>
</div>
{result_html}
</body></html>""")


# ---------- Routing pre-flight (which push path would this device take?) ----------
@api_router.get("/admin/route-check")
async def route_check(
    user_id: str = Query(..., description="user_id of the device to inspect"),
    token: str = Query(default=""),
    x_admin_token: Optional[str] = Header(default=None, alias="X-Admin-Token"),
):
    """Show which push delivery path a given device would take on the next
    /api/trigger-alert call. Does not send anything. Answers the question:
    'Was the last silent-no-CRITICAL-badge because we fell back to SuprSend?'
    """
    if not ADMIN_TRIGGER_PASSWORD:
        raise HTTPException(500, "ADMIN_TRIGGER_PASSWORD not configured")
    if (x_admin_token or token) != ADMIN_TRIGGER_PASSWORD:
        raise HTTPException(401, "Invalid admin token")

    target = user_id.strip()
    device = await db.push_devices.find_one({"user_id": target}, {"_id": 0})
    if not device:
        raise HTTPException(404, f"No device row for user_id={target}")

    platform = (device.get("platform") or "").lower()
    device_token = device.get("device_token") or ""
    token_len = len(device_token)

    apns_status = await apns_config_status(db)

    # Replicate the exact filter used in /api/trigger-alert.
    would_take_apns = platform == "ios" and bool(device_token)
    would_take_suprsend = platform != "ios"
    would_be_dropped = not would_take_apns and not would_take_suprsend

    if would_take_apns and not apns_status.get("configured"):
        expected_outcome = (
            "APNs config MISSING → send_critical_alerts returns a stub event "
            "with reason APNS_NOT_CONFIGURED; device gets NOTHING."
        )
    elif would_take_apns:
        expected_outcome = (
            "Direct APNs (Critical Alert payload). Screen wakes, CRITICAL "
            "badge shown, plays over silent — provided the device token is a "
            "real production APNs token."
        )
    elif would_take_suprsend:
        expected_outcome = (
            "SuprSend relay → regular push. No CRITICAL badge, no screen "
            "wake, respects silent/DND/Focus."
        )
    else:
        expected_outcome = (
            "DROPPED. Device is marked platform=ios but has no device_token "
            "in the DB — falls through both filters. Re-register from the "
            "Diagnostics screen in the app to fix."
        )

    return {
        "user_id": target,
        "platform_in_db": device.get("platform"),
        "device_token_length": token_len,
        "device_token_fingerprint": (
            f"{device_token[:8]}…{device_token[-8:]}" if token_len > 16 else device_token
        ),
        "routing": {
            "would_take_apns_critical": would_take_apns,
            "would_take_suprsend": would_take_suprsend,
            "would_be_dropped": would_be_dropped,
        },
        "apns_configured": apns_status.get("configured", False),
        "apns_metadata": {
            "key_id": apns_status.get("key_id"),
            "team_id": apns_status.get("team_id"),
            "bundle_id": apns_status.get("bundle_id"),
            "updated_at": apns_status.get("updated_at"),
        },
        "expected_outcome": expected_outcome,
    }


# ---------- APNs auth key status (read-only) ----------
# NOTE: The one-time upload endpoints (GET /admin/apns-key form + POST
# /admin/apns-key) have been removed after the key was successfully
# persisted, to reduce lingering attack surface. If the key ever needs to
# be rotated, restore the upload handler from git history for a single
# session, then remove it again.


@api_router.get("/admin/apns-status")
async def apns_status_json(
    x_admin_token: Optional[str] = Header(default=None, alias="X-Admin-Token"),
    token: str = Query(default=""),
):
    if (x_admin_token or token) != ADMIN_TRIGGER_PASSWORD:
        raise HTTPException(401, "Invalid admin token")
    return await apns_config_status(db)


