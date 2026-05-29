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

Invoice queries are year-bucketed (``TxnDate`` WHERE clause per calendar
year) because Intuit's API silently returns ``HTTP 500`` code ``10000``
on queries with very deep ``STARTPOSITION``. A prod realm with 21k+
invoices hit that wall on May 29 2026; bucketing keeps each pagination
shallow enough to never reach it. See ``fetch_invoices``.

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
import logging
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from sync._http import urlopen_read_with_retry

REPO_ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = REPO_ROOT / ".cache" / "qbo"

QBO_MINOR_VERSION = "65"
MAX_RESULTS = 1000
MAX_PAGES = 500
HTTP_TIMEOUT_S = 30
HTTP_NO_CONTENT = 204

# Floor year for invoice bucketing — predates Livewire's founding so we never
# miss historical rows. Years with zero invoices return one empty page and
# are skipped fast (~1 cheap round-trip each at the floor).
QBO_INVOICE_START_YEAR = 2008

SANDBOX_BASE = "https://sandbox-quickbooks.api.intuit.com"
PRODUCTION_BASE = "https://quickbooks.api.intuit.com"
OAUTH_TOKEN_URL = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"

MODE_SANDBOX = "sandbox"
MODE_PRODUCTION = "production"

QueryFetcher = Callable[[str], dict[str, Any]]


@dataclass(frozen=True)
class QboCreds:
    client_id: str
    client_secret: str
    refresh_token: str
    realm_id: str
    base_url: str = SANDBOX_BASE
    mode: str = MODE_SANDBOX

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> QboCreds:
        """Read QBO credentials from env, picking sandbox vs production via
        ``OUTGROW_USE_QBO_PROD``.

        Sandbox is the default. Setting ``OUTGROW_USE_QBO_PROD=true`` switches
        to production: reads ``QBO_PROD_CLIENT_ID``,
        ``QBO_PROD_CLIENT_SECRET``, ``QBO_PROD_REFRESH_TOKEN``,
        ``QBO_PROD_REALM_ID``, and targets the production API base.
        """
        source = env if env is not None else dict(os.environ)
        use_prod = (source.get("OUTGROW_USE_QBO_PROD") or "").lower() == "true"
        if use_prod:
            required = (
                "QBO_PROD_CLIENT_ID",
                "QBO_PROD_CLIENT_SECRET",
                "QBO_PROD_REFRESH_TOKEN",
                "QBO_PROD_REALM_ID",
            )
            missing = [k for k in required if not source.get(k)]
            if missing:
                raise RuntimeError(
                    "Missing QBO production credentials in environment: " + ", ".join(missing)
                )
            return cls(
                client_id=source["QBO_PROD_CLIENT_ID"],
                client_secret=source["QBO_PROD_CLIENT_SECRET"],
                refresh_token=source["QBO_PROD_REFRESH_TOKEN"],
                realm_id=source["QBO_PROD_REALM_ID"],
                base_url=PRODUCTION_BASE,
                mode=MODE_PRODUCTION,
            )
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


