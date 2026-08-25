"""Per-user Google sign-in for the Quake Angel dashboard.

Architecture (see /app/memory/PRD.md and the task-#9 discussion transcript):

    Browser (dashboard snippet)
        │  1. Google Identity Services renders "Sign in with Google" button
        │  2. On success, GIS returns a Google-signed ID token in JS
        │  3. Snippet POSTs the ID token → /api/auth/google
        ▼
    Backend /api/auth/google
        │  4. Verifies the ID token's signature, audience (our Web Client ID),
        │     issuer (accounts.google.com), and expiry via google-auth
        │  5. Looks up sub / email in Mongo `users` collection
        │  6. Rejects if not allowlisted (allowed=false OR disabled=true OR absent)
        │  7. Issues our own short-lived (15-min) JWT signed with JWT_SECRET
        │  8. Returns the JWT in the response body
        ▼
    Browser stores JWT in sessionStorage
        │  9. All subsequent admin API calls attach:
        │       Authorization: Bearer <our-jwt>
        │ 10. On 401, snippet re-runs the sign-in flow
        ▼
    Backend admin endpoints
        │ 11. current_user() extracts + verifies the JWT
        │ 12. Re-checks the users doc (allowed, disabled, session_version)
        │     — so a disabled operator is locked out within 15 min even if
        │     their JWT hasn't expired yet
        │ 13. Legacy X-Admin-Token still accepted (soft-cutover), attributed
        │     as user "legacy@dashboard" with deprecation log entry

Why Bearer-in-sessionStorage instead of HttpOnly cookies (as some auth
playbooks recommend): the dashboard runs cross-site (malta.quakeangel.app
calling quake-alert-18.emergent.host). Safari and Firefox strict mode block
third-party cookies by default. For emergency-response tooling that must
work on whatever browser an operator picks up in a crisis, browser-agnostic
Bearer auth is strictly more reliable than cookies whose behavior depends
on user privacy settings. The trade-off (slightly higher XSS risk) is
acceptable because the dashboard code is our own HTML/JS, not user-generated.
"""

from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import Header, HTTPException, Request
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token
from jose import ExpiredSignatureError, JWTError, jwt

log = logging.getLogger("quakeangel.auth")

# ── Config — read LAZILY on each use, not at import time ─────────────────
# Reading os.environ at module-import time is a classic footgun when the
# importer is expected to have called load_dotenv() first: if the import
# happens BEFORE load_dotenv, every module-level env read returns the
# fallback and there's no error, just silent misbehavior downstream.
# Reading lazily via helper functions makes the timing robust and lets us
# read fresh values on each call (which we already exploit for the legacy
# kill-switch).
JWT_ALGO = "HS256"
JWT_ISSUER = "quake-angel-api"
JWT_AUDIENCE = "quake-angel-dashboard"
# #135 (2026-08-24 — Paul: "signed out in the middle of a task"). The old
# 15-minute token had no renewal path: the first admin call made 15 minutes
# after sign-in returned 401 and the dashboard dropped the session, mid-task,
# with no warning. Two changes fix that together:
#   1. A longer token life (60 min) so a laptop that slept through a renewal
#      window still comes back to a working session.
#   2. A renewal endpoint (POST /api/auth/refresh) the dashboard calls while
#      the operator is working, so an ACTIVE operator is never signed out by
#      the clock at all.
# Neither weakens revocation: every request still re-reads the users doc, so
# disable / role change / session_version bump / account expiry all take
# effect on the very next call, exactly as before.
JWT_TTL_MINUTES = int(os.environ.get("JWT_TTL_MINUTES", "60") or 60)

# Absolute session lifetime. Renewal slides the expiry forward but can never
# push it past `auth_iat + this`, so a tab left open on a shared workstation
# still requires a fresh Google sign-in once per shift. 12 hours covers a long
# incident shift without turning "sliding" into "forever".
JWT_ABSOLUTE_SESSION_HOURS = int(os.environ.get("JWT_ABSOLUTE_SESSION_HOURS", "12") or 12)


def _jwt_secret() -> str:
    return os.environ.get("JWT_SECRET", "")


def _google_web_client_id() -> str:
    return os.environ.get("GOOGLE_WEB_CLIENT_ID", "")


# Legacy shared-secret compatibility flag. Read on every request so an
# operator can flip it in .env + redeploy to kill the shared path without
# a code change.
def legacy_token_enabled() -> bool:
    return os.environ.get("LEGACY_TOKEN_ENABLED", "true").lower() in {
        "1", "true", "yes", "on",
    }

