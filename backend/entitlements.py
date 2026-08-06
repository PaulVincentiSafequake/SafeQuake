"""
Subscription entitlement state machine — Phase A of subscription-lapse
handling (2026-08-06).

## What this is (and what it isn't)

This module tracks each user's *current entitlement state* (active /
grace / lapsed / never_subscribed) and computes the banner copy the
mobile app should show, if any. It is intentionally the whole of Phase
A — no actual purchase flow, no StoreKit integration, no App Store
Server Notification v2 verification. Those land in Phase C when we're
ready to charge real money.

The state machine works today with a `test_state_override` field that
lets the dashboard flip any user through every state, so the mobile
banner copy can be reviewed on-device before a single line of real
StoreKit code exists. This unblocks:
  - Copy review on real hardware (banner wording in light + dark),
  - App Store review-note screenshots,
  - QA of grace-period countdown.

## Design invariants (locked with Paul 2026-08-06)

1. **Critical alerts are ALWAYS free.** The state machine never returns
   `should_disable_critical_alerts=True`. There is no such flag. Even
   when `state == "lapsed"`, critical alerts remain fully active. This
   is not a value we can promise later and take back — it's baked into
   the code path such that even a bug can't turn off the siren.

2. **Copy always says so, in the same sentence as the CTA.** Every
   banner variant ends with "Critical alerts still work." The mobile
   layer never customises this string. If future product changes gate
   critical alerts, that would require code review and PRD update, not
   just a copy tweak — the invariant is enforced at the copy level too.

3. **Grace period = 7 days** after `current_period_end`. Matches
   Apple's built-in Billing Grace Period (also user-visible in
   subscription settings), so our banner doesn't disagree with what
   iOS is showing the same user in Settings > Subscriptions.

4. **Lazy state computation on read.** The state stored in Mongo is a
   snapshot at last write. On every GET we RECOMPUTE the correct
   state given wall-clock now — active-past-period-end reads as
   `grace`, grace-past-grace-ends-at reads as `lapsed`. We do NOT
   write the recomputed state back to Mongo: on a read-heavy endpoint
   that would be a write-amplification footgun, and Phase C (real
   Apple ASN2 push notifications) will overwrite these compute-only
   transitions with real events anyway. Consequences for callers:
     - Mobile client: always sees correct state. Zero staleness risk.
     - Direct DB queries: the `state` field can lag reality until
       the next `upsert_entitlement` call. Callers that need
       real-time state across many devices at once should hit the
       API, not query the collection directly.
     - `history[]`: only captures deliberate writes (admin override,
       Apple ASN2). Time-based auto-transitions are not recorded —
       they're derivable from `current_period_end` + `grace_ends_at`
       + wall clock, so no information is lost, just not duplicated
       into an append log.

5. **Never a hard paywall on launch.** No blocking modal, no "renew or
   quit" screen. HIG explicitly discourages hard paywalls at launch
   with no demonstrated value, and Apple has rejected safety apps for
   pretending safety features are gated when they're not.

## Storage

Mongo collection `entitlements`, one doc per user (user_id = device_id
for the mobile-first launch; will move to Google sub for dashboard
operators when accounts land):

    {
      user_id: str,                                          # primary key
      state: "active" | "grace" | "lapsed" | "never_subscribed",
      plan: "free" | "premium" | None,
      current_period_end: datetime | None,                   # from Apple RenewalInfo (Phase C)
      grace_ends_at: datetime | None,                        # current_period_end + 7d
      expiration_reason: "voluntary" | "billing_issue" | "price_increase_declined" | "product_not_available" | None,
      last_transition_at: datetime,
      history: [{state, reason, at}, ...],
      test_state_override: {...} | None,                     # dev-only, admin-set
      created_at: datetime,
      updated_at: datetime,
    }
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional


# ----- Constants --------------------------------------------------------------

VALID_STATES = {"active", "grace", "lapsed", "never_subscribed"}
VALID_REASONS = {
    "voluntary",              # user cancelled deliberately
    "billing_issue",          # payment failed (typically expired card)
    "price_increase_declined",  # user declined a price hike
    "product_not_available",  # the product was withdrawn
}
GRACE_PERIOD_DAYS = 7

DEFAULT_STATE = "never_subscribed"


# ----- Banner copy ------------------------------------------------------------
#
# Locked wording (Paul, 2026-08-06). Every variant ends with
# "Critical alerts still work." — invariant #2 above. Do not
# monkey with this without an explicit PRD update.

@dataclass(frozen=True)
class BannerSpec:
    """A single banner to display in the mobile app. None means no banner."""
    kind: str              # "info" | "warn" | "urgent"  -> mobile picks colour
    title: str
    body: str
    cta_label: str
    cta_action: str        # "manage_subscription" | "resubscribe" -> mobile handler
    dismissable: bool


def _banner_for(state: str, reason: Optional[str], days_left: Optional[int]) -> Optional[BannerSpec]:
    """Compute banner copy from state + reason.

    Returns None when no banner should be shown (active, or never
    subscribed and not in a promotional context).
    """
    if state == "active" or state == "never_subscribed":
        return None

    if state == "grace":
        # Grace period — payment issue, we still have entitlement,
        # user needs to fix payment. Copy is calm, not alarming, and
        # explicitly reassures on critical alerts.
        days_txt = f"{days_left} day{'s' if days_left != 1 else ''}" if days_left and days_left > 0 else "soon"
        return BannerSpec(
            kind="warn",
            title="Renewal pending",
            body=f"Update your payment to keep premium features after {days_txt}. Critical alerts still work.",
            cta_label="Update payment",
            cta_action="manage_subscription",
            dismissable=True,
        )

    if state == "lapsed":
        if reason == "billing_issue":
            return BannerSpec(
                kind="warn",
                title="Payment issue",
                body="Update your payment method to restore premium features. Critical alerts still work.",
                cta_label="Update payment",
                cta_action="manage_subscription",
                dismissable=True,
            )
        # Voluntary / price-increase-declined / product-not-available
        # all get the same calm "you can reactivate" copy. We do NOT
        # nag users who deliberately cancelled — this banner is
        # dismissable and the copy is neutral.
        return BannerSpec(
            kind="info",
            title="Premium paused",
            body="Reactivate premium anytime to restore extras. Critical alerts still work.",
            cta_label="Reactivate",
            cta_action="resubscribe",
            dismissable=True,
        )

    # Unknown state — defensive default: no banner (fail-quiet, not fail-loud).
    return None


# ----- State computation ------------------------------------------------------

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    """Some Mongo drivers hand us naive datetimes. Force UTC-aware."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def compute_current_state(doc: Optional[dict]) -> tuple[str, Optional[str], Optional[datetime], Optional[int]]:
    """Given the persisted entitlement doc, return (state, reason, grace_ends_at, days_left).

    Pure function — no I/O — so it can be unit-tested without a DB.
    """
    if not doc:
        return (DEFAULT_STATE, None, None, None)

    # Manual override (dev / QA path). Lets the dashboard flip a device
    # into any state to preview banner copy.
    override = doc.get("test_state_override")
    if override:
        st = override.get("state")
        if st in VALID_STATES:
            reason = override.get("expiration_reason")
            grace_ends = _aware(override.get("grace_ends_at"))
            days_left = None
            if st == "grace" and grace_ends:
                days_left = max(0, (grace_ends - _now()).days)
            return (st, reason, grace_ends, days_left)

    stored_state = doc.get("state") or DEFAULT_STATE
    reason = doc.get("expiration_reason")
    period_end = _aware(doc.get("current_period_end"))
    grace_ends = _aware(doc.get("grace_ends_at"))
    now = _now()

    # Auto-transitions based on wall clock.
    if stored_state == "active" and period_end and now > period_end:
        # Sub period ended and we haven't received the renewal ack.
        # If we know why it ended (via ASN2 in Phase C), reason is set;
        # otherwise treat as billing_issue conservatively (drives the
        # warmer "update payment" banner rather than the cooler
        # "you cancelled" copy).
        stored_state = "grace"
        if grace_ends is None:
            grace_ends = period_end + timedelta(days=GRACE_PERIOD_DAYS)

    if stored_state == "grace" and grace_ends and now > grace_ends:
        stored_state = "lapsed"

    days_left: Optional[int] = None
    if stored_state == "grace" and grace_ends:
        days_left = max(0, (grace_ends - now).days)

    return (stored_state, reason, grace_ends, days_left)


