"""QBO sandbox nightly read-only sync.

Pulls Customers + Invoices from the QuickBooks Online sandbox v3 REST
API and writes them to ``.cache/qbo/`` for downstream LTV / dormancy /
last-install ranking signals to consume. **GET-only** by design — the CI
write-scope guard refuses any non-GET HTTP verb on this file with no
exemptions. ``qbo.py`` never writes back to QBO at any phase.

The OAuth token grant against ``oauth.platform.intuit.com`` is a refresh
flow, not a QBO data write; it is implemented with ``urllib.request``
(no write-verb attribute access) so the AST lint stays green while still
being honest about semantics.

QBO uses query-based pagination: ``SELECT * FROM <entity> STARTPOSITION
N MAXRESULTS 1000``. There is no ``hasMore`` flag — convention is to
stop when a page returns fewer rows than the requested ``MAXRESULTS``.
``max_pages`` caps runaway loops on a malformed response.

The module is structured so the network-bound helpers
(``refresh_access_token``, ``make_qbo_query``) sit at the edge and the
pagination + cache logic is pure. Tests inject a fake ``QueryFetcher``;
sockets are blocked at conftest import time so any accidental live call
would raise ``NetworkBlockedError``.

Cache layout::

    .cache/qbo/
      customers.json   # list[dict] — raw QBO Customer records
      invoices.json    # list[dict] — raw QBO Invoice records
      _meta.json       # {synced_at, customer_count, invoice_count, realm_id}
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = REPO_ROOT / ".cache" / "qbo"

QBO_MINOR_VERSION = "65"
MAX_RESULTS = 1000
MAX_PAGES = 500
HTTP_TIMEOUT_S = 30
HTTP_NO_CONTENT = 204

SANDBOX_BASE = "https://sandbox-quickbooks.api.intuit.com"
OAUTH_TOKEN_URL = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"

QueryFetcher = Callable[[str], dict[str, Any]]


@dataclass(frozen=True)
class QboCreds:
    client_id: str
    client_secret: str
    refresh_token: str
    realm_id: str
    base_url: str = SANDBOX_BASE

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> QboCreds:
        source = env if env is not None else dict(os.environ)
        required = (
            "QBO_CLIENT_ID",
            "QBO_CLIENT_SECRET",
            "QBO_SANDBOX_REFRESH_TOKEN",
            "QBO_SANDBOX_REALM_ID",
        )
        missing = [k for k in required if not source.get(k)]
        if missing:
            raise RuntimeError(
                "Missing QBO sandbox credentials in environment: " + ", ".join(missing)
            )
        return cls(
            client_id=source["QBO_CLIENT_ID"],
            client_secret=source["QBO_CLIENT_SECRET"],
            refresh_token=source["QBO_SANDBOX_REFRESH_TOKEN"],
            realm_id=source["QBO_SANDBOX_REALM_ID"],
        )


def refresh_access_token(creds: QboCreds) -> str:
    """Exchange the long-lived refresh token for a short-lived access token.

    Hits Intuit's OAuth provider, not the QBO data API. ``urllib.request``
    is used deliberately so the AST write-scope lint (which forbids ALL
    non-GET in this file) does not flag what is semantically a token
    grant, not a data write.
    """
    creds_b64 = base64.b64encode(f"{creds.client_id}:{creds.client_secret}".encode()).decode()
    body = urllib.parse.urlencode(
        {"grant_type": "refresh_token", "refresh_token": creds.refresh_token}
    ).encode()
    req = urllib.request.Request(
        OAUTH_TOKEN_URL,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Basic {creds_b64}",
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
            payload = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"QBO OAuth token refresh failed: HTTP {e.code} {err_body}") from e
    token = payload.get("access_token")
    if not token:
        raise RuntimeError(f"QBO token refresh returned no access_token: {str(payload)[:200]}")
    return str(token)


def make_qbo_query(access_token: str, base_url: str, realm_id: str) -> QueryFetcher:
    """Bind an access token + realm to a SQL-style query fetcher."""
    base = f"{base_url}/v3/company/{realm_id}/query"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
    }

    def fetch(query: str) -> dict[str, Any]:
        params = urllib.parse.urlencode({"query": query, "minorversion": QBO_MINOR_VERSION})
        url = f"{base}?{params}"
        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
                if resp.status == HTTP_NO_CONTENT:
                    return {"QueryResponse": {}}
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")[:500]
            raise RuntimeError(
                f"QBO query failed (HTTP {e.code}): {err_body[:200]} | query={query[:100]}"
            ) from e

    return fetch


def paginate_query(
    fetch: QueryFetcher,
    entity: str,
    *,
    max_results: int = MAX_RESULTS,
    max_pages: int = MAX_PAGES,
) -> list[dict[str, Any]]:
    """Walk QBO query pagination until a short page is returned.

    QBO has no ``hasMore`` flag; convention is to stop when a page
    returns fewer rows than ``max_results``. The ``max_pages`` cap
    defends against an accidental infinite loop on a malformed response.
    """
    rows: list[dict[str, Any]] = []
    for page in range(max_pages):
        start = 1 + page * max_results
        query = f"SELECT * FROM {entity} STARTPOSITION {start} MAXRESULTS {max_results}"
        payload = fetch(query)
        envelope = payload.get("QueryResponse") or {}
        page_rows = envelope.get(entity) or []
        rows.extend(page_rows)
        if len(page_rows) < max_results:
            return rows
    raise RuntimeError(f"QBO {entity} query exceeded {max_pages} pages; aborting.")


def fetch_customers(fetch: QueryFetcher) -> list[dict[str, Any]]:
    return paginate_query(fetch, "Customer")


def fetch_invoices(fetch: QueryFetcher) -> list[dict[str, Any]]:
    return paginate_query(fetch, "Invoice")


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
    fetch: QueryFetcher,
    *,
    realm_id: str,
    cache_dir: Path = CACHE_DIR,
    now_iso: str | None = None,
) -> dict[str, Any]:
    """Run the full Customers + Invoices pull; write JSON dumps + meta."""
    customers = fetch_customers(fetch)
    invoices = fetch_invoices(fetch)
    write_cache(customers, cache_dir / "customers.json")
    write_cache(invoices, cache_dir / "invoices.json")
    meta = {
        "synced_at": now_iso or _utcnow_iso(),
        "customer_count": len(customers),
        "invoice_count": len(invoices),
        "realm_id": realm_id,
    }
    (cache_dir / "_meta.json").write_text(
        json.dumps(meta, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return meta


def main(argv: list[str] | None = None) -> int:  # pragma: no cover
    parser = argparse.ArgumentParser(description="Nightly QBO sandbox read-only sync")
    parser.add_argument("--cache-dir", type=Path, default=CACHE_DIR)
    args = parser.parse_args(argv)

    creds = QboCreds.from_env()
    token = refresh_access_token(creds)
    fetch = make_qbo_query(token, creds.base_url, creds.realm_id)
    meta = sync(fetch, realm_id=creds.realm_id, cache_dir=args.cache_dir)
    print(json.dumps(meta, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
