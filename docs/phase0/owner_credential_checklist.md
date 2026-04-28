# Phase 0 credential checklist (owner)

Work down this list once. Every box turns into a single GitHub Actions
secret or repo variable; each section names exactly which one.

> **Hard rule.** No customer data, no live API call, and no production
> write happens until the relevant box below is checked AND the matching
> dry-run from `intake/` or `voice/` succeeds.

---

## 1. Zoho CRM — production read-only

- [ ] Log in at <https://accounts.zoho.com> as an admin.
- [ ] Zoho Developer Console → Add Client → "Self Client".
- [ ] Generate a code with scopes:
      `ZohoCRM.modules.contacts.READ`, `ZohoCRM.modules.deals.READ`.
      **Activity write scope is NOT minted yet** — that lands in Phase 2.
- [ ] Exchange the code for a refresh token; copy it.
- [ ] GitHub repo → Settings → Secrets and variables → Actions:
      add secret `ZOHO_CRM_REFRESH_TOKEN`, plus `ZOHO_CRM_CLIENT_ID`,
      `ZOHO_CRM_CLIENT_SECRET`, `ZOHO_CRM_DC` (e.g. `us`, `eu`).

## 2. Zoho CRM sandbox (Phase 2 prep, do not skip)

- [ ] Confirm a Zoho CRM sandbox company exists (Settings → Sandbox).
- [ ] Repeat the Self Client + scope flow targeting the sandbox.
- [ ] Add `ZOHO_CRM_SANDBOX_REFRESH_TOKEN` (parked until Phase 2 day 1).

## 3. Zoho Desk — read

- [ ] Zoho Desk → Setup → API → Create Connection. Scope:
      `Desk.tickets.READ`.
- [ ] Add `ZOHO_DESK_REFRESH_TOKEN`, `ZOHO_DESK_DEPARTMENT_ID`.

## 4. QuickBooks Online — sandbox first

- [ ] Sign in at <https://developer.intuit.com> with the Intuit
      developer account that owns Livewire's app.
- [ ] Connect the app to the **sandbox company**, not production.
- [ ] Add `QBO_CLIENT_ID`, `QBO_CLIENT_SECRET`,
      `QBO_SANDBOX_REFRESH_TOKEN`, `QBO_SANDBOX_REALM_ID`.
- [ ] Production tokens (`QBO_PROD_REFRESH_TOKEN`, `QBO_PROD_REALM_ID`)
      land **only after** the Phase 0 match audit shows ≥98% precision
      on tier-1+2 matches.

## 5. D-Tools Cloud — read

- [ ] D-Tools admin → API access → generate a personal access token.
- [ ] Confirm the token can read projects + project items for both
      Owner and Zack.
- [ ] Add `DTOOLS_API_TOKEN`, `DTOOLS_ACCOUNT_ID`.

## 6. Anthropic API

- [ ] Anthropic console → API keys → create a project key for `outgrow`.
- [ ] Set monthly budget alert at 2× expected pilot spend.
- [ ] Add `ANTHROPIC_API_KEY`.

## 7. Outbound email (SMTP) — pulse delivery

- [ ] Pick the sending domain. Cheapest reliable options: Postmark,
      Resend, Amazon SES.
- [ ] Verify sender + DKIM/SPF on the domain.
- [ ] Add `OUTGROW_SMTP_HOST`, `OUTGROW_SMTP_USER`,
      `OUTGROW_SMTP_PASSWORD`, `OUTGROW_SMTP_FROM`.

## 8. Inbound control inbox (IMAP) — kill switch + action links

- [ ] Provision `outgrow-control@livewire.com` (or the equivalent
      internal-only address).
- [ ] Enable IMAP access; mint an app-specific password.
- [ ] Add `OUTGROW_IMAP_HOST`, `OUTGROW_IMAP_USER`,
      `OUTGROW_IMAP_PASSWORD`, `OUTGROW_CONTROL_INBOX`.

## 9. HMAC signing secret (mailto action links)

- [ ] Generate a 32-byte secret: `python -c "import secrets;
      print(secrets.token_hex(32))"`.
- [ ] Save in 1Password under "Outgrow → HMAC".
- [ ] Add `OUTGROW_HMAC_SECRET`. **Never write this value into a
      committed file.** `.gitignore` and the `forbid-hmac-secret`
      pre-commit hook are belt-and-suspenders.

## 10. Recipient allowlist + repo variables

- [ ] Update `config/recipient_allowlist.yaml` with the real internal
      pulse-recipient addresses (owner + Zack + any CC).
- [ ] Add repo variable `OUTGROW_PAUSED=true` (kept ON during Phase 0;
      flipped to `false` only after Phase 1 dry-runs are green).
- [ ] Add repo variable `OUTGROW_TIMEZONE=America/New_York`.