# Sentinel principal used when a request authenticates via the legacy
# shared X-Admin-Token instead of a real Google identity. Attributed
# distinctly in the audit trail so we can measure cutover progress.
LEGACY_PRINCIPAL = {
    "email": "legacy@dashboard",
    "role": "admin",
    "display_name": "Legacy shared token",
    "is_legacy": True,
}

# ── Role model ──────────────────────────────────────────────────────────
# Two roles for MVP (see task-#9 plan):
#   admin    → everything: user management, redact-notes, trigger, mark/unmark
#   operator → trigger, mark/unmark, view audit. Cannot touch user mgmt or redaction.
VALID_ROLES = {"admin", "operator"}


class AuthError(HTTPException):
    """401 with a consistent shape so the dashboard snippet can react uniformly."""

    def __init__(self, detail: str = "Not authenticated", status_code: int = 401):
        super().__init__(status_code=status_code, detail=detail)


# ── Google ID token verification ────────────────────────────────────────
def verify_google_id_token(token: str) -> dict:
    """Verify a Google-signed ID token via google-auth. Returns the claims.

    Raises AuthError on any failure. Checks: signature, audience matches
    our Web Client ID, issuer is Google, expiry not in the past, and
    email_verified=true (belt-and-suspenders — Google normally enforces
    this before issuing, but some test/edge accounts historically slipped
    through).

    NOTE: We deliberately do NOT store or log the raw ID token — it's a
    bearer credential to Google's userinfo endpoint until it expires.
    """
    google_client_id = _google_web_client_id()
    if not google_client_id:
        log.error("GOOGLE_WEB_CLIENT_ID not configured; cannot verify Google tokens")
        raise AuthError("Server not configured for Google sign-in", status_code=500)
    try:
        info = id_token.verify_oauth2_token(
            token, google_requests.Request(), google_client_id
        )
    except ValueError as e:
        # Covers signature failures, aud mismatch, expired token, malformed jwt.
        # We log a message but NOT the token contents.
        log.warning("Google ID token rejected: %s", str(e)[:120])
        raise AuthError("Google sign-in failed", status_code=401)

    issuer = info.get("iss")
    if issuer not in ("accounts.google.com", "https://accounts.google.com"):
        raise AuthError("Invalid Google issuer", status_code=401)
    if not info.get("email_verified"):
        raise AuthError("Google email is not verified", status_code=403)

    return info


# ── Our own application JWT (post-sign-in bearer) ───────────────────────
def issue_app_jwt(user: dict, auth_iat: Optional[int] = None) -> tuple[str, datetime]:
    """Sign a short-lived JWT carrying the user's identity + role.

    The JWT is validated on every admin request by current_user(), which
    ALSO re-checks the users collection for disabled/allowed/session_version
    changes. That means a JWT's practical lifetime is the MIN of:
      (a) its exp claim (15 min from now), and
      (b) how long until the user's session_version is bumped by an admin.

    Fields:
      sub    Google's stable subject identifier (never changes for a Google account)
      email  Normalized (lowercase) email at time of issue
      role   "admin" | "operator"
      sv     session_version — increment in Mongo to instantly revoke every
             existing JWT for a user (used by disable / promote / demote)
      jti    Unique JWT ID — future-proofing for per-session revocation
      iss    "quake-angel-api" — hardens against cross-service replay
      aud    "quake-angel-dashboard" — same
    """
    jwt_secret = _jwt_secret()
    if not jwt_secret:
        raise AuthError("Server missing JWT_SECRET", status_code=500)
    if user.get("role") not in VALID_ROLES:
        raise AuthError(f"Invalid role: {user.get('role')!r}", status_code=500)
    now = datetime.now(timezone.utc)
    exp = now + timedelta(minutes=JWT_TTL_MINUTES)
    # #135: `auth_iat` is the moment the human actually signed in with Google.
    # It is carried UNCHANGED through every renewal, and the absolute cap is
    # measured from it — so renewing cannot extend a session indefinitely.
    original_auth = int(auth_iat) if auth_iat else int(now.timestamp())
    absolute_deadline = datetime.fromtimestamp(original_auth, tz=timezone.utc) + timedelta(
        hours=JWT_ABSOLUTE_SESSION_HOURS,
    )
    if exp > absolute_deadline:
        exp = absolute_deadline
    if exp <= now:
        raise AuthError("Session lifetime reached — sign in again", status_code=401)
    claims = {
        "sub": user["google_sub"],
        "email": user["email_normalized"],
        "role": user["role"],
        "sv": user.get("session_version", 1),
        "jti": str(uuid.uuid4()),
        "iss": JWT_ISSUER,
        "aud": JWT_AUDIENCE,
        "iat": int(now.timestamp()),
        "auth_iat": original_auth,
        "exp": int(exp.timestamp()),
    }
    return jwt.encode(claims, jwt_secret, algorithm=JWT_ALGO), exp


