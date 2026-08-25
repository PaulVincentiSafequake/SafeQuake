"""#135 — an active operator is never signed out by the clock.

Paul, 2026-08-24 (live test batch):
  > It signed me out in the middle of a task.

Root cause: the dashboard token lived 15 minutes and there was no way to
renew it. The first admin call made after that returned "not
authenticated", the dashboard dropped the session, and the operator was
signed out by nothing more than a clock.

What this file locks:
  1. A still-valid token can be renewed — POST /api/auth/refresh returns a
     NEW token with a later expiry.
  2. Renewal carries `auth_iat` (the moment the human actually signed in
     with Google) through unchanged, and can never push the expiry past
     the absolute per-shift limit.
  3. Renewal is NOT a way around revocation: a disabled account, a bumped
     session_version and an expired account are all refused.
  4. An already-expired token cannot be renewed. Renewal keeps an active
     session alive; it does not resurrect a dead one.
  5. The legacy shared token has no session and is refused.
"""
import os
import sys
from datetime import datetime, timedelta, timezone

import jwt as pyjwt
import pytest
import requests
from pymongo import MongoClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
except Exception:
    pass

import auth as auth_mod  # noqa: E402  (after load_dotenv on purpose)

BASE = os.environ.get("QG_BASE", "http://localhost:8001")
API = f"{BASE}/api"
ADMIN_TOKEN = os.environ.get("ADMIN_TRIGGER_PASSWORD", "")
MONGO_URL = os.environ.get("MONGO_URL", "mongodb://localhost:27017")
DB_NAME = os.environ.get("DB_NAME", "test_database")

PREFIX = "test_refresh_135_"


@pytest.fixture(scope="module")
def mongo():
    client = MongoClient(MONGO_URL)
    yield client[DB_NAME]
    client.close()


@pytest.fixture(autouse=True)
def cleanup(mongo):
    mongo.users.delete_many({"email_normalized": {"$regex": f"^{PREFIX}"}})
    yield
    mongo.users.delete_many({"email_normalized": {"$regex": f"^{PREFIX}"}})


def _seed(mongo, name, *, role="operator", disabled=False, session_version=1,
          expires_at=...):
    email = f"{PREFIX}{name}@example.com"
    doc = {
        "email": email,
        "email_normalized": email,
        "display_name": name,
        "role": role,
        "allowed": True,
        "disabled": disabled,
        "session_version": session_version,
        "google_sub": f"{PREFIX}sub-{name}",
        "created_at": datetime.now(timezone.utc),
        "created_by": "test-fixture",
        "expires_at": (datetime.now(timezone.utc) + timedelta(days=90)
                       if expires_at is ... else expires_at),
    }
    mongo.users.insert_one(doc)
    return mongo.users.find_one({"email_normalized": email})


def _claims(token):
    return pyjwt.decode(
        token, os.environ["JWT_SECRET"], algorithms=["HS256"],
        issuer=auth_mod.JWT_ISSUER, audience=auth_mod.JWT_AUDIENCE,
    )


def _refresh(token):
    return requests.post(f"{API}/auth/refresh",
                         headers={"Authorization": f"Bearer {token}"}, timeout=15)


def test_valid_session_renews_and_keeps_working(mongo):
    user = _seed(mongo, "alice")
    token, _ = auth_mod.issue_app_jwt(user)
    r = _refresh(token)
    assert r.status_code == 200, r.text
    fresh = r.json()["token"]
    assert fresh != token
    before, after = _claims(token), _claims(fresh)
    assert after["exp"] >= before["exp"]
    assert after["email"] == before["email"]
    assert after["role"] == before["role"]
    # And the renewed token actually works on a real admin call.
    ok = requests.get(f"{API}/devices",
                      headers={"Authorization": f"Bearer {fresh}"}, timeout=15)
    assert ok.status_code == 200, ok.text


