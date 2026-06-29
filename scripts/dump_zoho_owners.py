"""Print every distinct Zoho contact Owner with their contact count.

Reads ``.cache/zoho/contacts.json`` (written by ``sync.zoho_crm``) and
emits a sorted table — useful for finding the Zoho user ID of a former
rep when populating ``zoho_user_id_aliases`` in a rep YAML.

Usage::

    uv run python -m sync.zoho_crm   # populate cache
    uv run python -m scripts.dump_zoho_owners

Output columns: contact_count, owner_id, owner_name. Sorted by count desc.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CACHE = REPO_ROOT / ".cache" / "zoho" / "contacts.json"


def main(argv: list[str] | None = None) -> int:  # pragma: no cover
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    args = parser.parse_args(argv)

    if not args.cache.exists():
        print(f"cache not found: {args.cache} — run `uv run python -m sync.zoho_crm` first")
        return 1

    contacts = json.loads(args.cache.read_text(encoding="utf-8"))
    counts: Counter[tuple[str, str]] = Counter()
    for c in contacts:
        owner = c.get("Owner") or {}
        oid = str(owner.get("id") or "—")
        oname = str(owner.get("name") or "—")
        counts[(oid, oname)] += 1

    print(f"{'count':>7}  {'owner_id':<22}  owner_name")
    print("-" * 60)
    for (oid, oname), n in counts.most_common():
        print(f"{n:>7}  {oid:<22}  {oname}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