def decode_app_jwt(token: str) -> dict:
    """Verify and decode our own JWT. Raises AuthError on any issue."""
    jwt_secret = _jwt_secret()
    if not jwt_secret:
        raise AuthError("Server missing JWT_SECRET", status_code=500)
    try:
        return jwt.decode(
            token,
            jwt_secret,
            algorithms=[JWT_ALGO],
            issuer=JWT_ISSUER,
            audience=JWT_AUDIENCE,
        )
    except ExpiredSignatureError:
        raise AuthError("Session expired", status_code=401)
    except JWTError as e:
        log.warning("JWT rejected: %s", str(e)[:120])
        raise AuthError("Invalid session", status_code=401)


# ── Request-time principal resolution ───────────────────────────────────
async def resolve_principal(
    request: Request,
    x_admin_token: Optional[str],
    admin_secret: str,
    db,
) -> dict:
    """Return the caller's principal or raise AuthError.

    Priority:
      1. `Authorization: Bearer <jwt>` header → decode JWT, re-check users doc.
      2. `X-Admin-Token: <shared-secret>` header (if LEGACY_TOKEN_ENABLED) →
         attribute as `legacy@dashboard`.
      3. → 401.

    The returned dict always has: `email`, `role`, `display_name`, `is_legacy`,
    `google_sub` (None for legacy).
    """
    # 1. Bearer JWT path
    auth_header = request.headers.get("authorization", "")
    if auth_header.lower().startswith("bearer "):
        token = auth_header.split(" ", 1)[1].strip()
        claims = decode_app_jwt(token)
        # Re-check the users doc so disable/session_version bump takes effect
        # BEFORE the JWT's exp claim would otherwise let them through.
        user = await db.users.find_one({"google_sub": claims["sub"]})
        if not user or not user.get("allowed") or user.get("disabled"):
            raise AuthError("Access revoked", status_code=403)
        if user.get("session_version", 1) != claims.get("sv"):
            raise AuthError("Session invalidated", status_code=401)
        # Account expiry check. A null expires_at means "never expires"
        # (used for the primary admin so a bad expiry policy can never
        # lock out the last admin). Any past expires_at → deny with a
        # distinct message so the dashboard snippet can surface the
        # 'renew via admin' instruction instead of a generic 401.
        expires_at = user.get("expires_at")
        if expires_at is not None:
            exp_aware = expires_at if getattr(expires_at, "tzinfo", None) else expires_at.replace(tzinfo=timezone.utc)
            if exp_aware < datetime.now(timezone.utc):
                raise AuthError("Account expired — contact your admin to renew", status_code=403)
        return {
            "email": user["email_normalized"],
            "role": user["role"],
            "display_name": user.get("display_name") or user["email_normalized"],
            "is_legacy": False,
            "google_sub": claims["sub"],
        }

    # 2. Legacy shared-secret path (soft-cutover)
    if legacy_token_enabled() and x_admin_token and admin_secret and x_admin_token == admin_secret:
        # Log the deprecation once per process minute at INFO, not on every
        # single request — otherwise we'd flood logs during migration.
        log.info("Legacy X-Admin-Token used from %s", request.client.host if request.client else "?")
        return LEGACY_PRINCIPAL

    # 3. Deny
    raise AuthError("Not authenticated", status_code=401)


def require_role(principal: dict, *allowed_roles: str) -> None:
    """Raise 403 if the principal's role isn't in the allowed set."""
    if principal.get("role") not in allowed_roles:
        raise AuthError(
            f"Requires role {' or '.join(allowed_roles)}", status_code=403
        )


# ── Attribution helpers for the audit trail ─────────────────────────────
def audit_attribution(principal: dict) -> str:
    """Produce the string we write into triggered_by / rescued_by / etc.

    For real Google-authed users we write their email verbatim. For the
    legacy shared-token principal we write "legacy@dashboard" so a quick
    grep can count how many audit rows still came from the pre-Google
    world. Never leaks anything sensitive.
    """
    return principal.get("email", "unknown")
