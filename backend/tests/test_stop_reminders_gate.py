"""Batch 7 C3 — server-side refusal of Stop-reminders when unauthenticated.

The dashboard hides the button when signed out (that's the presentation
layer). This test guarantees that even a hand-crafted POST — from curl,
a script, or a bookmarked link — cannot cancel reminders without an
admin/operator token.

The action is system-wide (silent push to every registered iOS device).
Missing the server-side guard would let any anonymous visitor cancel
check-in reminders for people who may be trapped.
"""
from __future__ import annotations
import os
import pytest
import requests

BASE_URL = os.environ.get("BACKEND_URL", "http://localhost:8001")


def test_cancel_reminders_refuses_anonymous():
    """Anonymous POST returns 401. Silent-fail (200 with a body) would
    be worse than the visible refusal, so we assert on the status code."""
    r = requests.post(f"{BASE_URL}/api/admin/reminders/cancel", timeout=5)
    assert r.status_code == 401, (
        f"Unauthenticated /api/admin/reminders/cancel returned {r.status_code} — "
        f"expected 401. This endpoint cancels check-in reminders on every phone "
        f"and MUST refuse anonymous callers. Body: {r.text[:200]}"
    )


def test_cancel_reminders_refuses_bad_token():
    """Wrong admin token also refuses, no timing side-channel."""
    r = requests.post(
        f"{BASE_URL}/api/admin/reminders/cancel",
        headers={"X-Admin-Token": "not-a-real-token"},
        timeout=5,
    )
    assert r.status_code in (401, 403), (
        f"Wrong-token /api/admin/reminders/cancel returned {r.status_code} — "
        f"expected 401 or 403. Body: {r.text[:200]}"
    )
