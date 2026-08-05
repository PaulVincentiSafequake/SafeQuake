# Subscription Lapse — Copy Doc (v1, awaiting red-line)

Every string below is legal-evidence copy. Never uses "subscription", "payment",
or "billing". Always "cover" + explicit safety consequence. Consequences **bolded**.
One-tap "Reactivate now" CTA on every warning. Only ⚠️ emoji, on terminal state.

Timezone rule: all dates + times shown in the USER'S LOCAL TIMEZONE derived
from their device, not UTC and not a relative "tomorrow".

---

## Tier 30 — 30 days before cover ends
### Push notification title
Your Quake Angel cover ends in 30 days
### Push body
After [DATE, local], **you will not be alerted to earthquakes near you, and rescuers will not see you if you are trapped.** Tap to reactivate.
### In-app banner
Your **Quake Angel cover ends in 30 days** (on [DATE, local]). After that, **you will not be alerted to earthquakes near you, and rescuers will not see you if you are trapped.** Reactivate to keep cover continuous.
### In-app banner CTAs
[ Reactivate now ]    [ I understand ]
### Email subject
Quake Angel cover ends in 30 days
### Email body opening
Your Quake Angel cover ends on **[DATE, local]** — 30 days from today.

After that date, **you will not be alerted to earthquakes near you, and rescuers on the emergency dashboard will not see you if you are trapped.**

Reactivate now to keep your cover continuous: [ Reactivate ]

---

## Tier 14 — 14 days before cover ends
(same structure, "in 14 days" / "in 2 weeks")

## Tier 7 — 7 days before cover ends
(same structure, "in 7 days" / "in 1 week")

## Tier 1 — day before cover ends
### Push notification title
Your Quake Angel cover ends tomorrow
### Push body
Cover ends **[EXACT DATE + TIME, local]**. After that, **you will not be alerted, and rescuers will not see you if you are trapped.** One tap to reactivate.
### In-app banner
Your **Quake Angel cover ends [EXACT DATE + TIME, local]** — under 24 hours from now. After that, **you will not be alerted to earthquakes near you, and rescuers will not see you if you are trapped.** [ Reactivate now ]

---

## Grace period (post-expiry, functionality still on)
### Daily in-app banner
Your cover **expired on [DATE, local]**. You have **[N] days of grace period** remaining before all protection features stop. **Reactivate now to avoid interruption.**  [ Reactivate ]
### Daily push notification (once per day during grace)
Your Quake Angel cover expired [DATE, local]. **[N] days of grace remaining.** Reactivate now.

---

## NOT PROTECTED — cover ended, grace exhausted
### Full-width red screen on every launch (dismissible with acknowledgement)
⚠️ **NOT PROTECTED**

Your cover **expired on [DATE, local]**. You will **not be alerted to earthquakes near you**, and **rescuers will not see you if you are trapped**.
[ Reactivate now ]  (primary, large)
[ Continue without cover ]  (secondary, requires 3-second hold, records acknowledgement)

### Persistent chip on every screen after acknowledgement
⚠️ NOT PROTECTED — tap to reactivate
(Red, top edge, all screens, replaces the "SYSTEM ACTIVE · MONITORING" pill on home)

---

## Reactivation success (post-payment)
### In-app confirmation
✅ Cover reactivated. Your Quake Angel cover is now current through [NEW_END_DATE, local].
### Push (silent — no banner, just clears the NOT PROTECTED state on next foreground)
(no user-facing push — the state change speaks for itself)

---

## Payment retry failure (Apple billing grace elapsed, no user action)
### Push
We couldn't renew your Quake Angel cover — your payment method needs attention. **You are still covered for now**, but if this isn't resolved cover will end soon. Tap to update.
(Note: this is the ONLY warning that mentions "payment" — because Apple's system has already tried and failed and there's no cover-consequence phrasing that's accurate. Falls under Apple's own billing grace period, before our grace begins.)

---

## Fields to red-line before hardcoding
- [ ] Exact word/framing on "cover" — is that the right noun, or would "protection" / "monitoring" read better?
- [ ] "Rescuers will not see you if you are trapped" — accurate today (dashboard reads /api/devices which lists everyone regardless of subscription, but that changes when we gate); confirm it's the phrasing you want.
- [ ] 3-second hold duration for "Continue without cover" — is 3s the right friction? Longer? Add a visual countdown ring?
- [ ] Payment-retry copy — is the softening ("we couldn't renew") acceptable given it's Apple's flow, not our neglect?
- [ ] Any per-country legal-language obligations (Malta consumer-protection statute) I should be aware of?
