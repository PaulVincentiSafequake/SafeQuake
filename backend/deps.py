"""Shared runtime dependencies: env, Mongo handle, and cross-module helpers.

Created 2026-06-18 while splitting server.py (6,000+ lines) into modules.
Everything here previously lived at the top of server.py; the values are
identical and there is exactly ONE Mongo client for the process, as before.

Import rules that keep this safe:
  * .env is loaded HERE, before anything reads os.environ.
  * This module imports nothing from server.py — the dependency arrow only
    ever points inwards, so route modules can import it freely without a
    circular-import risk.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from dotenv import dotenv_values, load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / ".env")

# ---------- MongoDB ----------
client = AsyncIOMotorClient(os.environ["MONGO_URL"])
db = client[os.environ["DB_NAME"]]

# ---------- Admin shared secret (legacy, being retired) ----------
# .env-first read (targeted inversion of the default OS-wins priority): the
# production deploy pipeline injects a stale OS-level ADMIN_TRIGGER_PASSWORD
# that survives redeploys and cannot be edited from any console, so with the
# default load_dotenv() behaviour a rotation via .env would never take
# effect. Scoped to this ONE key — every other variable keeps OS priority so
# the prod-injected MONGO_URL etc. still win. See the 2026-08-04 incident
# notes in memory/test_credentials.md.
_env_file_values = dotenv_values(ROOT_DIR / ".env")
ADMIN_TRIGGER_PASSWORD = (
    _env_file_values.get("ADMIN_TRIGGER_PASSWORD")
    or os.environ.get("ADMIN_TRIGGER_PASSWORD", "")
)

# ---------- CORS allowlist (single source of truth) ----------
# Both the CORSMiddleware wire-up in server.py AND /api/cors-debug read these.
CORS_ALLOWED_ORIGINS: List[str] = [
    "https://safequake.onrender.com",
    "https://malta.quakeangel.app",
    "https://quakeangel.app",
    "https://www.quakeangel.app",
]
CORS_ALLOWED_ORIGIN_REGEX = (
    r"^(http://localhost:\d+|https://[a-z0-9-]+\.quakeangel\.app)$"
)


def short_code(device_id: Optional[str]) -> Optional[str]:
    """Rescuer-facing tie-breaker code. Last 5 chars of the device_id,
    uppercased. Not unique globally — it exists ONLY to disambiguate 2-3
    victim pins already narrowed down by GPS proximity in the field.

    Returns None when device_id is missing / too short to be meaningful.
    """
    if not device_id:
        return None
    tail = str(device_id)[-5:]
    if len(tail) < 3:
        return None
    return tail.upper()


def iso_utc(value):
    """ISO-8601 with an explicit UTC offset, or None.

    Motor hands back NAIVE datetimes, and a naive isoformat() has no offset;
    JavaScript then reads it as LOCAL time. That rendered an 08:07 UTC quake
    as 08:07 on a Malta (UTC+2) phone — two hours early, on the timestamp
    users compare a notification's arrival against (2026-08-18). Every
    timestamp leaving the API goes through here.
    """
    if not value:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()
    return value


# ---------- EMSC/USGS background workers ----------
# Instantiated here (not in server.py) so route modules can read poller state
# without importing server — that import direction would be circular. Started
# and stopped by server.py's startup/shutdown handlers, exactly as before.
from apns import send_preview_alerts as _send_preview_alerts  # noqa: E402
from apns import send_recheck_prompts as _send_recheck_prompts  # noqa: E402
from emsc.poller import EMSCPoller  # noqa: E402
from emsc.testimonies import TestimoniesSweeper  # noqa: E402
from recheckin import RecheckSweeper  # noqa: E402

emsc_poller = EMSCPoller(db, apns_send_preview=_send_preview_alerts)
emsc_testimonies = TestimoniesSweeper(db)

# C1 — periodic re-check ladder for people who reported trapped.
recheck_sweeper = RecheckSweeper(db, apns_send_rechecks=_send_recheck_prompts)


import re as _re  # noqa: E402

# ---------- #146: telling test entries apart from real casualties -------
# Synthetic/test rows used to sit in the live trapped list looking exactly
# like real people. That's dangerous in both directions: an operator can
# waste attention on a ghost, or dismiss a real person as "probably another
# test". Detection is deliberately two-pronged.
TEST_DEVICE_MARKERS = (
    "test",       # qg-snippet-test-…, qg-rescue-test-…, TEST_…
    "e2e",        # qg-rescue-e2e-…
    "loadtest",   # B5 load-test seeder
    "diag",       # diagnostics screen
    "snippet",    # browser-automation harnesses
    "playwright",
    "demo",
    "-mob-",      # qg-mob-safe-…, qg-mob-mobile-…
)

# The mobile app's own ids look like `qg-<13-digit epoch>-<8 random chars>`.
# Anything matching this is treated as a REAL person no matter what its
# random suffix happens to spell, so a marker substring can never
# accidentally hide a genuine casualty. Only the explicit operator flag can
# hide one of these — a decision with a name attached to it in the audit log.
_REAL_DEVICE_ID_RE = _re.compile(r"^qg-\d{10,14}-[a-z0-9]{6,12}$")


def is_test_device(row: dict) -> bool:
    """True when a device_status row is a test/synthetic entry.

    The explicit flag comes first and is the important one: test check-ins
    made from a real phone (which is how ours are made) cannot be
    recognised from the id, so an operator tags them by hand.
    """
    if row.get("synthetic") is True:
        return True
    did = str(row.get("device_id") or "")
    if did == "dashboard":
        return True
    if _REAL_DEVICE_ID_RE.match(did):
        return False
    low = did.lower()
    return any(m in low for m in TEST_DEVICE_MARKERS)


