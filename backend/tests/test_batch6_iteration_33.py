"""Independent verification for Batch 6 (iteration 33).

Covers:
  A1 — Casualty-report chart / narrative consistency
    - /api/admin/casualty-report/operational.pdf?detail=summary
    - /api/admin/casualty-report/operational.pdf?detail=full
    - /api/admin/casualty-report/public.pdf
    - _bucket_timeline unit-level behaviour (dedupe per device, most-severe wins)
  A0 — dispatch_preview_if_needed
    - revision, magnitude change <0.3 -> revision_no_material_change
    - revision, magnitude change >=0.3 -> update body + title
    - event older than max_event_age_minutes -> event_too_old
  Regression
    - EMSC poller still up (backend logs)
    - /api/devices includes is_test + test_count
    - /api/admin/device-history/{id} flags reconfirmation
    - /api/admin/audit-log/export.csv still has display_name and short_code
"""
from __future__ import annotations

import asyncio
import io
import os
import re
import sys
from datetime import datetime, timedelta, timezone

import pytest
import requests
from dotenv import load_dotenv

# Load backend env so MONGO_URL / ADMIN_TRIGGER_PASSWORD are visible.
load_dotenv("/app/backend/.env")

BASE_URL = os.environ["EXPO_PUBLIC_BACKEND_URL"].rstrip("/") \
    if "EXPO_PUBLIC_BACKEND_URL" in os.environ else None
if not BASE_URL:
    # Fall back to the frontend .env
    with open("/app/frontend/.env") as f:
        for line in f:
            if line.startswith("EXPO_PUBLIC_BACKEND_URL="):
                BASE_URL = line.split("=", 1)[1].strip().rstrip("/")
                break
ADMIN_TOKEN = os.environ["ADMIN_TRIGGER_PASSWORD"]

sys.path.insert(0, "/app/backend")


