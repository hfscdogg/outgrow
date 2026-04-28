"""CI guard: every rep email must appear in `config/recipient_allowlist.yaml`.

Run by ``.github/workflows/ci.yml`` and by the pre-commit `recipient-allowlist`
hook. Returns exit code 1 with a printed reason on the first violation.
"""

from __future__ import annotations

import sys
from collections.abc import Iterable
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
ALLOWLIST_PATH = REPO_ROOT / "config" / "recipient_allowlist.yaml"
REPS_GLOB = "config/reps/*.yaml"


def load_allowlist(path: Path) -> set[str]:
    if not path.exists():
        return set()
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    raw = payload.get("allowed_recipients") or []
    return {str(addr).strip().lower() for addr in raw}


def collect_rep_emails(rep_files: Iterable[Path]) -> dict[Path, str]:
    """Return ``{rep_yaml_path: email}`` for every rep YAML that declares one."""
    emails: dict[Path, str] = {}
    for rep_file in rep_files:
        payload = yaml.safe_load(rep_file.read_text(encoding="utf-8")) or {}
        email = payload.get("email")
        if email:
            emails[rep_file] = str(email).strip().lower()
    return emails


def find_violations(rep_emails: dict[Path, str], allowlist: set[str]) -> list[tuple[Path, str]]:
    return [(p, e) for p, e in rep_emails.items() if e not in allowlist]


def main(repo_root: Path = REPO_ROOT) -> int:
    allowlist = load_allowlist(repo_root / "config" / "recipient_allowlist.yaml")
    rep_files = sorted(repo_root.glob(REPS_GLOB))
    rep_emails = collect_rep_emails(rep_files)
    violations = find_violations(rep_emails, allowlist)
    if violations:
        for path, email in violations:
            print(
                f"FAIL: {path.relative_to(repo_root)} declares email "
                f"{email!r} which is not in recipient_allowlist.yaml",
                file=sys.stderr,
            )
        return 1
    print(
        f"OK: {len(rep_emails)} rep email(s) checked against "
        f"{len(allowlist)} allowlisted recipient(s)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
