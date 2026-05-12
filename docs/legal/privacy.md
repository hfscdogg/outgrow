# Outgrow — Privacy Policy

**Effective date:** May 12, 2026
**Software:** Outgrow daily-pulse engine
**Operator:** Livewire (the "Company")
**Contact:** henry@getlivewire.com

This document describes how the Outgrow daily-pulse engine ("the
Software") handles data. The Software is an internal sales-prompting
tool used solely by Livewire's own employees on Livewire's own
customer data. It is not provided to or used by any third party.

## What data the Software accesses

The Software reads, but does not modify, the following:

### From Zoho CRM (read-only API access)

- Contact records: full name, email address, phone number, mailing
  address, contact owner (which Livewire sales rep), and creation
  timestamp.
- Deal records: deal name, contact reference, stage, amount, and
  closing date.

### From QuickBooks Online (read-only API access)

- Customer records: display name, primary email and phone (when
  present), billing address, and creation timestamp.
- Invoice records: customer reference, total amount, transaction date,
  and document number.

### From Livewire-internal configuration

- Per-rep voice features (preferred greetings, sign-offs, typical
  sentence length, distinctive vocabulary) authored by each rep
  themselves.
- Rep email addresses, used solely to deliver each rep's own daily
  pulse to their inbox.

## What data the Software transmits to third parties

A pre-pulse customer briefing — comprising the chosen customer's
name, lifetime spend summary, last-purchase month, and pre-sanitized
notes — is sent to **Anthropic's Claude API** (Anthropic, PBC, United
States) for the sole purpose of generating a draft text suggestion in
the rep's voice. Raw Zoho or QuickBooks records are not sent to
Anthropic; only the briefing fields enumerated here are transmitted.

Anthropic's data handling for these API requests follows their
published policy at https://www.anthropic.com/legal/privacy. Livewire
does not opt these requests into Anthropic's training-data programs.

No data is transmitted to any other third party.

## Where data is processed

- GitHub Actions ephemeral runners (United States) for the daily
  workflow execution. Files written to a runner's filesystem are
  destroyed when the run completes.
- Anthropic's API endpoint (United States) for draft generation
  requests only.
- Each rep's own inbox (the rep's existing email provider, as
  managed by Livewire) for delivery of their daily pulse email.

## Data retention

- The Software itself stores no persistent data. Each daily run
  rebuilds its working state from scratch from the Zoho CRM and
  QuickBooks Online APIs.
- GitHub Actions retains workflow logs for 90 days per its default
  retention policy. Logs may include rep email addresses, customer
  names appearing in drafted text, and dollar amounts referenced in
  briefings, but do not include raw Zoho or QuickBooks records.
- Anthropic's retention period for API requests is governed by their
  policy at https://www.anthropic.com/legal/privacy.

## Who has access

Only Livewire personnel with permissioned access to Livewire's GitHub
repository may invoke or read the output of the Software. No external
party has access to the Software, its inputs, or its outputs.

## TCPA / SMS consent

The Software does not send SMS messages. It surfaces a suggested text
to a Livewire sales rep, who chooses whether to send that text from
their own phone to a customer with whom they have a pre-existing
business relationship. The rep is the sender of record on every SMS
the recipient receives.

## Changes to this policy

Material changes to this policy will be communicated to Livewire
employees by the Company.

## Contact

Questions: henry@getlivewire.com.
