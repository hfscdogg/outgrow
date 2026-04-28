# Outgrow

Daily-pulse engine that emails each rep one pre-drafted Outgrow pulse per
weekday morning. The rep sends the suggested text from their own phone and
clicks "I sent it" to log a Call Activity to Zoho. The engine **never** sends
SMS — it is a habit prompter, not a sender.

The full design is in [`docs/plan.md`](docs/plan.md).

## Status

Phase 0 scaffold. All integrations raise `NotImplementedError` until
credentials and live data flow are wired up in Phase 1.

## Quickstart (operator)

Requires Python 3.12 in production; Python 3.11+ works for local dev and CI.

```bash
# Bootstrap a venv with uv (fast) or pip
uv venv && source .venv/bin/activate
uv pip install -e ".[dev]"

# Run the test suite (no network, no live API calls)
pytest

# Lint + type-check
ruff check . && ruff format --check . && pyright

# Install pre-commit hooks (refuses to commit hmac_secret.txt)
pre-commit install
```

## Layout

```
.github/workflows/    # CI + scheduled jobs (paused by default)
config/               # YAML stubs: plays, allowlist, DYK matrix
docs/phase0/          # Operator runbook for Phase 0 (credentials, voice samples, consent)
intake/               # Zoho/QBO/D-Tools match audits (Phase 0 dispatch tools)
voice/                # Voice-profile bootstrap from rep history
drafting/             # Draft-quality auto-rejector
scripts/              # CI guard scripts
tests/                # 20+ tests, sockets blocked at import time
```

## Hard rules

- No live API calls anywhere in this scaffold. Every integration boundary
  raises `NotImplementedError`.
- No customer data in fixtures. All tests use `<example.com>` addresses and
  synthetic names.
- HMAC secrets never live in git. `.gitignore` excludes `hmac_secret.txt`,
  `*.env`, and `.cache/`. A pre-commit hook refuses any commit containing a
  file named `hmac_secret.txt`.
- Scheduled GitHub Actions are paused by default via `vars.OUTGROW_PAUSED`.
  Cron triggers are commented out until the operator unpauses.

## Phase 0 dispatch tools

Run these locally during the Phase 0 de-risk window. None of them touch a
production API yet — connector calls raise `NotImplementedError` until you
land credentials.

```bash
python -m intake.match_audit --sample 200 --out match_audit.csv
python -m intake.rep_triangulation --sample 100 --out rep_disagreements.csv
python -m voice.wizard --rep zack --history .cache/voice/zack.csv
python -m drafting.sample_judge --draft path/to/draft.txt --rep zack
```

## CI guards

- `scripts/check_recipient_allowlist.py` — fails CI if any rep email under
  `config/reps/*.yaml` is missing from `config/recipient_allowlist.yaml`.
- `scripts/check_write_scopes.py` — fails CI if `qbo.py` calls any non-GET
  HTTP verb, or if `zoho_crm.py` calls any non-GET except Zoho Activity
  writes.
