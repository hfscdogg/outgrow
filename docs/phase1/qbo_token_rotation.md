# QBO refresh-token rotation runbook

## Problem

Intuit returns a new `refresh_token` on every successful OAuth refresh and
invalidates the old one after a ~24h grace period. If the engine doesn't
write the rotated token back, the GitHub Actions secret
`QBO_SANDBOX_REFRESH_TOKEN` goes stale within a day or two — the next
`Sync — QBO sandbox` step then fails with HTTP 400
`invalid_grant: Incorrect or invalid refresh token`.

## Solution

`sync/qbo.py`'s `maybe_persist_refresh_token` writes the rotated token
back to the GitHub Actions secret via the GitHub REST API after every
successful refresh. The PUT body is libsodium sealed-box encrypted with
the repo's public key per [GitHub's docs](https://docs.github.com/en/rest/actions/secrets#create-or-update-a-repository-secret).

Persistence is **best-effort**: if the API call fails, the current sync
still completes (the access token from the same refresh already worked).
The next run will surface staleness via 400 `invalid_grant` on its own
refresh, the same way it would have without persistence.

## Setup (one-time per repo)

1. **Mint a fine-grained PAT** at
   [github.com/settings/personal-access-tokens/new](https://github.com/settings/personal-access-tokens/new):
   * **Resource owner**: your account / org owning the outgrow repo
   * **Repository access**: *Only select repositories* → pick `outgrow`
   * **Permissions** (Repository):
     * `Secrets: Read and write`
     * `Metadata: Read-only` (always required for fine-grained PATs)
   * **Expiration**: 90 days. Set a calendar reminder to rotate; GitHub
     fine-grained PATs cap at 1 year and the security model assumes
     periodic rotation.
2. **Add it as a repo secret**: Settings → Secrets and variables →
   Actions → Secrets tab → New repository secret →
   `GH_PAT_ROTATE_SECRETS` = the PAT value.
3. The `daily-dispatch` workflow's QBO step already passes
   `GH_PAT_ROTATE_SECRETS` through as an env var; no workflow edits
   needed after the persistence PR merges.

## What the code does on each run

```
1. read QBO_SANDBOX_REFRESH_TOKEN (current secret value)
2. POST oauth.platform.intuit.com/.../tokens/bearer with that token
3. response: { access_token, refresh_token: <NEW or unchanged> }
4. if response refresh_token differs from secret value:
     a. GET api.github.com/repos/<repo>/actions/secrets/public-key
     b. encrypt new token with libsodium sealed box
     c. PUT api.github.com/repos/<repo>/actions/secrets/QBO_SANDBOX_REFRESH_TOKEN
     d. on success: log info, continue
     e. on failure: log error, continue (don't crash the sync)
5. continue with QBO data sync using the new access token
```

## Why a PAT and not a GitHub App

A GitHub App is the production-grade alternative — no human-owned PAT to
expire, finer-grained per-installation permissions, audit trail. The
tradeoff is implementation complexity (JWT signing, app installation
flow, token minting on every run). A fine-grained PAT scoped to a single
repo with a single permission is the right granularity for the Phase 1
pilot. Reconsider when:

* A second secret needs the same rotation pattern (e.g., Phase 2's Zoho
  Activity write token)
* The pilot expands beyond Livewire's repo
* The PAT's first 90-day rotation feels burdensome

## What to do when this breaks

* **Run logs `Failed to persist rotated QBO token`**: the PAT expired or
  was revoked. Re-mint per the Setup section, update the secret. Manually
  re-mint the QBO refresh token too (the sync's last access token is now
  expired and persistence didn't catch the rotated refresh).
* **Run logs `persistence skipped: GH_PAT_ROTATE_SECRETS or GITHUB_REPOSITORY not set`**:
  expected when running locally or via `workflow_dispatch` outside a
  proper Actions run. The QBO sync still succeeds, but the secret will go
  stale within ~24h.
* **HTTP 400 `invalid_grant` resurfaces despite this PR**: race condition
  (two near-simultaneous workflow runs both refreshed) or PAT lost write
  access. Re-mint the QBO refresh token via the Intuit OAuth Playground
  and update the secret manually.

## Race-condition note

Two workflow runs firing within seconds of each other (e.g., daily cron
+ manual `workflow_dispatch`) can both refresh in parallel. The first
rotates the token; the second's refresh sees the old token, which is
still valid for ~24h, and rotates it again — leaving the persistence
race-prone. With Phase 1's once-daily cron + occasional manual dispatch,
the risk is acceptable; if it bites, add a workflow-level concurrency
group (`concurrency: { group: qbo-sync, cancel-in-progress: false }`).
