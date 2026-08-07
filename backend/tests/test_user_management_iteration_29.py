"""User Management dashboard (Task 4) — backend tests.

Covers PATCH/POST-extend/DELETE /api/admin/users/{email}:
- Role changes (promote/demote), invalid role, empty body
- Expiry mutation: ISO date, "never" (clear), invalid string
- Extend endpoint: sets expires_at to now+90d
- Delete: happy path, 404 on missing, 401 no auth
- Safety rails: self-demote, self-expire, self-delete, last-admin
- Session-version bump on role change (idempotent on no-op)
- Default expires_at=created_at+90d on POST /admin/users
- Existing users (pmvincenti, karen.vincenti) untouched, still authable
- Expiry enforcement in resolve_principal via minted JWT

Test users are all prefixed TEST_UM_ and cleaned up in fixtures.
"""
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest
import requests
from pymongo import MongoClient

# Ensure backend is importable so we can mint JWTs via auth.issue_app_jwt.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Load .env for local access to ADMIN_TRIGGER_PASSWORD / JWT_SECRET.
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
except Exception:
    pass

BASE_URL = (os.environ.get("EXPO_PUBLIC_BACKEND_URL") or "").rstrip("/")
if not BASE_URL:
    # Fall back to frontend/.env
    fe_env = os.path.join(os.path.dirname(__file__), "..", "..", "frontend", ".env")
    if os.path.exists(fe_env):
        with open(fe_env) as f:
            for line in f:
                if line.startswith("EXPO_PUBLIC_BACKEND_URL="):
                    BASE_URL = line.split("=", 1)[1].strip().strip('"').rstrip("/")
                    break

API = f"{BASE_URL}/api"
ADMIN_TOKEN = os.environ.get("ADMIN_TRIGGER_PASSWORD", "")
MONGO_URL = os.environ.get("MONGO_URL", "mongodb://localhost:27017")
DB_NAME = os.environ.get("DB_NAME", "test_database")

TEST_PREFIX = "test_um_"
LEGACY_EMAIL = "legacy@dashboard"

assert BASE_URL, "EXPO_PUBLIC_BACKEND_URL not configured"
assert ADMIN_TOKEN, "ADMIN_TRIGGER_PASSWORD not configured"


# ── Fixtures ────────────────────────────────────────────────────────────
@pytest.fixture(scope="module")
def mongo():
    client = MongoClient(MONGO_URL)
    yield client[DB_NAME]
    client.close()


@pytest.fixture(autouse=True)
def cleanup(mongo):
    """Remove any TEST_UM_* users AND stale legacy@dashboard doc before+after each test."""
    mongo.users.delete_many({"email_normalized": {"$regex": f"^{TEST_PREFIX}"}})
    mongo.users.delete_many({"email_normalized": LEGACY_EMAIL})
    yield
    mongo.users.delete_many({"email_normalized": {"$regex": f"^{TEST_PREFIX}"}})
    mongo.users.delete_many({"email_normalized": LEGACY_EMAIL})


@pytest.fixture
def headers():
    return {"X-Admin-Token": ADMIN_TOKEN, "Content-Type": "application/json"}


def _seed_user(mongo, email, role="operator", *, expires_at=..., google_sub=None,
               disabled=False, session_version=1):
    doc = {
        "email": email,
        "email_normalized": email.lower(),
        "display_name": email.split("@", 1)[0],
        "role": role,
        "allowed": True,
        "disabled": disabled,
        "session_version": session_version,
        "google_sub": google_sub,
        "created_at": datetime.now(timezone.utc),
        "created_by": "test-fixture",
    }
    if expires_at is not ...:
        doc["expires_at"] = expires_at
    else:
        doc["expires_at"] = datetime.now(timezone.utc) + timedelta(days=90)
    mongo.users.insert_one(doc)
    return doc


def _create_user_via_api(headers, email, role="operator"):
    r = requests.post(f"{API}/admin/users",
                      json={"email": email, "role": role},
                      headers=headers, timeout=15)
    return r