# ─── A1: casualty PDFs (integration) ────────────────────────────────────
def _pdf_text(pdf_bytes: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError:
        from PyPDF2 import PdfReader  # type: ignore
    r = PdfReader(io.BytesIO(pdf_bytes))
    return "\n".join((p.extract_text() or "") for p in r.pages)


@pytest.fixture(scope="module")
def admin_headers():
    return {"X-Admin-Token": ADMIN_TOKEN}


@pytest.mark.parametrize("url_suffix", [
    "/api/admin/casualty-report/operational.pdf?detail=summary",
    "/api/admin/casualty-report/operational.pdf?detail=full",
    "/api/admin/casualty-report/public.pdf",
])
def test_casualty_pdf_renders_and_has_people_axis(url_suffix, admin_headers):
    r = requests.get(BASE_URL + url_suffix, headers=admin_headers, timeout=60)
    assert r.status_code == 200, r.text[:500]
    assert r.headers["content-type"].startswith("application/pdf"), r.headers
    assert r.content[:4] == b"%PDF", "not a PDF"
    text = _pdf_text(r.content)
    # Y-axis label was missing pre-fix.
    assert "People" in text, f"'People' word missing in {url_suffix}"


def test_pdf_narrative_and_aggregate_do_not_contradict():
    """Table 'Total devices reporting' must never be less than the trapped
    figure in the narrative sentence 'N people told us they were trapped'
    for a given time window."""
    r = requests.get(
        BASE_URL + "/api/admin/casualty-report/operational.pdf?detail=full",
        headers={"X-Admin-Token": ADMIN_TOKEN},
        timeout=60,
    )
    assert r.status_code == 200
    text = _pdf_text(r.content)
    # Extract "Total devices reporting: N"
    m_total = re.search(r"Total devices reporting[:\s]+(\d+)", text)
    m_trapped_narr = re.search(r"(\d+)\s+(?:people|person)\s+told\s+us\s+they\s+(?:were|are)\s+trapped", text, re.I)
    if m_total and m_trapped_narr:
        total = int(m_total.group(1))
        narr = int(m_trapped_narr.group(1))
        # narrative trapped count can never exceed total devices
        assert narr <= total, (
            f"Narrative says {narr} trapped but table says {total} devices"
        )


# ─── A1: _bucket_timeline unit tests ────────────────────────────────────
# A1 reopened 2026-06-18: de-duplicating WITHIN a bucket was not enough. A
# person still trapped an hour later reports again — the C1 re-check ladder
# makes that routine — so one person produced a red bar of 1 in three
# consecutive hours and a reader adding the bars up read three trapped
# people while the sentence underneath said one. The rule is now: each
# device counts ONCE PER STATUS FOR THE WHOLE WINDOW, in the period it
# FIRST reported that status. Invariant: sum of red bars == the narrative's
# "N people told us they were trapped".
def test_bucket_timeline_counts_each_person_once_per_status():
    """One device reporting trapped in three different hours must produce a
    red total of 1, not 3 — this is the A1 press-facing misreading."""
    import reports_export
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    rows = [
        {"device_id": "d1", "status": "trapped", "recorded_at": (now + timedelta(minutes=1)).isoformat()},
        {"device_id": "d1", "status": "trapped", "recorded_at": (now + timedelta(minutes=70)).isoformat()},
        {"device_id": "d1", "status": "trapped", "recorded_at": (now + timedelta(minutes=130)).isoformat()},
    ]
    buckets, _ = reports_export._bucket_timeline(rows, now, now + timedelta(hours=3))
    assert sum(b["trapped"] for b in buckets) == 1, (
        "one person still trapped across three hours must not read as three people"
    )
    # and it lands in the FIRST hour, when they first told us
    assert buckets[0]["trapped"] == 1


def test_bucket_timeline_counts_first_of_each_status():
    """A person who was trapped and later checked in safe appears once in
    each series — the safe check-in is a real event the chart must show —
    but never twice in the same series."""
    import reports_export
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    rows = [
        {"device_id": "d1", "status": "trapped", "recorded_at": (now + timedelta(minutes=1)).isoformat()},
        {"device_id": "d1", "status": "trapped", "recorded_at": (now + timedelta(minutes=70)).isoformat()},
        {"device_id": "d1", "status": "safe",    "recorded_at": (now + timedelta(minutes=130)).isoformat()},
        {"device_id": "d1", "status": "safe",    "recorded_at": (now + timedelta(minutes=190)).isoformat()},
    ]
    buckets, _ = reports_export._bucket_timeline(rows, now, now + timedelta(hours=4))
    assert sum(b["trapped"] for b in buckets) == 1
    assert sum(b["safe"] for b in buckets) == 1
    assert sum(b["rescued"] for b in buckets) == 0


def test_bucket_series_totals_never_exceed_distinct_devices():
    import reports_export
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    rows = []
    for i in range(3):
        did = f"d{i}"
        for j in range(4):
            rows.append({
                "device_id": did,
                "status": ["trapped", "safe", "rescued", "trapped"][j],
                "recorded_at": (now + timedelta(minutes=j*3)).isoformat(),
            })
    buckets, _ = reports_export._bucket_timeline(rows, now, now + timedelta(hours=1))
    distinct = 3
    for series in ("trapped", "safe", "rescued"):
        total = sum(b[series] for b in buckets)
        assert total <= distinct, f"{series} total {total} > distinct devices {distinct}"


def test_seeded_chartchk_device_contributes_one_red_bar():
    """The seeded qg-1787000000000-chartchk device with 5 alternating
    events must contribute exactly 1 to the red series."""
    import reports_export
    from pymongo import MongoClient
    c = MongoClient(os.environ["MONGO_URL"])
    db = c[os.environ.get("DB_NAME", "test_database")]
    rows = list(db.status_events.find(
        {"device_id": "qg-1787000000000-chartchk"}
    ))
    assert len(rows) == 5
    # Window derived from the seeded rows themselves — the seed is fixed in
    # time, so a window anchored to "now" drifts out of range as time passes.
    stamps = sorted(
        datetime.fromisoformat(str(r["recorded_at"]).replace("Z", "+00:00"))
        for r in rows
    )
    since = stamps[0] - timedelta(hours=1)
    until = stamps[-1] + timedelta(hours=1)
    buckets, _ = reports_export._bucket_timeline(rows, since, until)
    assert sum(b["trapped"] for b in buckets) == 1
    assert sum(b["rescued"] for b in buckets) == 0


def test_red_bars_sum_equals_narrative_trapped_figure():
    """The A1 invariant, stated as a test: adding the red bars must give the
    same number as the sentence under the chart."""
    import reports_export
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    rows = []
    for i in range(4):
        for h in range(3):          # each person re-reports every hour
            rows.append({
                "device_id": f"p{i}",
                "status": "trapped",
                "recorded_at": (now + timedelta(hours=h, minutes=2)).isoformat(),
            })
    buckets, _ = reports_export._bucket_timeline(rows, now, now + timedelta(hours=3))
    red = sum(b["trapped"] for b in buckets)
    figures = reports_export._progress_figures(rows, [], {})
    assert red == figures["total_trapped_reports"] == 4, (
        f"chart red bars {red} vs narrative {figures['total_trapped_reports']}"
    )


# ─── A0: dispatch_preview_if_needed ────────────────────────────────────
class _FakeCollection:
    def __init__(self, docs=None):
        self.docs = list(docs or [])
        self.inserted = []

    async def find_one(self, query, projection=None, sort=None):
        # Very simple matcher supporting equality + $gte on sent_at
        def match(d):
            for k, v in query.items():
                if k == "delivered":
                    if d.get("delivered") != v:
                        return False
                elif k == "sent_at" and isinstance(v, dict):
                    gte = v.get("$gte")
                    if gte is not None and d.get("sent_at", datetime.min.replace(tzinfo=timezone.utc)) < gte:
                        return False
                elif isinstance(v, dict) and "$in" in v:
                    if d.get(k) not in v["$in"]:
                        return False
                elif "." in k:
                    parts = k.split(".")
                    cur = d
                    for p in parts:
                        cur = (cur or {}).get(p) if isinstance(cur, dict) else None
                    if cur != v:
                        return False
                elif d.get(k) != v:
                    return False
            return True
        matches = [d for d in self.docs if match(d)]
        if sort:
            key, direction = sort[0]
            matches.sort(key=lambda x: x.get(key) or datetime.min.replace(tzinfo=timezone.utc), reverse=direction < 0)
        return matches[0] if matches else None

    async def insert_one(self, doc):
        self.inserted.append(doc)
        self.docs.append(doc)

    def find(self, query, projection=None):
        class _Cur:
            def __init__(s, rows):
                s.rows = rows
            async def to_list(s, n):
                return list(s.rows)[:n]
        rows = []
        for d in self.docs:
            ok = True
            for k, v in query.items():
                if isinstance(v, dict):
                    if "$in" in v and d.get(k) not in v["$in"]:
                        ok = False
                    if "$exists" in v and bool(v["$exists"]) != (d.get(k) is not None):
                        ok = False
                    if "$ne" in v and d.get(k) == v["$ne"]:
                        ok = False
                elif d.get(k) != v:
                    ok = False
                if not ok:
                    break
            if ok:
                rows.append(d)
        return _Cur(rows)


class _FakeDB:
    def __init__(self):
        self.emsc_preview_notifications = _FakeCollection()
        self.push_devices = _FakeCollection([
            {"user_id": "dev-A", "device_token": "tok", "platform": "ios",
             "notification_preset": "everything_nearby"}
        ])
        self.user_places = _FakeCollection()


def _country_cfg():
    return {
        "country_code": "MT",
        "country_name": "Malta",
        "center": {"lat": 35.9375, "lon": 14.3754},
        "poll_radius_km": 600.0,
        "preview_mode": {
            "enabled": True,
            "device_ids": ["dev-A"],
            "trigger_tier": "all_ingested",
            "rate_limit_minutes": 10,
            "max_event_age_minutes": 90,
        },
    }


async def _fake_send(**kw):
    return {"events": [
        {"user_id": d.get("user_id"), "delivered": True, "apns_id": "x", "status_code": 200}
        for d in kw["devices"]
    ]}


def test_preview_revision_small_change_suppressed():
    from emsc.preview import dispatch_preview_if_needed
    db = _FakeDB()
    # Seed a prior delivered notice at M3.3
    db.emsc_preview_notifications.docs.append({
        "delivered": True,
        "sent_at": datetime.now(timezone.utc) - timedelta(minutes=2),
        "emsc_event_ref": {"provider": "EMSC", "external_id": "evt-1",
                           "revision": 1, "magnitude": 3.3},
    })
    event = {
        "provider": "EMSC", "external_id": "evt-1", "revision": 2,
        "magnitude": 3.5, "depth_km": 10,
        "latitude": 36.5, "longitude": 14.5,
        "region": "Sicily",
        "observed_at": datetime.now(timezone.utc) - timedelta(minutes=5),
        "evaluations": [],
        "intensity_estimates": {"at_MT_center": {}},
    }
    r = asyncio.run(
        dispatch_preview_if_needed(
            db=db, apns_send_preview=_fake_send,
            emsc_event=event, country_config=_country_cfg(),
        )
    )
    assert r is None
    skips = [d for d in db.emsc_preview_notifications.inserted
             if d.get("skipped_reason") == "revision_no_material_change"]
    assert len(skips) == 1, f"expected suppression skip, got inserted={db.emsc_preview_notifications.inserted}"


def test_preview_revision_material_change_sends_update():
    from emsc.preview import dispatch_preview_if_needed
    db = _FakeDB()
    db.emsc_preview_notifications.docs.append({
        "delivered": True,
        "sent_at": datetime.now(timezone.utc) - timedelta(minutes=2),
        "emsc_event_ref": {"provider": "EMSC", "external_id": "evt-2",
                           "revision": 1, "magnitude": 3.3},
    })
    event = {
        "provider": "EMSC", "external_id": "evt-2", "revision": 2,
        "magnitude": 3.7, "depth_km": 10,
        "latitude": 36.5, "longitude": 14.5,
        "region": "Sicily",
        "observed_at": datetime.now(timezone.utc) - timedelta(minutes=5),
        "evaluations": [],
        "intensity_estimates": {"at_MT_center": {"mmi_predicted_upper_band": 4.5}},
    }
    r = asyncio.run(
        dispatch_preview_if_needed(
            db=db, apns_send_preview=_fake_send,
            emsc_event=event, country_config=_country_cfg(),
        )
    )
    assert r is not None
    # Look at the delivered insert
    delivered = [d for d in db.emsc_preview_notifications.inserted if d.get("delivered")]
    assert delivered, "expected a delivered row"
    row = delivered[0]
    assert row.get("title") == "PREVIEW · Updated seismic reading", row.get("title")
    assert row.get("body", "").startswith("Updated: now measured at M"), row.get("body")


def test_preview_event_too_old_skipped():
    from emsc.preview import dispatch_preview_if_needed
    db = _FakeDB()
    event = {
        "provider": "EMSC", "external_id": "evt-old", "revision": 1,
        "magnitude": 3.7, "depth_km": 10,
        "latitude": 36.5, "longitude": 14.5,
        "region": "Sicily",
        # 3 hours old > default 90 minute cap
        "observed_at": datetime.now(timezone.utc) - timedelta(hours=3),
        "evaluations": [],
        "intensity_estimates": {"at_MT_center": {}},
    }
    r = asyncio.run(
        dispatch_preview_if_needed(
            db=db, apns_send_preview=_fake_send,
            emsc_event=event, country_config=_country_cfg(),
        )
    )
    assert r is None
    skips = [d for d in db.emsc_preview_notifications.inserted
             if str(d.get("skipped_reason", "")).startswith("event_too_old")]
    assert len(skips) == 1


def test_preview_fresh_first_notice_sends():
    from emsc.preview import dispatch_preview_if_needed
    db = _FakeDB()
    event = {
        "provider": "EMSC", "external_id": "evt-new", "revision": 1,
        "magnitude": 3.7, "depth_km": 10,
        "latitude": 36.5, "longitude": 14.5,
        "region": "Sicily",
        "observed_at": datetime.now(timezone.utc) - timedelta(minutes=2),
        "evaluations": [],
        "intensity_estimates": {"at_MT_center": {"mmi_predicted_upper_band": 4.5}},
    }
    r = asyncio.run(
        dispatch_preview_if_needed(
            db=db, apns_send_preview=_fake_send,
            emsc_event=event, country_config=_country_cfg(),
        )
    )
    assert r is not None
    delivered = [d for d in db.emsc_preview_notifications.inserted if d.get("delivered")]
    assert delivered
    assert delivered[0].get("title") == "PREVIEW · Seismic activity"


# ─── REGRESSION ─────────────────────────────────────────────────────────
def test_emsc_poller_no_preview_tracebacks():
    out = ""
    for f in ["/var/log/supervisor/backend.err.log", "/var/log/supervisor/backend.out.log"]:
        try:
            with open(f) as fh:
                out += fh.read()
        except FileNotFoundError:
            pass
    # look for tracebacks mentioning preview.py or dispatch functions
    bad_markers = ["Traceback"]
    tb_windows = []
    for i, line in enumerate(out.splitlines()):
        if "Traceback" in line:
            tb_windows.append("\n".join(out.splitlines()[i:i+15]))
    offenders = [tb for tb in tb_windows
                 if "preview.py" in tb or "dispatch_preview_if_needed" in tb
                 or "dispatch_place_notices" in tb]
    assert not offenders, f"preview-related tracebacks: {offenders[:1]}"
    # positive check: poller started at least once recently
    assert "EMSC poller started" in out


def test_devices_endpoint_has_is_test_and_test_count():
    r = requests.get(BASE_URL + "/api/devices",
                     headers={"X-Admin-Token": ADMIN_TOKEN}, timeout=30)
    assert r.status_code == 200, r.text[:200]
    body = r.json()
    assert "test_count" in body, body
    devices = body.get("devices") if isinstance(body, dict) else body
    assert isinstance(devices, list)
    if devices:
        assert "is_test" in devices[0]


def test_device_history_reconfirmation_flag():
    did = "qg-1787000000001-reconf"
    r = requests.get(BASE_URL + f"/api/admin/device-history/{did}",
                     headers={"X-Admin-Token": ADMIN_TOKEN}, timeout=30)
    assert r.status_code == 200, r.text[:200]
    body = r.json()
    hist = body.get("history") or body.get("events") or body
    if isinstance(hist, dict):
        hist = hist.get("history") or hist.get("events") or []
    assert isinstance(hist, list), body
    recon = [h for h in hist if h.get("reconfirmation") is True]
    # 3 identical trapped events -> the 2nd and 3rd should be reconfirmations
    assert len(recon) >= 1, f"expected reconfirmation flag, got {hist}"


def test_audit_log_export_csv_columns():
    r = requests.get(BASE_URL + "/api/admin/audit-log/export.csv",
                     headers={"X-Admin-Token": ADMIN_TOKEN}, timeout=30)
    assert r.status_code == 200, r.text[:200]
    body = r.text
    assert "display_name" in body, "display_name column missing"
    assert "short_code" in body, "short_code column missing"
