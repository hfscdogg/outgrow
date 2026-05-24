# Gmail OAuth setup for the inbox poller

## Why

Phase 2's inbox poller reads action-reply emails from your `+outgrow`
Gmail label, verifies their HMAC tokens, and records what each rep
actually did with each suggested pulse (sent-as-is / edited / skipped /
reassigned). Closing that feedback loop turns the engine from a one-way
prompt into a measurable system.

Reading Gmail requires the Gmail API (not SMTP). We use the **OAuth
2.0 refresh-token flow** authenticating as a single human mailbox
(`outgrow-control@getlivewire.com` or henry's own mailbox — whichever
receives the `+outgrow` replies). One-time setup, then a long-lived
refresh token does its thing.

## One-time setup

### 1. Create the Google Cloud project + OAuth client

1. Open [console.cloud.google.com](https://console.cloud.google.com)
   signed in as the **Workspace administrator** for `getlivewire.com`
   (so the OAuth consent screen can be marked **Internal** — bypasses
   Google's verification process for restricted scopes).
2. Create a new project: name it `outgrow-inbox-poller` (or use an
   existing one).
3. Enable the Gmail API: **APIs & Services → Library → search "Gmail
   API" → Enable**.
4. **OAuth consent screen** (left sidebar):
   * User type: **Internal**
   * App name: `Outgrow inbox poller`
   * User support email: yours
   * Developer contact: yours
   * Scopes: add `https://www.googleapis.com/auth/gmail.modify`
     (covers reading messages + adding the "OutgrowProcessed" label
     so we don't re-process)
   * Save and continue.
5. **Credentials** (left sidebar) → **Create Credentials → OAuth
   client ID**:
   * Application type: **Desktop app**
   * Name: `outgrow-inbox-poller`
   * Create. Copy the **Client ID** and **Client secret** — save
     them somewhere; you'll need them in step 3.

### 2. Mint the refresh token (one-time)

Easiest path: **Google OAuth 2.0 Playground**.

1. Open [developers.google.com/oauthplayground](https://developers.google.com/oauthplayground).
2. Top-right gear icon → check **"Use your own OAuth credentials"** →
   paste the **Client ID** + **Client secret** from step 1.5 → close.
3. Left panel "Step 1": in the input box at the bottom, paste exactly:
   `https://www.googleapis.com/auth/gmail.modify` → click
   **Authorize APIs**.
4. Sign in as the **mailbox owner** (the account receiving the
   `+outgrow` replies — typically henry@getlivewire.com). Approve.
5. You're redirected back. Left panel "Step 2": click **Exchange
   authorization code for tokens**.
6. The response shows a **Refresh token** (starts with `1//`). **Copy
   it.** It's long-lived — losing it means re-doing this dance.

### 3. Add the four secrets to GitHub

Settings → Secrets and variables → Actions → **Secrets** tab → New
repository secret. Four entries:

* `OUTGROW_GMAIL_CLIENT_ID` — from step 1.5
* `OUTGROW_GMAIL_CLIENT_SECRET` — from step 1.5
* `OUTGROW_GMAIL_REFRESH_TOKEN` — from step 2.6
* `OUTGROW_GMAIL_USER` — the mailbox email (e.g. `henry@getlivewire.com`)

Plus one variable (Variables tab, not secret):

* `OUTGROW_INBOX_LABEL` — the Gmail label the poller reads from.
  Default `Outgrow` (matches the label you set up earlier). Override
  only if you renamed it.

### 4. Add the cron-job.org job for hourly polling

cron-job.org → Create cronjob:
* **Title**: `Outgrow inbox poll`
* **URL**: `https://api.github.com/repos/hfscdogg/outgrow/dispatches`
* **Schedule**: hourly, Mon–Fri 08:00–18:00 America/New_York (or
  whatever business-hours cadence you prefer; off-hours polling is
  wasted runs since reps tap action buttons during the day)
* **Method**: POST
* **Headers**: same as the daily-pulse job:
  * `Authorization: Bearer <your PAT>` (the same `outgrow-dispatch2`
    fine-grained PAT works for both — it already has Contents-write)
  * `Accept: application/vnd.github+json`
  * `Content-Type: application/json`
* **Body**: `{"event_type": "inbox-poll"}`

## Verifying

After the secrets are in place and the PR merges, hit cron-job.org's
"run now" for the new job. Within a few seconds an `inbox-poll`
workflow run should appear in Actions. The run logs should show
`processed N action(s)` for whatever's currently unread in the
`Outgrow` label.

Each processed Gmail message gets a new `OutgrowProcessed` label
auto-added — that's how the poller tracks idempotency. The label
shows up in Gmail too, so you can verify visually.

## Rotation

OAuth refresh tokens last indefinitely unless explicitly revoked or
unused for 6 months. No rotation cadence needed. If anything goes
sideways:

* **401 from Gmail** → refresh token was revoked or the OAuth client
  was deleted. Re-mint via the Playground (step 2).
* **403 insufficient scope** → the OAuth client wasn't granted
  `gmail.modify`. Re-do step 1.4 to add the scope, then re-mint.
* **No new actions processed but messages exist** → check the Gmail
  label name matches `OUTGROW_INBOX_LABEL` (case-sensitive).