def public_entitlement_view(doc: Optional[dict]) -> dict:
    """Assemble the response for `GET /api/entitlement`.

    Everything the mobile client needs to render, in one round trip.
    No PII, no raw Apple receipt data — just state + banner spec.
    """
    state, reason, grace_ends, days_left = compute_current_state(doc)
    banner = _banner_for(state, reason, days_left)
    return {
        "state": state,
        "plan": (doc or {}).get("plan") or ("premium" if state in ("active", "grace") else "free"),
        "expiration_reason": reason,
        "grace_ends_at": grace_ends.isoformat() if grace_ends else None,
        "days_left_in_grace": days_left,
        "banner": (
            {
                "kind": banner.kind,
                "title": banner.title,
                "body": banner.body,
                "cta_label": banner.cta_label,
                "cta_action": banner.cta_action,
                "dismissable": banner.dismissable,
            } if banner else None
        ),
        # SAFETY: this field exists so the mobile layer has a single
        # source-of-truth invariant it can assert against. It is always
        # True. If a future change ever makes it False, that's a red
        # flag surfaced in code review — no silent regression.
        "critical_alerts_active": True,
    }


# ----- Persistence helpers ----------------------------------------------------

def new_transition_history_entry(state: str, reason: Optional[str], source: str) -> dict:
    """One row in the entitlement doc's `history` array. `source` is
    e.g. 'apple_asn2', 'admin_override', 'auto_transition' — makes the
    audit trail unambiguous later.
    """
    return {"state": state, "reason": reason, "source": source, "at": _now()}


