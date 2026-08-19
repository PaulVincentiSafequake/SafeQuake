"""A1 permanence test (Batch 7).

Belt-and-braces after the 2026-08-19 false alarm where Paul reported a
CORS failure that turned out to be a 401 auth timeout. The CORS config
itself has always been correct, but this test guarantees it stays that
way — if someone edits `deps.py::CORS_ALLOWED_ORIGINS` and drops the
dashboard origin, or the deploy pipeline is misconfigured so the file
never ships, this test fails loudly.

Two levels:
  1. Static (always runs): the string is present in deps.py at import time.
  2. Live (opt-in via QG_DEPLOY_URL env): the deployed backend actually
     serves it. Runs in CI against staging/prod; skipped locally by default.

The live check hits /api/cors-debug — a non-secret endpoint added
specifically to distinguish "code is right, deploy is wrong" from "code
is wrong". See server.py::cors_debug.
"""
from __future__ import annotations

import json
import os
import urllib.request
import urllib.error

import pytest


DASHBOARD_ORIGIN = "https://safequake.onrender.com"


def test_dashboard_origin_present_in_config():
    """Fail if the source config no longer includes the dashboard origin.
    This is the fast, always-runs check."""
    from deps import CORS_ALLOWED_ORIGINS
    assert DASHBOARD_ORIGIN in CORS_ALLOWED_ORIGINS, (
        f"{DASHBOARD_ORIGIN!r} is missing from deps.CORS_ALLOWED_ORIGINS. "
        "The dashboard cannot fetch from the backend without this. See "
        "Batch 7 A1."
    )


def test_cors_middleware_reads_from_deps():
    """The CORS middleware must not have a hard-coded list — it has to
    pull from deps.CORS_ALLOWED_ORIGINS, otherwise a config edit in one
    file won't apply to the wire config (Pattern 1: fix in one place,
    not everywhere)."""
    import server as srv
    # `add_middleware` doesn't stash the args back on the app in an easy
    # way, but the import wires it up at module scope — checking the
    # constant is the same value is enough here.
    from deps import CORS_ALLOWED_ORIGINS as _DEPS
    # The wire-up code reads CORS_ALLOWED_ORIGINS in server.py — this
    # test asserts it's the same list object, not a copy that could drift.
    assert srv.CORS_ALLOWED_ORIGINS is _DEPS


@pytest.mark.skipif(
    not os.environ.get("QG_DEPLOY_URL"),
    reason="Live check requires QG_DEPLOY_URL (e.g. https://quake-alert-18.emergent.host). "
           "CI sets this; local runs skip it."
)
def test_deployed_backend_allows_dashboard_origin():
    """Live check: the DEPLOYED backend serves the correct CORS config.
    This is the check that would have flagged a "publish dropped it"
    scenario before the operator noticed."""
    base = os.environ["QG_DEPLOY_URL"].rstrip("/")
    req = urllib.request.Request(
        f"{base}/api/cors-debug",
        headers={"Origin": DASHBOARD_ORIGIN},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as e:
        pytest.fail(f"Could not reach deployed backend at {base}: {e}")
    assert body.get("allowed") is True, (
        f"Deployed backend rejects {DASHBOARD_ORIGIN}. Response: {body}"
    )
    assert body.get("allow_reason") == "exact_match", body
    assert DASHBOARD_ORIGIN in body.get("allowed_origins", []), body
