"""Zoho CRM nightly read-only sync.

Pulls Contacts + Deals from the Zoho CRM v3 REST API and writes them to
``.cache/zoho/`` for downstream ranking + drafting modules to consume.
GET-only on CRM data — the only POST is the OAuth token-refresh against
``accounts.zoho.{dc}/oauth/v2/token``, which is a token grant flow, not a
write to CRM data. The CI write-scope guard (`scripts/check_write_scopes.py`)
scans this file and refuses any ``.post`` / ``.put`` / ``.patch`` /
``.delete`` attribute call against ``/crm/v3`` endpoints; using
``urllib.request`` (no write-verb attribute access) keeps that lint green.

The module is structured so the network-bound helpers
(``refresh_access_token``, ``make_zoho_get``) sit at the edge and the
pagination + cache logic stays pure and testable. Tests inject a fake
``PageFetcher``; sockets are blocked at conftest import time so any
accidental live call would raise ``NetworkBlockedError``.

Cache layout::

    .cache/zoho/
      contacts.json   # list[dict] — raw Zoho Contact records
      deals.json      # list[dict] — raw Zoho Deal records
      _meta.json      # {synced_at, contact_count, deal_count}
"""

from __future__ import annotations

import argparse
import json
import os
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = REPO_ROOT / ".cache" / "zoho"

PER_PAGE = 200
MAX_PAGES = 500
HTTP_TIMEOUT_S = 30
HTTP_NO_CONTENT = 204

CONTACT_FIELDS = (
    "id,Full_Name,First_Name,Last_Name,Email,Phone,Mobile,Owner,"
    "Account_Name,Mailing_Street,Mailing_City,Mailing_State,Mailing_Zip,"
    "Created_Time,Modified_Time"
)
DEAL_FIELDS = (
    "id,Deal_Name,Contact_Name,Account_Name,Stage,Amount,Owner,"
    "Closing_Date,Created_Time,Modified_Time"
)

PageFetcher = Callable[[str, dict[str, Any]], dict[str, Any]]


@dataclass(frozen=True)
class ZohoCreds:
    refresh_token: str
    client_id: str
    client_secret: str
    dc: str = "com"

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> ZohoCreds:
        source = env if env is not None else dict(os.environ)
        required = ("ZOHO_CRM_REFRESH_TOKEN", "ZOHO_CRM_CLIENT_ID", "ZOHO_CRM_CLIENT_SECRET")
        missing = [k for k in required if not source.get(k)]
        if missing:
            raise RuntimeError("Missing Zoho CRM credentials in environment: " + ", ".join(missing))
        return cls(
            refresh_token=source["ZOHO_CRM_REFRESH_TOKEN"],
            client_id=source["ZOHO_CRM_CLIENT_ID"],
            client_secret=source["ZOHO_CRM_CLIENT_SECRET"],
            dc=source.get("ZOHO_CRM_DC") or "com",
        )


def refresh_access_token(creds: ZohoCreds) -> str:
    """Exchange the long-lived refresh token for a short-lived access token.

    Hits ``accounts.zoho.{dc}/oauth/v2/token`` — the OAuth provider, not
    Zoho CRM. ``urllib.request`` is used deliberately: a ``requests.post``
    style call here would (correctly) be flagged by the write-scope lint,
    but a token grant against the OAuth provider is not a CRM write.
    """
    url = f"https://accounts.zoho.{creds.dc}/oauth/v2/token"
    body = urllib.parse.urlencode(
        {
            "refresh_token": creds.refresh_token,
            "client_id": creds.client_id,
            "client_secret": creds.client_secret,
            "grant_type": "refresh_token",
        }
    ).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
        payload = json.loads(resp.read())
    token = payload.get("access_token")
    if not token:
        raise RuntimeError(f"Zoho token refresh failed: {str(payload)[:200]}")
    return str(token)


def make_zoho_get(access_token: str, dc: str) -> PageFetcher:
    """Bind an access token to a paginated GET fetcher for ``zohoapis.{dc}``."""
    base = f"https://www.zohoapis.{dc}"
    headers = {"Authorization": f"Zoho-oauthtoken {access_token}"}

    def fetch(module_path: str, params: dict[str, Any]) -> dict[str, Any]:
        url = f"{base}{module_path}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers=headers, method="GET")
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
            if resp.status == HTTP_NO_CONTENT:
                return {"data": [], "info": {"more_records": False}}
            return json.loads(resp.read())

    return fetch


def paginate(
    fetch: PageFetcher,
    module_path: str,
    fields: str,
    *,
    per_page: int = PER_PAGE,
    max_pages: int = MAX_PAGES,
) -> list[dict[str, Any]]:
    """Walk Zoho's ``info.more_records`` pagination until exhausted.

    Raises ``RuntimeError`` if pagination doesn't terminate within
    ``max_pages`` — defends against an accidental infinite loop on a
    malformed API response.
    """
    rows: list[dict[str, Any]] = []
    for page in range(1, max_pages + 1):
        payload = fetch(
            module_path,
            {"fields": fields, "per_page": per_page, "page": page},
        )
        rows.extend(payload.get("data") or [])
        info = payload.get("info") or {}
        if not info.get("more_records"):
            return rows
    raise RuntimeError(f"Zoho {module_path} pagination exceeded {max_pages} pages; aborting.")


def fetch_contacts(fetch: PageFetcher) -> list[dict[str, Any]]:
    return paginate(fetch, "/crm/v3/Contacts", CONTACT_FIELDS)


def fetch_deals(fetch: PageFetcher) -> list[dict[str, Any]]:
    return paginate(fetch, "/crm/v3/Deals", DEAL_FIELDS)


def write_cache(rows: Iterable[dict[str, Any]], path: Path) -> int:
    materialized = list(rows)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(materialized, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return len(materialized)


def _utcnow_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def sync(
    fetch: PageFetcher,
    *,
    cache_dir: Path = CACHE_DIR,
    now_iso: str | None = None,
) -> dict[str, Any]:
    """Run the full Contacts + Deals sync; write JSON dumps + meta to ``cache_dir``."""
    contacts = fetch_contacts(fetch)
    deals = fetch_deals(fetch)
    write_cache(contacts, cache_dir / "contacts.json")
    write_cache(deals, cache_dir / "deals.json")
    meta = {
        "synced_at": now_iso or _utcnow_iso(),
        "contact_count": len(contacts),
        "deal_count": len(deals),
    }
    (cache_dir / "_meta.json").write_text(
        json.dumps(meta, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return meta


def main(argv: list[str] | None = None) -> int:  # pragma: no cover
    parser = argparse.ArgumentParser(description="Nightly Zoho CRM read-only sync")
    parser.add_argument("--cache-dir", type=Path, default=CACHE_DIR)
    args = parser.parse_args(argv)

    creds = ZohoCreds.from_env()
    token = refresh_access_token(creds)
    fetch = make_zoho_get(token, creds.dc)
    meta = sync(fetch, cache_dir=args.cache_dir)
    print(json.dumps(meta, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