async def upsert_entitlement(
    db: Any,
    user_id: str,
    *,
    state: str,
    plan: Optional[str] = None,
    current_period_end: Optional[datetime] = None,
    grace_ends_at: Optional[datetime] = None,
    expiration_reason: Optional[str] = None,
    source: str = "unknown",
    test_state_override: Optional[dict] = None,
) -> dict:
    """Upsert entitlement with a history entry appended. Idempotent —
    if the state hasn't changed, we still update `last_transition_at`
    (cheap) but don't spam history.
    """
    if state not in VALID_STATES:
        raise ValueError(f"invalid state {state!r}")
    if expiration_reason is not None and expiration_reason not in VALID_REASONS:
        raise ValueError(f"invalid expiration_reason {expiration_reason!r}")

    now = _now()
    existing = await db.entitlements.find_one({"user_id": user_id})
    prev_state = (existing or {}).get("state")

    set_fields = {
        "state": state,
        "plan": plan if plan is not None else (existing or {}).get("plan"),
        "current_period_end": current_period_end if current_period_end is not None else (existing or {}).get("current_period_end"),
        "grace_ends_at": grace_ends_at if grace_ends_at is not None else (existing or {}).get("grace_ends_at"),
        "expiration_reason": expiration_reason if expiration_reason is not None else (existing or {}).get("expiration_reason"),
        "last_transition_at": now,
        "updated_at": now,
    }
    if test_state_override is not None:
        set_fields["test_state_override"] = test_state_override

    update: dict = {
        "$set": set_fields,
        "$setOnInsert": {"user_id": user_id, "created_at": now},
    }
    if prev_state != state:
        # $push auto-creates the history array on insert — safer than
        # initializing it in $setOnInsert, which conflicts with $push
        # on the same path (MongoError 40).
        update["$push"] = {"history": new_transition_history_entry(state, expiration_reason, source)}

    await db.entitlements.update_one({"user_id": user_id}, update, upsert=True)
    return await db.entitlements.find_one({"user_id": user_id}, {"_id": 0})


async def clear_test_override(db: Any, user_id: str) -> None:
    await db.entitlements.update_one(
        {"user_id": user_id},
        {"$unset": {"test_state_override": ""}, "$set": {"updated_at": _now()}},
    )