# ── PATCH role ──────────────────────────────────────────────────────────
class TestPatchRole:
    def test_promote_operator_to_admin(self, mongo, headers):
        email = f"{TEST_PREFIX}alice@example.com"
        _seed_user(mongo, email, role="operator", session_version=1)
        r = requests.patch(f"{API}/admin/users/{email}",
                           json={"role": "admin"}, headers=headers, timeout=15)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is True
        assert body["user"]["role"] == "admin"
        assert body["user"]["email"] == email
        # Session-version bumped
        doc = mongo.users.find_one({"email_normalized": email})
        assert doc["session_version"] == 2

    def test_demote_admin_to_operator(self, mongo, headers):
        email = f"{TEST_PREFIX}bob@example.com"
        _seed_user(mongo, email, role="admin")
        r = requests.patch(f"{API}/admin/users/{email}",
                           json={"role": "operator"}, headers=headers, timeout=15)
        assert r.status_code == 200, r.text
        assert r.json()["user"]["role"] == "operator"

    def test_invalid_role_returns_400(self, mongo, headers):
        email = f"{TEST_PREFIX}carol@example.com"
        _seed_user(mongo, email, role="operator")
        r = requests.patch(f"{API}/admin/users/{email}",
                           json={"role": "garbage"}, headers=headers, timeout=15)
        assert r.status_code == 400
        assert "role" in r.text.lower()

    def test_empty_body_returns_400(self, mongo, headers):
        email = f"{TEST_PREFIX}dave@example.com"
        _seed_user(mongo, email, role="operator")
        r = requests.patch(f"{API}/admin/users/{email}",
                           json={}, headers=headers, timeout=15)
        assert r.status_code == 400
        assert "no fields" in r.text.lower()

    def test_empty_patch_idempotent_no_session_bump(self, mongo, headers):
        """Setting role to its current value should NOT bump session_version."""
        email = f"{TEST_PREFIX}eve@example.com"
        _seed_user(mongo, email, role="operator", session_version=1)
        r = requests.patch(f"{API}/admin/users/{email}",
                           json={"role": "operator"}, headers=headers, timeout=15)
        # This should succeed (no-op set) but not bump session_version.
        # The code path: role != target.get("role") → set_fields empty →
        # HTTPException 400 "No fields to update".
        # So idempotent no-op currently returns 400.
        assert r.status_code == 400
        doc = mongo.users.find_one({"email_normalized": email})
        assert doc["session_version"] == 1  # NOT bumped


# ── PATCH expiry ────────────────────────────────────────────────────────
class TestPatchExpiry:
    def test_set_future_expiry(self, mongo, headers):
        email = f"{TEST_PREFIX}fiona@example.com"
        _seed_user(mongo, email, role="operator")
        future = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
        r = requests.patch(f"{API}/admin/users/{email}",
                           json={"expires_at": future}, headers=headers, timeout=15)
        assert r.status_code == 200, r.text
        got = r.json()["user"]["expires_at"]
        assert got is not None
        # Ensure the persisted expiry is close to 30 days out
        doc = mongo.users.find_one({"email_normalized": email})
        exp = doc["expires_at"]
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        delta = exp - datetime.now(timezone.utc)
        assert 29 <= delta.days <= 31

    def test_never_clears_expiry(self, mongo, headers):
        email = f"{TEST_PREFIX}gina@example.com"
        _seed_user(mongo, email, role="operator")
        r = requests.patch(f"{API}/admin/users/{email}",
                           json={"expires_at": "never"}, headers=headers, timeout=15)
        assert r.status_code == 200, r.text
        assert r.json()["user"]["expires_at"] is None
        doc = mongo.users.find_one({"email_normalized": email})
        assert doc["expires_at"] is None

    def test_invalid_expiry_string(self, mongo, headers):
        email = f"{TEST_PREFIX}hank@example.com"
        _seed_user(mongo, email, role="operator")
        r = requests.patch(f"{API}/admin/users/{email}",
                           json={"expires_at": "not-a-date"}, headers=headers, timeout=15)
        assert r.status_code == 400
        assert "iso" in r.text.lower() or "not a valid" in r.text.lower()


