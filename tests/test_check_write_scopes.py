"""Write-scope guard tests."""

from __future__ import annotations

from pathlib import Path

from scripts.check_write_scopes import main, scan_file


def _write_pkg(repo: Path, rel: str, source: str) -> Path:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source)
    return path


def test_qbo_post_call_is_flagged(tmp_path: Path) -> None:
    path = _write_pkg(
        tmp_path,
        "intake/sources/qbo.py",
        "import requests\n\ndef do():\n    requests.post('/v3/customer', json={})\n",
    )
    violations = scan_file(path)
    assert len(violations) == 1
    assert violations[0].method == "post"


def test_qbo_get_only_passes(tmp_path: Path) -> None:
    path = _write_pkg(
        tmp_path,
        "intake/sources/qbo.py",
        "import requests\n\ndef do():\n    return requests.get('/v3/customer')\n",
    )
    assert scan_file(path) == []


def test_zoho_activity_post_is_allowed(tmp_path: Path) -> None:
    path = _write_pkg(
        tmp_path,
        "intake/sources/zoho_crm.py",
        "import requests\n"
        "def log_call(d):\n"
        "    return requests.post('/crm/v3/Activities', json=d)\n",
    )
    assert scan_file(path) == []


def test_zoho_non_activity_post_is_flagged(tmp_path: Path) -> None:
    path = _write_pkg(
        tmp_path,
        "intake/sources/zoho_crm.py",
        "import requests\n\n"
        "def edit_contact(d):\n"
        "    return requests.put('/crm/v3/Contacts', json=d)\n",
    )
    violations = scan_file(path)
    assert len(violations) == 1
    assert violations[0].method == "put"


def test_unrelated_file_is_skipped(tmp_path: Path) -> None:
    path = _write_pkg(
        tmp_path,
        "intake/match_audit.py",
        "import requests\n\ndef do():\n    requests.post('/anywhere', json={})\n",
    )
    assert scan_file(path) == []


def test_main_succeeds_on_empty_repo(tmp_path: Path) -> None:
    (tmp_path / "intake").mkdir()
    (tmp_path / "voice").mkdir()
    (tmp_path / "drafting").mkdir()
    assert main(repo_root=tmp_path) == 0


def test_main_fails_when_qbo_writes(tmp_path: Path) -> None:
    _write_pkg(
        tmp_path,
        "intake/sources/qbo.py",
        "import requests\n\ndef do():\n    requests.post('/v3/customer')\n",
    )
    assert main(repo_root=tmp_path) == 1