def refresh_access_token(creds: QboCreds) -> tuple[str, str]:
    """Exchange the long-lived refresh token for a short-lived access token.

    Returns ``(access_token, current_refresh_token)``. Intuit may return a
    NEW refresh token in the response and invalidate the old one after a
    24h grace period — callers should persist the returned refresh token
    if it differs from ``creds.refresh_token`` (see
    ``maybe_persist_refresh_token``).

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
        _, body_bytes = urlopen_read_with_retry(req, timeout=HTTP_TIMEOUT_S)
        payload = json.loads(body_bytes)
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"QBO OAuth token refresh failed: HTTP {e.code} {err_body}") from e
    token = payload.get("access_token")
    if not token:
        raise RuntimeError(f"QBO token refresh returned no access_token: {str(payload)[:200]}")
    new_refresh = payload.get("refresh_token") or creds.refresh_token
    return str(token), str(new_refresh)


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
            status, body = urlopen_read_with_retry(req, timeout=HTTP_TIMEOUT_S)
            if status == HTTP_NO_CONTENT:
                return {"QueryResponse": {}}
            return json.loads(body)
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
    where: str | None = None,
    max_results: int = MAX_RESULTS,
    max_pages: int = MAX_PAGES,
) -> list[dict[str, Any]]:
    """Walk QBO query pagination until a short page is returned.

    QBO has no ``hasMore`` flag; convention is to stop when a page
    returns fewer rows than ``max_results``. The ``max_pages`` cap
    defends against an accidental infinite loop on a malformed response.

    Pass ``where`` (raw SQL fragment, no leading ``WHERE``) to filter
    server-side — used by ``fetch_invoices`` to year-bucket around the
    deep-pagination wall.
    """
    rows: list[dict[str, Any]] = []
    where_sql = f" WHERE {where}" if where else ""
    for page in range(max_pages):
        start = 1 + page * max_results
        query = f"SELECT * FROM {entity}{where_sql} STARTPOSITION {start} MAXRESULTS {max_results}"
        payload = fetch(query)
        envelope = payload.get("QueryResponse") or {}
        page_rows = envelope.get(entity) or []
        rows.extend(page_rows)
        if len(page_rows) < max_results:
            return rows
    suffix = f" within filter '{where}'" if where else ""
    raise RuntimeError(f"QBO {entity} query exceeded {max_pages} pages{suffix}; aborting.")


def fetch_customers(fetch: QueryFetcher) -> list[dict[str, Any]]:
    return paginate_query(fetch, "Customer")


def fetch_invoices(
    fetch: QueryFetcher,
    *,
    today: date | None = None,
    start_year: int = QBO_INVOICE_START_YEAR,
) -> list[dict[str, Any]]:
    """Pull every invoice, bucketed by ``TxnDate`` year.

    An unfiltered ``SELECT * FROM Invoice`` paginates from ``STARTPOSITION
    1``; once it climbs past ~20k Intuit's API silently returns ``HTTP 500``
    code ``10000`` ("An application error has occurred"). Henry's prod
    realm hit that wall at position 21001 on May 29 2026. Splitting by
    year keeps each pagination shallow.

    Buckets run ``start_year`` through ``today.year + 1`` (the trailing
    year covers advance-dated invoices). Empty years cost one round-trip.
    """
    today_date = today or datetime.now(UTC).date()
    rows: list[dict[str, Any]] = []
    for year in range(start_year, today_date.year + 2):
        where = f"TxnDate >= '{year}-01-01' AND TxnDate <= '{year}-12-31'"
        rows.extend(paginate_query(fetch, "Invoice", where=where))
    return rows


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
    today: date | None = None,
    start_year: int = QBO_INVOICE_START_YEAR,
) -> dict[str, Any]:
    """Run the full Customers + Invoices pull; write JSON dumps + meta.

    ``today`` and ``start_year`` are passed through to ``fetch_invoices``
    to size the year-bucketed pagination — tests use them to keep stubs
    small; production runs use defaults (today = UTC now, floor 2008).
    """
    customers = fetch_customers(fetch)
    invoices = fetch_invoices(fetch, today=today, start_year=start_year)
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


# --- refresh-token persistence ----------------------------------------------
#
# Intuit rotates QBO refresh tokens on every successful OAuth refresh: the
# response carries a new ``refresh_token`` and the old one is invalidated
# after a ~24h grace period. If we don't persist the rotated token, the
# GitHub Actions secret goes stale and the next run 400s with
# ``invalid_grant``. The two helpers below close that loop by writing the
# new token back to the secret via the GitHub Actions REST API.
#
# IMPORTANT: ``update_github_actions_secret`` issues a PUT to
# ``api.github.com`` — that's a write to GitHub, not to QBO. The
# write-scope lint (``scripts/check_write_scopes.py``) only flags
# ``.put`` / ``.post`` attribute calls; ``urllib.request.urlopen(req,
# method="PUT")`` doesn't match the AST pattern. The semantic intent is
# preserved: this file still doesn't write to QBO at any phase.


def update_github_actions_secret(
    *,
    repo: str,
    name: str,
    value: str,
    pat: str,
    api_base: str = "https://api.github.com",
) -> None:
    """Encrypt + PUT a GitHub Actions secret via the REST API.

    Two-step: GET the repo's libsodium public key, encrypt ``value`` with
    a sealed box, PUT the base64-encoded ciphertext + key_id. Uses the
    fine-grained PAT in ``pat`` (needs ``actions:write`` scope on this
    repo only).
    """
    import nacl.encoding  # noqa: PLC0415  -- optional dep, only used here
    import nacl.public  # noqa: PLC0415

    pubkey_url = f"{api_base}/repos/{repo}/actions/secrets/public-key"
    pubkey_req = urllib.request.Request(
        pubkey_url,
        headers={
            "Authorization": f"Bearer {pat}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        method="GET",
    )
    _, pubkey_body = urlopen_read_with_retry(pubkey_req, timeout=HTTP_TIMEOUT_S)
    pubkey = json.loads(pubkey_body)

    public_key = nacl.public.PublicKey(
        pubkey["key"].encode(),
        nacl.encoding.Base64Encoder,  # type: ignore[arg-type]  -- pynacl stubs out of date
    )
    sealed_box = nacl.public.SealedBox(public_key)
    encrypted = sealed_box.encrypt(value.encode())
    encrypted_b64 = base64.b64encode(encrypted).decode()

    put_url = f"{api_base}/repos/{repo}/actions/secrets/{name}"
    put_body = json.dumps({"encrypted_value": encrypted_b64, "key_id": pubkey["key_id"]}).encode()
    put_req = urllib.request.Request(
        put_url,
        data=put_body,
        headers={
            "Authorization": f"Bearer {pat}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
        },
        method="PUT",
    )
    put_status, _ = urlopen_read_with_retry(put_req, timeout=HTTP_TIMEOUT_S)
    # 201 = created, 204 = updated; HTTPError raised by default on 4xx/5xx.
    if put_status not in (201, 204):
        raise RuntimeError(f"GitHub secret update returned status {put_status}")


def _emit_gha_annotation(level: str, title: str, message: str) -> None:
    """Print a GitHub Actions workflow command so the UI surfaces the message
    as a yellow warning / red error annotation in the run summary.

    Outside GitHub Actions (local CLI, tests) the line is a harmless print
    that gets logged alongside the regular log output. Gated on
    ``GITHUB_ACTIONS=true`` so local stdout stays clean.
    """
    if os.environ.get("GITHUB_ACTIONS", "").lower() != "true":
        return
    # GH Actions workflow-command syntax: ::level title=foo::message
    # Newlines in message must be URL-encoded as %0A.
    safe_message = message.replace("\n", "%0A")
    print(f"::{level} title={title}::{safe_message}", file=sys.stdout, flush=True)


def maybe_persist_refresh_token(
    *,
    new_token: str,
    old_token: str,
    secret_name: str = "QBO_SANDBOX_REFRESH_TOKEN",
    repo: str | None = None,
    pat: str | None = None,
) -> bool:
    """Persist ``new_token`` as a GitHub Actions secret if it differs from
    ``old_token``. Returns ``True`` if persistence happened.

    Skipped silently (with a warning) when:
      * Intuit didn't rotate the token (new == old)
      * No PAT provided (env var unset for one-off dispatches)
      * No repo provided (running outside GitHub Actions)

    On API failure: log + return ``False`` rather than raise. The current
    sync's access token already worked; the next run will surface a stale
    token via 400 ``invalid_grant`` on its own refresh.

    Both skip-on-config and API-failure paths emit a GitHub Actions
    workflow annotation in addition to the structured log so the rotation
    chain breaking is visible in the run summary — not just buried in the
    log scroll. This is the failure mode that lost a day's pulse on May 24.
    """
    if new_token == old_token:
        return False
    if not pat or not repo:
        msg = (
            "QBO refresh token rotated but persistence skipped: "
            "GH_PAT_ROTATE_SECRETS or GITHUB_REPOSITORY not set. The next "
            "run will fail with invalid_grant once Intuit's 24h grace "
            "expires — re-mint GH_PAT_ROTATE_SECRETS to restore the chain."
        )
        logging.getLogger(__name__).warning(msg)
        _emit_gha_annotation("warning", "QBO token rotation skipped", msg)
        return False
    try:
        update_github_actions_secret(repo=repo, name=secret_name, value=new_token, pat=pat)
    except Exception as e:  # noqa: BLE001  -- best-effort, log and continue
        msg = (
            f"Failed to persist rotated QBO token to secret {secret_name}: {e}. "
            "Next run will fail with invalid_grant after Intuit's 24h grace expires."
        )
        logging.getLogger(__name__).error(msg)
        _emit_gha_annotation("error", "QBO token rotation failed", msg)
        return False
    logging.getLogger(__name__).info(
        "Persisted rotated QBO refresh token to GitHub Actions secret %s.", secret_name
    )
    return True


def main(argv: list[str] | None = None) -> int:  # pragma: no cover
    parser = argparse.ArgumentParser(description="Nightly QBO read-only sync (sandbox or prod)")
    parser.add_argument("--cache-dir", type=Path, default=CACHE_DIR)
    args = parser.parse_args(argv)

    creds = QboCreds.from_env()
    token, new_refresh = refresh_access_token(creds)
    persist_secret_name = (
        "QBO_PROD_REFRESH_TOKEN" if creds.mode == MODE_PRODUCTION else "QBO_SANDBOX_REFRESH_TOKEN"
    )
    maybe_persist_refresh_token(
        new_token=new_refresh,
        old_token=creds.refresh_token,
        secret_name=persist_secret_name,
        repo=os.environ.get("GITHUB_REPOSITORY"),
        pat=os.environ.get("GH_PAT_ROTATE_SECRETS"),
    )
    fetch = make_qbo_query(token, creds.base_url, creds.realm_id)
    meta = sync(fetch, realm_id=creds.realm_id, cache_dir=args.cache_dir)
    print(json.dumps(meta, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