def test_renewal_carries_the_original_sign_in_time(mongo):
    """`auth_iat` is what the absolute per-shift limit is measured from. If
    renewal reset it, a tab left open would never need a fresh sign-in."""
    user = _seed(mongo, "bruno")
    token, _ = auth_mod.issue_app_jwt(user)
    original = _claims(token)["auth_iat"]
    fresh = _refresh(token).json()["token"]
    assert _claims(fresh)["auth_iat"] == original


def test_renewal_can_never_exceed_the_absolute_session_limit(mongo):
    """A token issued at the very start of a shift cannot be renewed into
    the next one — the expiry is clamped, and once the limit is reached
    renewal is refused outright."""
    user = _seed(mongo, "carla")
    hours = auth_mod.JWT_ABSOLUTE_SESSION_HOURS
    # Signed in one minute short of the limit: renewal is allowed, but the
    # new expiry is clamped to the limit rather than a full token life.
    nearly = int((datetime.now(timezone.utc) - timedelta(hours=hours, minutes=-1)).timestamp())
    token, _ = auth_mod.issue_app_jwt(user, auth_iat=nearly)
    claims = _claims(token)
    assert claims["exp"] <= nearly + hours * 3600

    # Signed in past the limit: nothing left to renew.
    past = int((datetime.now(timezone.utc) - timedelta(hours=hours + 1)).timestamp())
    with pytest.raises(auth_mod.AuthError):
        auth_mod.issue_app_jwt(user, auth_iat=past)


def test_a_disabled_account_cannot_renew(mongo):
    user = _seed(mongo, "dave")
    token, _ = auth_mod.issue_app_jwt(user)
    mongo.users.update_one({"_id": user["_id"]}, {"$set": {"disabled": True}})
    assert _refresh(token).status_code == 403


def test_a_revoked_session_cannot_renew(mongo):
    """Bumping session_version is how an admin kills every issued token for
    a person. Renewal must respect it immediately."""
    user = _seed(mongo, "erika", session_version=1)
    token, _ = auth_mod.issue_app_jwt(user)
    mongo.users.update_one({"_id": user["_id"]}, {"$set": {"session_version": 2}})
    assert _refresh(token).status_code == 401


def test_an_expired_account_cannot_renew(mongo):
    user = _seed(mongo, "frank",
                 expires_at=datetime.now(timezone.utc) - timedelta(days=1))
    token, _ = auth_mod.issue_app_jwt(user)
    r = _refresh(token)
    assert r.status_code == 403
    assert "expired" in r.text.lower()


def test_an_already_expired_token_cannot_be_renewed(mongo):
    user = _seed(mongo, "gina")
    dead = pyjwt.encode(
        {
            "sub": user["google_sub"],
            "email": user["email_normalized"],
            "role": user["role"],
            "sv": 1,
            "iss": auth_mod.JWT_ISSUER,
            "aud": auth_mod.JWT_AUDIENCE,
            "iat": int((datetime.now(timezone.utc) - timedelta(hours=2)).timestamp()),
            "auth_iat": int((datetime.now(timezone.utc) - timedelta(hours=2)).timestamp()),
            "exp": int((datetime.now(timezone.utc) - timedelta(hours=1)).timestamp()),
        },
        os.environ["JWT_SECRET"], algorithm="HS256",
    )
    assert _refresh(dead).status_code == 401


def test_no_token_and_legacy_token_are_both_refused():
    assert requests.post(f"{API}/auth/refresh", timeout=15).status_code == 401
    r = requests.post(f"{API}/auth/refresh",
                      headers={"X-Admin-Token": ADMIN_TOKEN}, timeout=15)
    # No bearer session to renew — the shared token is not a session.
    assert r.status_code in (400, 401), r.text


def test_token_life_is_long_enough_to_survive_a_sleeping_laptop():
    """#135's other half: a 15-minute token expired while a laptop slept,
    before any renewal timer could fire. The token life must be
    comfortably longer than a short sleep, and the absolute limit must
    still be a shift rather than forever."""
    assert auth_mod.JWT_TTL_MINUTES >= 30
    assert 1 <= auth_mod.JWT_ABSOLUTE_SESSION_HOURS <= 24