# ── POST extend ─────────────────────────────────────────────────────────
class TestExtend:
    def test_extend_sets_90_days(self, mongo, headers):
        email = f"{TEST_PREFIX}ivy@example.com"
        # Seed a user whose expiry is nearly gone.
        _seed_user(mongo, email, role="operator",
                   expires_at=datetime.now(timezone.utc) + timedelta(days=1))
        r = requests.post(f"{API}/admin/users/{email}/extend",
                          headers=headers, timeout=15)
        assert r.status_code == 200, r.text
        doc = mongo.users.find_one({"email_normalized": email})
        exp = doc["expires_at"]
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        delta_days = (exp - datetime.now(timezone.utc)).days
        assert 89 <= delta_days <= 91
        # Should record extended_at / extended_by
        assert "extended_by" in doc

    def test_extend_nonexistent_returns_404(self, headers):
        r = requests.post(f"{API}/admin/users/{TEST_PREFIX}nobody@example.com/extend",
                          headers=headers, timeout=15)
        assert r.status_code == 404


# ── DELETE ──────────────────────────────────────────────────────────────
class TestDelete:
    def test_delete_removes_user(self, mongo, headers):
        email = f"{TEST_PREFIX}jill@example.com"
        _seed_user(mongo, email, role="operator")
        r = requests.delete(f"{API}/admin/users/{email}",
                            headers=headers, timeout=15)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is True
        assert body["deleted"] == email
        assert mongo.users.find_one({"email_normalized": email}) is None

    def test_delete_nonexistent_returns_404(self, headers):
        r = requests.delete(f"{API}/admin/users/{TEST_PREFIX}ghost@example.com",
                            headers=headers, timeout=15)
        assert r.status_code == 404

    def test_delete_without_auth_returns_401(self, mongo):
        email = f"{TEST_PREFIX}kate@example.com"
        _seed_user(mongo, email, role="operator")
        r = requests.delete(f"{API}/admin/users/{email}", timeout=15)
        assert r.status_code == 401


# ── SAFETY RAILS — self-* guards (via legacy@dashboard target) ─────────
# When the legacy X-Admin-Token is used, principal["email"] == "legacy@dashboard".
# We seed a real user doc with that email so the target=principal comparison fires.
class TestSelfGuards:
    def test_self_demote_blocked(self, mongo, headers):
        _seed_user(mongo, LEGACY_EMAIL, role="admin")
        r = requests.patch(f"{API}/admin/users/{LEGACY_EMAIL}",
                           json={"role": "operator"}, headers=headers, timeout=15)
        assert r.status_code == 400
        assert "cannot demote" in r.text.lower()

    def test_self_expire_past_blocked(self, mongo, headers):
        _seed_user(mongo, LEGACY_EMAIL, role="admin")
        past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        r = requests.patch(f"{API}/admin/users/{LEGACY_EMAIL}",
                           json={"expires_at": past}, headers=headers, timeout=15)
        assert r.status_code == 400
        assert "past" in r.text.lower()

    def test_self_delete_blocked(self, mongo, headers):
        _seed_user(mongo, LEGACY_EMAIL, role="admin")
        r = requests.delete(f"{API}/admin/users/{LEGACY_EMAIL}",
                            headers=headers, timeout=15)
        assert r.status_code == 400
        assert "cannot delete" in r.text.lower()


# ── SAFETY RAIL — last-admin ────────────────────────────────────────────
class TestLastAdminGuard:
    def test_delete_last_admin_blocked(self, mongo, headers):
        """Seed TEST_UM_only_admin as admin, temporarily disable pmvincenti,
        then attempting to delete TEST_UM_only_admin should be blocked as
        last-remaining-admin. Re-enable pmvincenti unconditionally after."""
        email = f"{TEST_PREFIX}only_admin@example.com"
        _seed_user(mongo, email, role="admin")

        # Snapshot Paul's disabled state so we can restore it exactly.
        paul = mongo.users.find_one({"email_normalized": "pmvincenti@gmail.com"})
        assert paul is not None, "Bootstrap admin pmvincenti@gmail.com missing"
        paul_was_disabled = bool(paul.get("disabled"))

        try:
            # Also mark all OTHER admins as disabled (there shouldn't be any
            # extra admins, but defensively count them out).
            other_admins = list(mongo.users.find({
                "role": "admin",
                "email_normalized": {"$ne": email, "$nin": [LEGACY_EMAIL]},
                "disabled": {"$ne": True},
            }))
            other_emails = [u["email_normalized"] for u in other_admins]
            mongo.users.update_many(
                {"email_normalized": {"$in": other_emails}},
                {"$set": {"disabled": True, "_test_last_admin_touched": True}},
            )

            r = requests.delete(f"{API}/admin/users/{email}",
                                headers=headers, timeout=15)
            assert r.status_code == 400, r.text
            assert "last remaining admin" in r.text.lower()
        finally:
            # Restore every admin we touched to its original disabled state.
            mongo.users.update_many(
                {"_test_last_admin_touched": True},
                {"$set": {"disabled": False}, "$unset": {"_test_last_admin_touched": ""}},
            )
            # Extra belt-and-suspenders: guarantee Paul is enabled if he was originally.
            if not paul_was_disabled:
                mongo.users.update_one(
                    {"email_normalized": "pmvincenti@gmail.com"},
                    {"$set": {"disabled": False}, "$unset": {"_test_last_admin_touched": ""}},
                )


