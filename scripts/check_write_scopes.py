"""CI guard: forbid non-GET HTTP outside Zoho Activity writes.

Scans every Python source file under known package roots for HTTP write
verbs in `qbo.py` (read-only by design) and in `zoho_crm.py` (read-only
except Zoho Activity writes — the rep-action logging path).

The check is AST-based so a method named `requests.post(...)` is
recognized regardless of import style. Files that don't yet exist in
the Phase 0 scaffold are silently skipped.
"""

from __future__ import annotations

import ast
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

WRITE_METHODS: frozenset[str] = frozenset({"post", "put", "patch", "delete"})

# zoho_crm.py is allowed to write *only* to the Zoho Activities endpoint
# (the Phase 2 rep-action logging path). Any string literal in the same
# call site that contains one of these tokens is treated as an exemption.
ZOHO_ACTIVITY_TOKENS: tuple[str, ...] = ("/Activities", "Activities/upsert")

SCAN_GLOBS: tuple[str, ...] = (
    "intake/**/*.py",
    "voice/**/*.py",
    "drafting/**/*.py",
    "sync/**/*.py",
    "src/**/*.py",
)


@dataclass(frozen=True)
class Violation:
    path: Path
    line: int
    method: str
    detail: str


def _call_is_write(node: ast.Call) -> str | None:
    """Return the lowercased write verb if `node` looks like a write HTTP call."""
    func = node.func
    if isinstance(func, ast.Attribute) and func.attr.lower() in WRITE_METHODS:
        return func.attr.lower()
    if isinstance(func, ast.Name) and func.id.lower() in WRITE_METHODS:
        return func.id.lower()
    return None


def _call_string_literals(node: ast.Call) -> list[str]:
    literals: list[str] = []
    for arg in node.args:
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            literals.append(arg.value)
    for kw in node.keywords:
        if isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
            literals.append(kw.value.value)
    return literals


def scan_file(path: Path) -> list[Violation]:
    """Return write-verb violations in a single Python source file."""
    if not path.exists():
        return []
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    is_zoho_crm = path.name == "zoho_crm.py"
    is_qbo = path.name == "qbo.py"
    if not (is_zoho_crm or is_qbo):
        return []

    violations: list[Violation] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        method = _call_is_write(node)
        if not method:
            continue
        literals = _call_string_literals(node)
        if is_zoho_crm and any(tok in lit for lit in literals for tok in ZOHO_ACTIVITY_TOKENS):
            continue
        violations.append(
            Violation(
                path=path,
                line=node.lineno,
                method=method,
                detail=", ".join(literals)[:80] if literals else "",
            )
        )
    return violations


def discover_files(repo_root: Path, globs: Iterable[str] = SCAN_GLOBS) -> list[Path]:
    files: list[Path] = []
    for pattern in globs:
        files.extend(repo_root.glob(pattern))
    return sorted(set(files))


def main(repo_root: Path = REPO_ROOT) -> int:
    violations: list[Violation] = []
    for source_file in discover_files(repo_root):
        violations.extend(scan_file(source_file))
    if violations:
        for v in violations:
            rel = v.path.relative_to(repo_root)
            print(
                f"FAIL: {rel}:{v.line} forbids HTTP {v.method.upper()} "
                f"({v.detail or 'no string literal'})",
                file=sys.stderr,
            )
        return 1
    print("OK: no forbidden HTTP write calls detected.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
