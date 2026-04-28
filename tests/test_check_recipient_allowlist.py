"""Recipient-allowlist guard tests."""

from __future__ import annotations

from pathlib import Path

import yaml

from scripts.check_recipient_allowlist import main


def _write_repo(repo: Path, allowlist: list[str], reps: dict[str, str]) -> None:
    (repo / "config").mkdir(parents=True)
    (repo / "config" / "reps").mkdir(parents=True)
    (repo / "config" / "recipient_allowlist.yaml").write_text(
        yaml.safe_dump({"allowed_recipients": allowlist})
    )
    for name, email in reps.items():
        (repo / "config" / "reps" / f"{name}.yaml").write_text(
            yaml.safe_dump({"rep_id": name, "email": email})
        )


def test_allowlist_passes_when_every_email_listed(tmp_path: Path) -> None:
    _write_repo(
        tmp_path,
        ["zack@example.com", "owner@example.com"],
        {"zack": "zack@example.com", "owner": "owner@example.com"},
    )
    assert main(repo_root=tmp_path) == 0


def test_allowlist_fails_when_email_missing(tmp_path: Path) -> None:
    _write_repo(
        tmp_path,
        ["owner@example.com"],
        {"zack": "zack@example.com"},
    )
    assert main(repo_root=tmp_path) == 1


def test_allowlist_passes_with_no_rep_files(tmp_path: Path) -> None:
    _write_repo(tmp_path, ["owner@example.com"], {})
    assert main(repo_root=tmp_path) == 0