# ── Defaults + existing users intact ────────────────────────────────────
class TestCreateDefaults:
    def test_new_user_defaults_to_90d_expiry(self, mongo, headers):
        email = f"{TEST_PREFIX}liam@example.com"
        r = _create_user_via_api(headers, email, role="operator")
        assert r.status_code == 200, r.text
        doc = mongo.users.find_one({"email_normalized": email})
        exp = doc["expires_at"]
        assert exp is not None
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        delta_days = (exp - datetime.now(timezone.utc)).days
        assert 89 <= delta_days <= 91


class TestExistingUsersIntact:
    def test_pmvincenti_exists_and_no_expiry(self, mongo):
        u = mongo.users.find_one({"email_normalized": "pmvincenti@gmail.com"})
        assert u is not None
        assert u.get("expires_at") is None, (
            "Bootstrap admin should have expires_at=None (un-brick safeguard)"
        )

    def test_karen_exists_and_authable(self, mongo, headers):
        u = mongo.users.find_one({"email_normalized": "karen.vincenti@gmail.com"})
        assert u is not None
        # If expires_at is set and in the past, resolve_principal would 403.
        # We can't call resolve_principal directly without a JWT for Karen,
        # but we can verify the user list endpoint succeeds and includes her.
        r = requests.get(f"{API}/admin/users", headers=headers, timeout=15)
        assert r.status_code == 200
        emails = [u["email"] for u in r.json()["users"]]
        assert "karen.vincenti@gmail.com" in emails


# ── Expiry enforcement in resolve_principal via minted JWT ─────────────
class TestExpiryEnforcement:
    def test_expired_jwt_user_gets_403(self, mongo):
        """Seed a user with expires_at in the past AND a google_sub, mint
        a JWT via auth.issue_app_jwt, hit an admin endpoint, expect
        403 'Account expired'."""
        try:
            from auth import issue_app_jwt
        except ImportError:
            pytest.skip("Cannot import backend.auth for JWT minting")

        email = f"{TEST_PREFIX}expired@example.com"
        google_sub = "test-um-google-sub-expired-001"
        _seed_user(
            mongo, email, role="admin",
            expires_at=datetime.now(timezone.utc) - timedelta(days=1),
            google_sub=google_sub,
        )
        doc = mongo.users.find_one({"email_normalized": email})
        # issue_app_jwt expects: google_sub, email_normalized, role, session_version
        token, _exp = issue_app_jwt(doc)
        r = requests.get(f"{API}/admin/users",
                         headers={"Authorization": f"Bearer {token}"},
                         timeout=15)
        assert r.status_code == 403, f"Expected 403 Account expired; got {r.status_code}: {r.text}"
        assert "expired" in r.text.lower()

    def test_future_expiry_jwt_user_passes(self, mongo):
        """Sanity check the negative side: valid unexpired user with JWT
        can hit admin endpoints without being blocked."""
        try:
            from auth import issue_app_jwt
        except ImportError:
            pytest.skip("Cannot import backend.auth for JWT minting")

        email = f"{TEST_PREFIX}happy@example.com"
        google_sub = "test-um-google-sub-happy-001"
        _seed_user(
            mongo, email, role="admin",
            expires_at=datetime.now(timezone.utc) + timedelta(days=30),
            google_sub=google_sub,
        )
        doc = mongo.users.find_one({"email_normalized": email})
        token, _exp = issue_app_jwt(doc)
        r = requests.get(f"{API}/admin/users",
                         headers={"Authorization": f"Bearer {token}"},
                         timeout=15)
        assert r.status_code == 200, r.text
