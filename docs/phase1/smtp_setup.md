# SMTP setup for Phase 2 sending (Google Workspace)

## What this enables

When `vars.OUTGROW_WRITE_MODE=true`, the daily-dispatch workflow swaps
`--dry-run` for `--write`. The orchestrator's `_smtp_sender_from_env`
reads four env vars and uses `smtplib.SMTP` + STARTTLS to deliver each
rep's pulse to their own inbox. The rep then texts the customer
manually — the engine never sends SMS and never emails customers.

The recipient allowlist (`config/recipient_allowlist.yaml`) is enforced
at runtime, so even if a bug substituted a customer email into the To
header, the send would refuse with `RuntimeError: recipient ... is not
in the recipient allowlist`. Belt-and-suspenders with the cron-level
`OUTGROW_PAUSED` kill-switch.

## One-time setup

### 1. Mint a Google App Password

App passwords let an external tool authenticate as a Workspace mailbox
without storing the user's actual password and without prompting for
2FA. They only work if 2FA is enabled on the mailbox.

1. Sign in to the **sending** mailbox at `myaccount.google.com` —
   typically `outgrow-control@getlivewire.com` (or whichever mailbox
   you want the pulses to come **From**).
2. Go to [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords).
   If the page 404s, enable 2-Step Verification first under Security.
3. Click **Create**. Name it `outgrow-dispatch` (or similar — the name
   is for your records, not for SMTP).
4. Google shows a 16-character password with spaces. **Copy it without
   the spaces** — that's the SMTP password.

### 2. Add the GH Actions secrets + repo variables

Settings → Secrets and variables → Actions.

**Secrets tab** (encrypted, never echoed):
* `OUTGROW_SMTP_USER` — the sending mailbox address, e.g.
  `outgrow-control@getlivewire.com`
* `OUTGROW_SMTP_PASS` — the 16-character app password from step 1
  (no spaces)

**Variables tab** (visible in workflow logs, can be safely committed
in spirit):
* `OUTGROW_SMTP_HOST` — `smtp.gmail.com` (or leave unset; that's the
  default the orchestrator falls back to)
* `OUTGROW_SMTP_PORT` — `587` (or leave unset; that's the default)
* `OUTGROW_WRITE_MODE` — **leave absent for now**. You'll flip it to
  `true` in step 3 once the credentials work.

### 3. Smoke-test with a manual dispatch

While `OUTGROW_WRITE_MODE` is still absent, the workflow stays in
`--dry-run` mode. To smoke-test the SMTP path **before** flipping the
cron loose:

1. Set `vars.OUTGROW_WRITE_MODE=true`.
2. Trigger the workflow manually from the Actions tab.
3. Check the rep inboxes (`henry@getlivewire.com`,
   `zack@getlivewire.com`) for the pulse email.
4. If it arrives correctly, you're done — the next 07:00 ET cron will
   send for real.
5. If it fails, the run log will surface `smtplib.SMTPAuthenticationError`
   (bad password), `smtplib.SMTPRecipientsRefused` (relay rejected the
   To address), or `OSError` (host/port wrong). Fix the secret and
   re-dispatch.

### 4. Kill switches (in order of escalation)

If anything goes sideways:

1. Set `vars.OUTGROW_WRITE_MODE=false` (or remove the variable) — next
   run goes back to dry-run.
2. Set `vars.OUTGROW_PAUSED=true` — next run exits before any work.
3. Revoke the app password at
   [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords)
   — SMTP login immediately starts returning auth errors.

## Switching providers later

If Livewire moves off Google Workspace, the same four env vars carry
over — change `OUTGROW_SMTP_HOST` / `OUTGROW_SMTP_PORT` to the new
provider's relay (Zoho Mail: `smtp.zoho.com:587`, Microsoft 365:
`smtp.office365.com:587`, etc.) and swap the user/pass secrets. The
STARTTLS pattern in `make_smtp_sender` works for every major hosted
relay; only implicit-TLS-only relays (port 465) would need a code
change to use `smtplib.SMTP_SSL`.

## What happens on the first real send

Each rep gets one email per weekday at 07:00 ET. Subject:
`Outgrow pulse — <Customer Name>`. Body contains the briefing
(LTV, last purchase, partial-since-2017 disclosure for legacy
customers), the AI-drafted suggested text, and four signed
`mailto:` action links the rep taps to log what they did.

Zack's pulses CC `OUTGROW_OWNER_EMAIL` (henry) for the first
`cc_review_remaining` sends — set in `config/reps/zack.yaml`. Drop
that counter to 0 once you're comfortable with Zack's voice
calibration.
