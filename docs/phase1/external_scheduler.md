# External scheduler setup (cron-job.org → repository_dispatch)

## Why

GitHub Actions `schedule` (cron) events are best-effort — GitHub openly
documents that scheduled runs "may be dropped" under load. In practice
the daily-dispatch cron was dropped **every weekday of May 19–20 2026**,
including a three-cron spread across an hour. Adding more cron lines
didn't help; the whole `schedule` mechanism is the problem.

`repository_dispatch` is different: it fires the workflow via an
explicit `POST` to the GitHub API. GitHub processes that call
immediately and deterministically — there's no "best effort", no
dropping. An external scheduler makes that POST on a real cron.

The workflow keeps its `schedule:` block as a no-cost tertiary fallback
and `workflow_dispatch` as the manual backstop, but the external
trigger is the one to trust.

## One-time setup

### 1. Mint a fine-grained PAT for the dispatch call

[github.com/settings/personal-access-tokens/new](https://github.com/settings/personal-access-tokens/new):

* **Resource owner**: the account owning `outgrow`
* **Repository access**: *Only select repositories* → `outgrow`
* **Permissions** (Repository):
  * `Contents: Read and write` — the `POST /dispatches` endpoint
    requires Contents-write
  * `Metadata: Read-only` (always required for fine-grained PATs)
* **Expiration**: 90 days. Calendar-reminder to rotate.

Copy the token. This is separate from `GH_PAT_ROTATE_SECRETS` (which is
scoped to Secrets-write for QBO token rotation) — keep the scopes
minimal and distinct.

### 2. Create the cron-job.org job

1. Sign up free at [cron-job.org](https://cron-job.org) (no credit card).
2. **Create cronjob**:
   * **Title**: `Outgrow daily pulse`
   * **URL**: `https://api.github.com/repos/hfscdogg/outgrow/dispatches`
   * **Schedule**: every weekday (Mon–Fri) at **11:13 UTC**
     (= 07:13 ET during DST). cron-job.org lets you pick the timezone;
     if you set it to UTC, use 11:13. At the November DST flip, change
     to 12:13 UTC.
   * **Request method**: `POST`
   * **Request headers**:
     * `Authorization: Bearer <the PAT from step 1>`
     * `Accept: application/vnd.github+json`
     * `Content-Type: application/json`
   * **Request body**:
     ```json
     {"event_type": "daily-pulse"}
     ```
3. Save. cron-job.org will POST to the dispatch endpoint every weekday;
   GitHub fires the `daily-dispatch` workflow within seconds.

### 3. Verify

Trigger the cron-job.org job manually (most schedulers have a "run now"
button). Within a few seconds a new `daily-dispatch` run should appear
in the GitHub Actions tab with event `repository_dispatch`. If it
doesn't:

* **401 from GitHub** → PAT wrong/expired, or missing Contents-write.
* **404 from GitHub** → repo path typo, or PAT can't see the repo.
* **422 from GitHub** → body malformed; must be exactly
  `{"event_type": "daily-pulse"}`.

## What to do when this breaks

* **No pulse + no Actions run**: cron-job.org didn't fire or the POST
  failed. Check cron-job.org's execution history (it logs every
  request + response code). 401 → re-mint the PAT.
* **cron-job.org itself is down**: rare, but the `schedule:` fallback
  in the workflow still gets an occasional run, and you can always
  hit Actions → daily-dispatch → Run workflow by hand.
* **PAT expired**: dispatch POSTs start returning 401. Re-mint per
  step 1, update the header in cron-job.org.

## Why not Vercel Cron / Cloud Scheduler / a real server

All would work. cron-job.org is the lowest-friction option for a
single weekday ping — free, no infra, no deploy, no credit card. If
Outgrow later grows a web surface (Phase 3+ dashboard) and picks up a
Vercel or cloud deployment anyway, fold this trigger into that
platform's native cron at that point.
