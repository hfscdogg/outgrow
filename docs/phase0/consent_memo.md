# Outgrow daily-pulse — consent + TCPA basis

**Owner of record:** Henry Frazer Sinclair Clifford  **Date:** April 28, 2026  **Signature:** ____________________

---

Outgrow is a habit-prompter. The engine sends one email per weekday to
each pilot rep with a suggested customer to reach out to and a draft
text written in the rep's voice. **The rep sends the text from their
own phone, from their own number, to a customer with whom they have
an existing paying-customer relationship.** The engine itself never
sends an SMS.

## Why this is a permitted contact

1. **Existing business relationship.** Every customer surfaced by the
   engine has a paid invoice in QuickBooks Online (typical lifetime
   spend $5K–$500K+, going back to 1994). The engine's eligibility
   filter requires at least one completed install before a customer
   can be surfaced for any play.

2. **Rep-initiated, rep-sent.** The text leaves the rep's personal
   business number. The engine never originates a message and never
   has access to the rep's SMS account. From a TCPA standpoint, this
   is a one-to-one personal text from a salesperson to an existing
   customer they have a working relationship with — the same legal
   posture as a rep texting a customer unprompted.

3. **No marketing template, no automation pattern.** Drafts are
   generated per-customer with that customer's specific install,
   purchase, and ticket history. The rep edits or sends as-is at
   their discretion. The pulse contains a one-tap "skip today" path.

4. **Suppression for live disputes.** Any customer with an open Zoho
   Desk ticket is auto-suppressed. Any customer with an active deal
   is auto-suppressed (so the engine never interrupts a live sales
   thread). Reps can `PAUSE <customer>` from the control inbox to
   immediately add a customer to suppressions.

## Hard limits

- The engine will **not** send SMS, autodial, or originate any
  electronic message to a customer.
- The engine will **not** send to non-customers, prospects, or any
  contact that lacks a QuickBooks invoice history.
- The engine will **not** surface a customer who has been marked
  `do_not_contact` in Zoho.

## Out-of-scope counsel review

Owner may forward this memo to outside counsel before the pilot's
first send. The pilot's first send is gated on owner sign-off below.

---

I have read the engine's design (see `docs/plan.md`) and authorize the
30-day pilot to proceed with owner + Zack as the only pilot reps.

**Owner signature:** ____________________  **Date:** April 28, 2026
