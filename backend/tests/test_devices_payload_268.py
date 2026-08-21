"""#268 — the shape of /api/devices that every surface depends on.

Kept in its own module: this suite's TestClient + Motor combination only
tolerates ONE database-touching request per module (the async loop is
closed after the first), which is why the #262 files are split the same
way. If these keys move, the dashboard silently shows a phantom casualty
again, so this is a pinning test.
"""
from __future__ import annotations

import os

from dotenv import load_dotenv
load_dotenv("/app/backend/.env")

from fastapi.testclient import TestClient  # noqa: E402

from server import app  # noqa: E402

client = TestClient(app)
HDR = {"X-Admin-Token": os.environ.get("ADMIN_TRIGGER_PASSWORD", "")}


def test_devices_payload_carries_the_board_split():
    """The one shape every surface depends on. If these keys move, the
    dashboard silently shows a phantom casualty again."""
    r = client.get("/api/devices?limit=5", headers=HDR)
    assert r.status_code == 200, r.text
    body = r.json()
    for key in ("devices", "off_board", "off_board_count", "notices",
                "counts", "count_notes"):
        assert key in body, key
    for key in ("waiting_for_answer", "phone_went_dark", "app_removed",
                "app_removed_held_on_board", "never_used",
                "resolved_by_operator", "off_board_total"):
        assert key in body["counts"], key
    # Nothing on the working board may be a record we have decided is not
    # a person to search for.
    for d in body["devices"]:
        assert d["record_state"]["on_working_board"] is True
    for d in body["off_board"]:
        assert d["label"]
        assert d["off_board_reason"]
    # Every number states what it leaves out.
    assert any("leaves out records we have set aside" in n
               for n in body["count_notes"])
