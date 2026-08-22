"""Shared test setup for the QuakeGuard backend suite.

The whole suite talks to ONE Motor client — the module-level `db` created
when `server.py` is imported. Motor binds that client's sockets to the
first event loop that uses it, so if different tests each spin up their
own loop (which Starlette's TestClient does per request by default, and
`asyncio.run()` does by definition) the second loop inherits sockets
belonging to a loop that has already been closed and Mongo calls blow up
with "Event loop is closed". Tests then pass alone and fail in a full
run, which is exactly the kind of noise that hides real regressions.

Fix: give the entire session a single event loop.

  * `TestClient.portal` is a class attribute Starlette checks before
    creating a per-request portal, so setting it once here makes every
    TestClient instance in the suite share one loop.
  * `run_async` runs a coroutine on that same loop, for the handful of
    tests that call server internals directly instead of over HTTP.
"""
from __future__ import annotations

import anyio.from_thread
import pytest
from dotenv import load_dotenv
from starlette.testclient import TestClient

# Loaded here so every test module can read MONGO_URL / DB_NAME /
# ADMIN_TRIGGER_PASSWORD from the environment instead of carrying its own
# copy of the credentials.
load_dotenv("/app/backend/.env")


@pytest.fixture(scope="session", autouse=True)
def shared_event_loop_portal():
    with anyio.from_thread.start_blocking_portal("asyncio") as portal:
        TestClient.portal = portal
        # Motor caches the event loop on FIRST use and keeps it forever, so
        # pin it to this loop before any test runs. Otherwise whichever test
        # happens to run first decides the loop for the whole session.
        import asyncio

        import deps

        deps.client._io_loop = portal.call(asyncio.get_running_loop)
        try:
            yield portal
        finally:
            TestClient.portal = None


@pytest.fixture()
def run_async(shared_event_loop_portal):
    """Run an async function on the suite-wide event loop.

    Use instead of `asyncio.run(...)` whenever the coroutine touches the
    database, otherwise you get "Event loop is closed" in a full run.
    """
    def _run(func, *args, **kwargs):
        return shared_event_loop_portal.call(lambda: func(*args, **kwargs))
    return _run


@pytest.fixture()
def clear_register_rate_limit():
    """Empty the /register-push per-IP rate-limit buckets.

    A real phone registers once per install; the suite registers dozens of
    times from one IP within the hour, which legitimately trips the limit
    and would otherwise make these tests fail depending on how often they
    were run rather than on what the code does.
    """
    import os

    import pymongo

    m = pymongo.MongoClient(os.environ["MONGO_URL"])
    db = m[os.environ.get("DB_NAME", "test_database")]
    db.push_register_rate_limit.delete_many({})
    yield
    m.close()


@pytest.fixture()
def stand_down_after():
    """Call off any alert the test sent before handing control on.

    A trigger with no stand-down leaves the incident ACTIVE, and an active
    incident legitimately holds records on the working board — which then
    reads as an unrelated failure in whichever board test runs next
    (including on the NEXT run of the suite, since the state is in Mongo).
    Tests that send a real alert must therefore stand it down.
    """
    import os

    import requests

    yield
    requests.post(
        "http://localhost:8001/api/admin/alert/stand-down",
        headers={"X-Admin-Token": os.environ["ADMIN_TRIGGER_PASSWORD"],
                 "Content-Type": "application/json"},
        json={"confirmation_phrase": "STANDDOWN",
              "reason": "test suite cleanup"},
        timeout=15,
    )
