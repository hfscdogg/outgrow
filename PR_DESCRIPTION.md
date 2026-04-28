# Phase 0 scaffold

Lays down the repo skeleton for the Outgrow daily-pulse engine: tooling,
CI, config stubs, three Phase 0 dispatch tools (match audit, rep
triangulation, voice-profile bootstrap), a draft auto-rejector, two CI
guard scripts, and 50+ tests. Every external integration raises
`NotImplementedError` until Phase 1 credentials land. No live API
call, no customer data, no production write happens from this branch.

The full design is in `docs/plan.md` (already on `main`).

## What's in the scaffold

### Tooling
- `pyproject.toml` — Python 3.11+ runtime; CI matrix pins 3.12. Ruff,
  pyright, pytest, pre-commit.
- `.gitignore` excludes `.cache/`, `*.env`, `hmac_secret.txt`,
  `__pycache__`, `.venv`.
- `.editorconfig` — standard Python settings.
- `.pre-commit-config.yaml` — ruff + pyright + a `forbid-hmac-secret`
  hook that refuses any commit containing a file named
  `hmac_secret.txt`.
- `README.md` — operator quickstart.

### GitHub Actions
- `ci.yml` — lint + format + pyright + pytest + recipient-allowlist
  guard + write-scope guard. Runs on push and PR.
- `daily_dispatch.yml` — 07:00 ET cron, **commented out**.
- `kill_switch_poller.yml` — 5-min cron, **commented out**.
- `eod_digest.yml` — 17:00 ET cron, **commented out**.
- All scheduled jobs are gated by
  `if: ${{ vars.OUTGROW_PAUSED != 'true' }}` so manual dispatch is
  also disabled while the repo variable is set.

### Config stubs (placeholders only)
- `config/recipient_allowlist.yaml` — internal-only pulse recipients.
- `config/plays.yaml` — Daily Proactive, Reverse DYK, Referral with
  default `base_weight` and **neutral** seasonal weights (1.0 every
  month — tune from real data after the pilot).
- `config/dyk_matrix.yaml` — schema + LLM prompt scaffold.

### Phase 0 dispatch tools (no live API calls)
- `intake/match_audit.py` — Zoho ↔ QBO 3-tier scorer (email exact,
  phone E.164 exact, name+address fuzzy via rapidfuzz). Auto-accepts
  ≥0.85; emits a CSV of **ambiguous rows only**.
- `intake/rep_triangulation.py` — Zoho-owner vs QBO-rep vs D-Tools-PM
  three-way comparator; emits CSV of **disagreements only**.
- `voice/wizard.py` — reads `.cache/voice/<rep>.csv` (gitignored),
  ranks by signal density, presents top 40, distills tone patterns
  to `config/reps/<rep>.yaml`. **Raw customer texts never enter git.**
- `drafting/sample_judge.py` — heuristic auto-rejector for LLM-drift
  phrases, hallucinated dollar amounts, off-profile greetings, emoji
  over budget, length out of band.

### CI guard scripts
- `scripts/check_recipient_allowlist.py` — fails CI if any
  `config/reps/*.yaml` declares an email not on the allowlist.
- `scripts/check_write_scopes.py` — AST-based; fails CI if `qbo.py`
  calls any non-GET HTTP verb, or if `zoho_crm.py` calls any non-GET
  outside the Zoho `Activities` endpoint.

### Tests (~50, all passing locally)
- `tests/conftest.py` blocks `socket.socket` and
  `socket.create_connection` at import time so any accidental
  network call fails loudly with `NetworkBlocked`.
- Coverage: fuzzy scorer, rep triangulation, voice density formula,
  voice distillation, sample-judge heuristics, recipient-allowlist
  guard, write-scope guard, NotImplementedError contracts on every
  connector.

### Phase 0 docs (operator runbook)
- `docs/phase0/voice_sample_email.md` — forward-to-Zack template.
- `docs/phase0/owner_credential_checklist.md` — 10-section, click-path
  credential setup.
- `docs/phase0/consent_memo.md` — single-signature TCPA basis memo.
- `docs/phase0/zoho_config_questionnaire.md` — 10-min config Q&A.

## Hard rules enforced

- **No live API calls.** `intake/`, `voice/`, `drafting/` connectors
  raise `NotImplementedError`. Tests assert on this.
- **No customer data.** Every test fixture uses `<example.com>`
  addresses and synthetic names.
- **HMAC secret never in git.** `.gitignore` excludes
  `hmac_secret.txt`; pre-commit refuses any path matching that name.
- **Workflows paused.** Cron triggers commented out; jobs guarded by
  `vars.OUTGROW_PAUSED != 'true'`.

## How to verify locally

```bash
uv venv && source .venv/bin/activate
uv pip install -e ".[dev]"
ruff check . && ruff format --check .
pyright
python scripts/check_recipient_allowlist.py
python scripts/check_write_scopes.py
pytest -ra
```

## Test plan (PR reviewer)

- [ ] `ci.yml` passes on this PR.
- [ ] `tests/conftest.py` actually blocks sockets — temporarily add
      `socket.socket(socket.AF_INET, socket.SOCK_STREAM)` to any
      test and confirm it fails with `NetworkBlocked`.
- [ ] `scripts/check_write_scopes.py` flags a deliberate
      `requests.post('/v3/customer')` in a fake `qbo.py`.
- [ ] `scripts/check_recipient_allowlist.py` flags a rep YAML whose
      email is missing from `recipient_allowlist.yaml`.
- [ ] No file in the PR contains a real customer name, real phone
      number, real email, or any HMAC secret.
- [ ] `docs/plan.md` (already on `main`) is unchanged.

## Out of scope for this PR

- Any live integration. All connectors are intentionally
  `NotImplementedError`.
- `config/reps/<rep>.yaml` voice profiles. Those are produced by the
  wizard from rep-marked favorites during Phase 0 dispatch and
  committed in a follow-up PR per rep.
- `config/dyk_matrix.yaml` rules. Owner authors during Phase 2.
- Production credentials. See
  `docs/phase0/owner_credential_checklist.md` for the click-path.
