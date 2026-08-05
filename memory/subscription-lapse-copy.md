# Subscription Lapse — Copy Doc (v2, incorporating user red-lines 2026-08-05)

Every string is legal-evidence copy. Never uses "subscription", "payment",
or "billing" as the primary noun. Consequences **bolded**. One-tap
"Reactivate now" CTA on every warning. Only ⚠️ emoji, on terminal state.

**Consistency rule** (locked): the noun is "protection" throughout the app.
Never "cover" (insurance jargon, British-inflected — bad for a bilingual
market with heavy tourist/non-native-English usage). Never "monitoring"
(cold, technical, passive). The terminal state says NOT PROTECTED, so
warnings must say "your protection ends" — no vocabulary switch under stress.

**Accuracy rule** (locked): never state or imply that professional
responders are monitoring the dashboard until they contractually are.
Current phrasing "you will not appear on the emergency response dashboard"
is accurate today; "rescuers will not see you" is an upgrade to be applied
ONLY after a signed arrangement with Civil Protection or equivalent
responder organisation.

**Mirror rule** (locked): never tell a user they're unprotected when they
are. False alarms train people to ignore the warnings that count. Payment-
retry copy in particular must reflect that protection continues during
Apple's billing grace period.

Timezone rule: all dates + times shown in the USER'S LOCAL TIMEZONE from
their device, not UTC and not a relative "tomorrow".

Statutory-language rule: EU disclosure obligations (auto-renewal, 14-day
withdrawal, pre-contract info, easy cancel) sit with the seller. Leave
`[STATUTORY_TEXT_TBD]` placeholders where lawyer-drafted wording will go
rather than inventing it here.

---

## Tier 30 — 30 days before protection ends
### Push notification title
Your Quake Angel protection ends in 30 days
### Push body
After [DATE, local], **you will not be alerted to earthquakes near you, and you will not appear on the emergency response dashboard if you are trapped.** Tap to reactivate.
### In-app banner
Your **Quake Angel protection ends in 30 days** (on [DATE, local]). After that, **you will not be alerted to earthquakes near you, and you will not appear on the emergency response dashboard if you are trapped.** Reactivate to keep protection continuous.
### In-app banner CTAs
[ Reactivate now ]    [ I understand ]
### Email subject
Quake Angel protection ends in 30 days
### Email body opening
Your Quake Angel protection ends on **[DATE, local]** — 30 days from today.

After that date, **you will not be alerted to earthquakes near you, and you will not appear on the emergency response dashboard if you are trapped.**

Reactivate now to keep your protection continuous: [ Reactivate ]

---

## Tier 14 — 14 days before protection ends
(same structure, "in 14 days" / "in 2 weeks")

## Tier 7 — 7 days before protection ends
(same structure, "in 7 days" / "in 1 week")

## Tier 1 — day before protection ends
### Push notification title
Your Quake Angel protection ends tomorrow
### Push body
Protection ends **[EXACT DATE + TIME, local]**. After that, **you will not be alerted, and you will not appear on the emergency response dashboard if you are trapped.** One tap to reactivate.
### In-app banner
Your **Quake Angel protection ends [EXACT DATE + TIME, local]** — under 24 hours from now. After that, **you will not be alerted to earthquakes near you, and you will not appear on the emergency response dashboard if you are trapped.** [ Reactivate now ]

---

## Grace period (post-Apple-expiresDate, our 14-day window, functionality still on)
### Daily in-app banner
Your protection **expired on [DATE, local]**. You have **[N] days of grace period** remaining before all protection features stop. **Reactivate now to avoid interruption.** [ Reactivate ]
### Daily push notification (once per day during grace)
Your Quake Angel protection expired [DATE, local]. **[N] days of grace remaining.** Reactivate now.

---

## NOT PROTECTED — grace exhausted, protection genuinely gone
### Full-width red screen on every launch (dismissible with acknowledgement)
⚠️ **NOT PROTECTED**

Your protection **expired on [DATE, local]**. You will **not be alerted to earthquakes near you**, and **you will not appear on the emergency response dashboard if you are trapped**.

[ Reactivate now ]  ← primary, large

[ Continue without protection ]  ← secondary, requires 2-second hold WITH visible progress ring; VoiceOver/TalkBack users and users who tap without holding are routed to a two-step confirm dialog instead (see Accessibility rule below)

### Persistent chip on every screen after acknowledgement
⚠️ NOT PROTECTED — tap to reactivate
(Red, top edge, all screens, replaces the "SYSTEM ACTIVE · MONITORING" pill on home)

### Accessibility rule (locked)
Hold-to-confirm is a trap for elderly users, users with tremor, arthritis,
or motor impairment — precisely our highest-risk lapse cohort. The 2-second
hold is a shortcut for able users; the primary path for anyone who can't
hold (short tap, VoiceOver, TalkBack, Switch Control, reduced-motion pref)
is a standard two-step confirm dialog:

    "Continue without protection?"
    You will not be alerted to earthquakes near you, and you will not
    appear on the emergency response dashboard if you are trapped.
    [ Cancel ]  [ Yes, continue without protection ]

Both paths (hold + two-step) record identical acknowledgement events in
the audit trail. Detection: default to two-step for any assistive-tech
user; escalate short-tap-without-hold to two-step automatically.

---

## Reactivation success (post-StoreKit purchase)
### In-app confirmation
✅ Protection reactivated. Your Quake Angel protection is now current through [NEW_END_DATE, local].
### Push (silent)
(no user-facing push — the state change speaks for itself)

---

## Payment-retry state (Apple's billing grace, protection STILL ACTIVE)
Critical: during Apple's own billing retry / billing-grace-period, the
user IS still protected. Copy must reflect that accurately — see mirror rule.
### Push
There's a problem with your payment method. **Your protection continues until [EXACT DATE + TIME, local]** while this is retried. Update your payment details to avoid interruption.
### In-app banner (lower-priority yellow, not red)
There's a problem with your payment method. **Your protection continues until [EXACT DATE + TIME, local]**. [ Update payment details ]

This is the ONLY warning that mentions "payment" as a primary noun — because
Apple's system has already tried and there's no protection-consequence
phrasing that would be accurate. Never uses red. Never says NOT PROTECTED.

---

## Statutory disclosures (lawyer to draft)
### Pre-purchase (before user confirms StoreKit sheet)
[STATUTORY_TEXT_TBD — pre-contract information: price, renewal term, what happens on non-renewal, how to cancel. EU Consumer Rights Directive + Malta Consumer Affairs Act compliance.]
### Auto-renewal disclosure (in-app, in App Store listing metadata)
[STATUTORY_TEXT_TBD — auto-renewal terms, clear and prominent, before purchase.]
### 14-day right of withdrawal
[STATUTORY_TEXT_TBD — how to exercise withdrawal, deadline, refund mechanism.]
### Cancellation instructions
[STATUTORY_TEXT_TBD — one-tap link to iOS Manage Subscriptions sheet, no dark patterns.]

---

## Fields to red-line before hardcoding
- [ ] Every string above marked ready for developer to hard-code (once red-lined)
- [ ] Statutory placeholders filled in by lawyer (task: Legal/Terms)
- [ ] Once a responder-org agreement (e.g. Civil Protection) is signed, upgrade "you will not appear on the emergency response dashboard" → "rescuers will not see you if you are trapped" — but ONLY then
- [ ] Malta-specific consumer-protection statutory wording (lawyer)
- [ ] Review of the payment-retry banner colour (yellow vs orange) once designer weighs in
