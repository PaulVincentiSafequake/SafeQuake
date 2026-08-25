"""Per-user Google sign-in (task #9) and user management.

Extracted from server.py on 2026-06-18 — behaviour unchanged. Replaces the
shared X-Admin-Token secret with per-user identities: the dashboard gets a
Google-signed ID token, we verify it and issue a short-lived HS256 JWT, and
every admin call re-checks allowed/disabled/session_version so disabling an
operator invalidates their token immediately rather than at expiry.

Safety rails that must not be softened (see memory/PRD.md task #9):
  * an admin cannot self-demote, self-expire, or self-delete
  * the last USABLE admin cannot be deleted or demoted
  * the bootstrap admin never expires — it is the un-brick account
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from auth import (
    AuthError,
    decode_app_jwt,
    issue_app_jwt,
    require_role,
    resolve_principal,
    verify_google_id_token,
)
from deps import ADMIN_TRIGGER_PASSWORD, db

import logging

logger = logging.getLogger(__name__)

router = APIRouter()
api_router = router   # endpoints below keep their original decorators verbatim


# ═════════════════════════════════════════════════════════════════════════
# Per-user Google sign-in (task #9)
# ═════════════════════════════════════════════════════════════════════════

class GoogleLoginPayload(BaseModel):
    """Request body for POST /api/auth/google.

    id_token: the JWT Google Identity Services returns to the browser after
    a successful sign-in. We verify it server-side (signature, audience,
    issuer, expiry, email_verified) via the google-auth library, then check
    the resulting email against our own users collection.
    """
    id_token: str = Field(..., min_length=20, max_length=4096)


class UserCreatePayload(BaseModel):
    email: str = Field(..., min_length=3, max_length=200)
    role: str = Field(..., pattern=r"^(admin|operator)$")
    display_name: Optional[str] = Field(default=None, max_length=80)


def _user_public(u: dict) -> dict:
    """Shape a users doc for API responses. Never returns fields that
    would be sensitive if the response were misrouted (session_version,
    google_sub, timestamps of the LAST login, are all fine — no secrets)."""
    exp = u.get("expires_at")
    now = datetime.now(timezone.utc)
    # A user is "expired" iff expires_at is set AND in the past. A null
    # expires_at means "never expires" (used for the primary admin).
    is_expired = False
    if exp is not None:
        exp_aware = exp if getattr(exp, "tzinfo", None) else exp.replace(tzinfo=timezone.utc) if exp else None
        is_expired = exp_aware is not None and exp_aware < now
    return {
        "email": u.get("email_normalized"),
        "display_name": u.get("display_name") or u.get("email_normalized"),
        "role": u.get("role"),
        "allowed": bool(u.get("allowed")),
        "disabled": bool(u.get("disabled")),
        "google_linked": bool(u.get("google_sub")),
        "last_login_at": u.get("last_login_at"),
        "created_at": u.get("created_at"),
        "created_by": u.get("created_by"),
        "disabled_at": u.get("disabled_at"),
        "disabled_by": u.get("disabled_by"),
        "expires_at": u.get("expires_at"),
        "is_expired": is_expired,
    }


# Default account lifetime — 90 days. Locked with Paul 2026-08-06.
# Rationale: an operator who hasn't been onboarded to the current
# dispatch protocol for 90 days shouldn't retain access without an
# admin re-confirmation. Set to None on the doc to mean "never expires"
# (used for the primary admin so a bad expiry policy can never lock out
# the last admin).
DEFAULT_ACCOUNT_LIFETIME_DAYS = 90


@api_router.post("/auth/google")
async def auth_google(payload: GoogleLoginPayload):
    """Exchange a Google-signed ID token for our own dashboard JWT.

    Flow:
      1. Verify the Google ID token (signature, aud, iss, exp, email_verified).
      2. Look up the email in our users allowlist.
      3. Reject if not allowlisted, or disabled, or explicitly not allowed.
      4. First-successful-sign-in for an allowlisted email links the account
         to Google's stable `sub` identifier so future logins are matched
         on `sub` (email can change; sub cannot).
      5. Issue our own 15-min JWT and return it.

    Never accepts an email as authoritative from the client — only from
    Google's signed token.
    """
    info = verify_google_id_token(payload.id_token)
    google_sub = info["sub"]
    email = str(info.get("email") or "").strip()
    if not email:
        raise AuthError("Google token missing email", status_code=400)
    email_norm = email.lower()

    # Match by google_sub first (stable), then fall back to email for the
    # first sign-in of a pre-created allowlist entry.
    user = await db.users.find_one({"google_sub": google_sub})
    if not user:
        user = await db.users.find_one({"email_normalized": email_norm})

    if not user or not user.get("allowed") or user.get("disabled"):
        # Log the denial so an admin can see failed sign-in attempts, but
        # don't reveal WHY (existence-of-account vs disabled) — same
        # 403 for both to avoid enumeration.
        logger.info("Sign-in denied for %s (google_sub=%s...)", email_norm, google_sub[:8])
        raise AuthError("This Google account is not authorized for the dashboard", status_code=403)

    # Link/refresh: bind sub if this is the first login, record last_login_at.
    now = datetime.now(timezone.utc)
    updates: dict = {
        "last_login_at": now,
        "last_login_email": email,           # so we see the display-form email in logs
    }
    if not user.get("google_sub"):
        updates["google_sub"] = google_sub
        updates["google_linked_at"] = now
    if user.get("display_name") is None and info.get("name"):
        updates["display_name"] = info["name"][:80]
    await db.users.update_one({"_id": user["_id"]}, {"$set": updates})

    # Re-fetch so the issued JWT reflects the freshest fields (in
    # particular the newly-set google_sub on first login).
    user = await db.users.find_one({"_id": user["_id"]})
    token, exp = issue_app_jwt(user)
    return {
        "token": token,
        "expires_at": exp.isoformat(),
        "user": _user_public(user),
    }


@api_router.post("/auth/refresh")
async def auth_refresh(request: Request):
    """Renew a still-valid dashboard session (#135).

    Paul, 2026-08-24: "It signed me out in the middle of a task."

    The dashboard calls this while the operator is working, before the
    current token expires. It exists so the CLOCK never signs anybody out
    while they are using the board — only a real reason does: account
    disabled, role changed, session revoked, account expired, or the
    absolute session cap for the shift reached.

    Deliberate properties:
      * An ALREADY-EXPIRED token cannot be renewed. Renewal is for an
        active session, not a resurrection.
      * `auth_iat` (the moment the human signed in with Google) is carried
        through unchanged, so renewal slides the expiry forward but can
        never push it past the absolute cap.
      * Every check `resolve_principal` performs on a normal admin request
        is performed here too — allowlist, disabled, session_version,
        account expiry. Renewal is not a way around revocation.
      * The legacy shared token has no session to renew and is refused.
    """
    auth_header = request.headers.get("authorization", "")
    if not auth_header.lower().startswith("bearer "):
        raise AuthError("No session to renew", status_code=401)
    claims = decode_app_jwt(auth_header.split(" ", 1)[1].strip())

    # Same checks as any other authenticated call — disable / demote /
    # revoke / expiry all take effect here.
    principal = await resolve_principal(
        request, request.headers.get("x-admin-token"), ADMIN_TRIGGER_PASSWORD, db,
    )
    if principal.get("is_legacy"):
        raise AuthError("Legacy shared token has no session to renew", status_code=400)

    user = await db.users.find_one({"google_sub": claims["sub"]})
    if not user:
        raise AuthError("Access revoked", status_code=403)
    token, exp = issue_app_jwt(user, auth_iat=claims.get("auth_iat") or claims.get("iat"))
    return {
        "token": token,
        "expires_at": exp.isoformat(),
        "user": _user_public(user),
    }


@api_router.get("/auth/me")
async def auth_me(request: Request):
    """Return the current caller's identity + role.

    Used by the dashboard on load to (a) decide whether to show a sign-in
    button or the operator UI, and (b) refresh cached role info so
    role-gated buttons update without a full re-login.
    """
    principal = await resolve_principal(
        request,
        request.headers.get("x-admin-token"),
        ADMIN_TRIGGER_PASSWORD,
        db,
    )
    # For real users, resolve to the users doc so we can return created_at etc.
    if not principal.get("is_legacy"):
        u = await db.users.find_one({"email_normalized": principal["email"]})
        if u:
            return {"authenticated": True, "user": _user_public(u), "is_legacy": False}
    return {
        "authenticated": True,
        "user": {
            "email": principal["email"],
            "display_name": principal["display_name"],
            "role": principal["role"],
        },
        "is_legacy": principal.get("is_legacy", False),
    }


@api_router.post("/auth/logout")
async def auth_logout(request: Request):
    """Client-side logout — nothing to invalidate server-side unless we
    bump session_version. Returns 200 unconditionally so the client can
    always clear its stored token.

    Rationale: a Bearer JWT logout is inherently client-side (the client
    forgets the token). For hard-revocation, an admin should disable the
    user OR the user should hit /api/auth/revoke-me (below) which bumps
    their session_version and invalidates every issued JWT for them.
    """
    return {"ok": True}


@api_router.post("/auth/revoke-me")
async def auth_revoke_me(request: Request):
    """Bump the caller's session_version, invalidating every issued JWT
    for them. Use when you suspect your device was compromised.
    """
    principal = await resolve_principal(
        request,
        request.headers.get("x-admin-token"),
        ADMIN_TRIGGER_PASSWORD,
        db,
    )
    if principal.get("is_legacy"):
        raise AuthError("Legacy principal cannot self-revoke", status_code=400)
    await db.users.update_one(
        {"email_normalized": principal["email"]},
        {"$inc": {"session_version": 1}},
    )
    return {"ok": True}


# ── User management (admin-only) ────────────────────────────────────────
@api_router.get("/admin/users")
async def list_users(request: Request):
    principal = await resolve_principal(
        request,
        request.headers.get("x-admin-token"),
        ADMIN_TRIGGER_PASSWORD,
        db,
    )
    require_role(principal, "admin")
    users = await db.users.find({}, {"_id": 0}).sort("created_at", 1).to_list(200)
    return {"count": len(users), "users": [_user_public(u) for u in users]}


@api_router.post("/admin/users")
async def create_user(payload: UserCreatePayload, request: Request):
    """Add a new user to the allowlist. They can sign in from that moment
    on with their Google account — no invite email, no temp password."""
    principal = await resolve_principal(
        request,
        request.headers.get("x-admin-token"),
        ADMIN_TRIGGER_PASSWORD,
        db,
    )
    require_role(principal, "admin")

    email_norm = payload.email.strip().lower()
    if "@" not in email_norm or " " in email_norm:
        raise HTTPException(400, "Invalid email")
    # Prevent creation of a user whose email collides with the sentinel
    # principal used by the legacy X-Admin-Token path. A real user with
    # that email would let self-* guards misattribute legacy-token
    # actions as "self", producing subtly wrong lockout behavior.
    if email_norm == "legacy@dashboard":
        raise HTTPException(400, "Reserved email address")

    existing = await db.users.find_one({"email_normalized": email_norm})
    if existing:
        raise HTTPException(409, f"User {email_norm} already exists")

    doc = {
        "email": payload.email.strip(),
        "email_normalized": email_norm,
        "display_name": (payload.display_name or email_norm.split("@", 1)[0]).strip(),
        "role": payload.role,
        "allowed": True,
        "disabled": False,
        "session_version": 1,
        "google_sub": None,
        "created_at": datetime.now(timezone.utc),
        "created_by": principal["email"],
        # Default account expiry: 90 days from creation. Admins can override
        # via PATCH or POST /extend after creation. The primary/bootstrap
        # admin doc has this set to null so their access never expires.
        "expires_at": datetime.now(timezone.utc) + timedelta(days=DEFAULT_ACCOUNT_LIFETIME_DAYS),
    }
    await db.users.insert_one(doc)
    return {"ok": True, "user": _user_public(doc)}


@api_router.post("/admin/users/{email}/disable")
async def disable_user(email: str, request: Request):
    """Disable a user AND bump session_version so their in-flight JWTs
    are immediately invalidated. Cannot disable yourself (safety rail
    against accidental self-lockout by the last admin)."""
    principal = await resolve_principal(
        request,
        request.headers.get("x-admin-token"),
        ADMIN_TRIGGER_PASSWORD,
        db,
    )
    require_role(principal, "admin")
    email_norm = email.strip().lower()
    if email_norm == principal["email"]:
        raise HTTPException(400, "Cannot disable your own account")
    res = await db.users.update_one(
        {"email_normalized": email_norm},
        {
            "$set": {
                "disabled": True,
                "disabled_at": datetime.now(timezone.utc),
                "disabled_by": principal["email"],
            },
            "$inc": {"session_version": 1},
        },
    )
    if res.matched_count == 0:
        raise HTTPException(404, f"User {email_norm} not found")
    return {"ok": True}


@api_router.post("/admin/users/{email}/enable")
async def enable_user(email: str, request: Request):
    principal = await resolve_principal(
        request,
        request.headers.get("x-admin-token"),
        ADMIN_TRIGGER_PASSWORD,
        db,
    )
    require_role(principal, "admin")
    email_norm = email.strip().lower()
    res = await db.users.update_one(
        {"email_normalized": email_norm},
        {
            "$set": {"disabled": False},
            "$unset": {"disabled_at": "", "disabled_by": ""},
        },
    )
    if res.matched_count == 0:
        raise HTTPException(404, f"User {email_norm} not found")
    return {"ok": True}


# ---------- User update / expiry / delete (Task 4: User Management dashboard) ----

class UserPatchBody(BaseModel):
    """Partial-update payload for PATCH /admin/users/{email}.

    All fields optional; only supplied ones are applied. Empty patch is a
    no-op (returns 400 to prevent accidental empty PATCHes that look like
    a UI bug at the caller).
    """
    role: Optional[str] = Field(None, description="'admin' or 'operator'")
    # expires_at accepts ISO8601 string OR the sentinel "never" (which
    # sets the field to null on the doc). Mobile-app JSON serialization
    # is quirky about nullable datetimes, so this two-mode API is safer
    # than trying to distinguish 'null' from 'field absent'.
    expires_at: Optional[str] = Field(
        None,
        description="ISO 8601 datetime, or the string 'never' to remove expiry.",
    )


@api_router.patch("/admin/users/{email}")
async def patch_user(email: str, body: UserPatchBody, request: Request):
    """Update role and/or expiry on an existing user.

    Safety rails (all enforced server-side, not just in the dashboard):
      - Admin cannot demote their own account (would lock themselves out
        of user management).
      - Admin cannot set their own expiry into the past.
      - Session version is bumped on role change so any active JWTs for
        the updated user are invalidated within one request cycle (they
        get a 401 on next call and re-authenticate — new JWT reflects
        new role).

    Deliberately does NOT allow email or display_name changes here —
    those would require a Google-sub re-link. Delete + re-add if needed.
    """
    principal = await resolve_principal(
        request,
        request.headers.get("x-admin-token"),
        ADMIN_TRIGGER_PASSWORD,
        db,
    )
    require_role(principal, "admin")

    email_norm = email.strip().lower()
    target = await db.users.find_one({"email_normalized": email_norm})
    if not target:
        raise HTTPException(404, f"User {email_norm} not found")

    is_self = email_norm == (principal.get("email") or "").lower()

    set_fields: dict = {}
    bump_session = False

    if body.role is not None:
        if body.role not in {"admin", "operator"}:
            raise HTTPException(400, "role must be 'admin' or 'operator'")
        if is_self and body.role != "admin":
            # Self-demotion lockout guard.
            raise HTTPException(400, "Cannot demote your own admin account")
        if body.role != target.get("role"):
            set_fields["role"] = body.role
            bump_session = True

    if body.expires_at is not None:
        raw = body.expires_at.strip()
        if raw.lower() == "never":
            set_fields["expires_at"] = None
        else:
            # Parse ISO 8601. Accept both 'Z' and '+00:00' suffixes.
            try:
                new_exp = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                if new_exp.tzinfo is None:
                    new_exp = new_exp.replace(tzinfo=timezone.utc)
            except ValueError:
                raise HTTPException(400, f"expires_at is not a valid ISO 8601 datetime: {raw!r}")
            if is_self and new_exp <= datetime.now(timezone.utc):
                # Self-expire lockout guard. Using <= (not <) so setting
                # expiry to exactly-now still counts as "self-brick" —
                # otherwise the guard passes and the account expires
                # one tick later, defeating the purpose.
                raise HTTPException(400, "Cannot set your own expiry to a past date")
            set_fields["expires_at"] = new_exp

    if not set_fields:
        raise HTTPException(400, "No fields to update")

    update: dict = {"$set": set_fields}
    if bump_session:
        update["$inc"] = {"session_version": 1}

    await db.users.update_one({"email_normalized": email_norm}, update)
    doc = await db.users.find_one({"email_normalized": email_norm})
    return {"ok": True, "user": _user_public(doc)}


@api_router.post("/admin/users/{email}/extend")
async def extend_user_expiry(email: str, request: Request):
    """One-click 'extend by 90 days from now' action.

    Convenience wrapper around PATCH — the dashboard's most common
    account-renewal flow ("Karen's account expires next week, extend
    it another 90 days") shouldn't require the admin to compute an
    ISO date string.

    Sets expires_at = now + 90 days regardless of the previous value.
    Deliberately not "now + 90 from previous expiry" because that would
    let admins accumulate arbitrarily long lifetimes by clicking
    Extend a few times.
    """
    principal = await resolve_principal(
        request,
        request.headers.get("x-admin-token"),
        ADMIN_TRIGGER_PASSWORD,
        db,
    )
    require_role(principal, "admin")

    email_norm = email.strip().lower()
    new_exp = datetime.now(timezone.utc) + timedelta(days=DEFAULT_ACCOUNT_LIFETIME_DAYS)
    res = await db.users.update_one(
        {"email_normalized": email_norm},
        {"$set": {"expires_at": new_exp, "extended_at": datetime.now(timezone.utc), "extended_by": principal["email"]}},
    )
    if res.matched_count == 0:
        raise HTTPException(404, f"User {email_norm} not found")
    doc = await db.users.find_one({"email_normalized": email_norm})
    return {"ok": True, "user": _user_public(doc)}


@api_router.delete("/admin/users/{email}")
async def delete_user(email: str, request: Request):
    """Permanently remove a user.

    In most cases, DISABLE is safer than DELETE — a disabled account
    keeps its audit trail (which rows they triggered, which they
    rescued) intact. Delete only when a user was created by mistake
    or must be scrubbed for GDPR/data-subject-request reasons.

    Safety rails:
      - Cannot delete yourself.
      - Cannot delete the LAST admin account (would leave nobody
        able to manage users going forward — recovery would require
        direct Mongo access).
    """
    principal = await resolve_principal(
        request,
        request.headers.get("x-admin-token"),
        ADMIN_TRIGGER_PASSWORD,
        db,
    )
    require_role(principal, "admin")

    email_norm = email.strip().lower()
    if email_norm == (principal.get("email") or "").lower():
        raise HTTPException(400, "Cannot delete your own account")

    target = await db.users.find_one({"email_normalized": email_norm})
    if not target:
        raise HTTPException(404, f"User {email_norm} not found")

    # Last-admin guard. Exclude disabled AND expired admins from the
    # count — an expired admin can't sign in either, so they can't
    # replace the one being deleted.
    if target.get("role") == "admin":
        now = datetime.now(timezone.utc)
        remaining_admins = await db.users.count_documents({
            "role": "admin",
            "email_normalized": {"$ne": email_norm},
            "disabled": {"$ne": True},
            # `expires_at` null OR in the future
            "$or": [
                {"expires_at": None},
                {"expires_at": {"$gt": now}},
            ],
        })
        if remaining_admins == 0:
            raise HTTPException(400, "Cannot delete the last remaining admin")

    await db.users.delete_one({"email_normalized": email_norm})
    return {"ok": True, "deleted": email_norm}


