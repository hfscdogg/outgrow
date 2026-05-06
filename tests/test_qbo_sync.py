"""QBO sandbox nightly read-only sync tests.

Sockets are blocked at conftest import time, so every test injects a
fake ``QueryFetcher``. Live API contact would raise ``NetworkBlockedError``.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from sync.qbo import (
    MAX_RESULTS,
    QboCreds,
    fetch_customers,
    fetch_invoices,
    paginate_query,
    sync,
    write_cache,
)


def _stub_fetch(pages: list[dict[str, Any]]) -> tuple[Callable[[str], dict[str, Any]], list[str]]:
    """Return a ``QueryFetcher`` that serves prebuilt pages and records queries."""
    seen: list[str] = []
    counter = {"i": 0}

    def fetch(query: str) -> dict[str, Any]:
        seen.append(query)
        idx = counter["i"]
        if idx >= len(pages):
            raise AssertionError(f"unexpected extra page request (query={query})")
        counter["i"] = idx + 1
        return pages[idx]

    return fetch, seen


def test_paginate_stops_when_page_is_short() -> None:
    pages = [{"QueryResponse": {"Customer": [{"Id": "1"}, {"Id": "2"}]}}]
    fetch, _ = _stub_fetch(pages)
    rows = paginate_query(fetch, "Customer", max_results=10)
    assert [r["Id"] for r in rows] == ["1", "2"]


def test_paginate_advances_until_short_page() -> None:
    pages = [
        {"QueryResponse": {"Invoice": [{"Id": str(i)} for i in range(10)]}},
        {"QueryResponse": {"Invoice": [{"Id": "10"}, {"Id": "11"}]}},
    ]
    fetch, queries = _stub_fetch(pages)
    rows = paginate_query(fetch, "Invoice", max_results=10)
    assert [r["Id"] for r in rows] == [str(i) for i in range(12)]
    assert queries == [
        "SELECT * FROM Invoice STARTPOSITION 1 MAXRESULTS 10",
        "SELECT * FROM Invoice STARTPOSITION 11 MAXRESULTS 10",
    ]


def test_paginate_handles_missing_query_response() -> None:
    fetch, _ = _stub_fetch([{}])
    assert paginate_query(fetch, "Customer", max_results=10) == []


def test_paginate_handles_missing_entity_key() -> None:
    fetch, _ = _stub_fetch([{"QueryResponse": {"startPosition": 1}}])
    assert paginate_query(fetch, "Customer", max_results=10) == []


def test_paginate_aborts_when_pages_never_shrink() -> None:
    pages = [{"QueryResponse": {"Customer": [{"Id": str(i)} for i in range(5)]}} for _ in range(10)]
    fetch, _ = _stub_fetch(pages)
    with pytest.raises(RuntimeError, match="exceeded 3 pages"):
        paginate_query(fetch, "Customer", max_results=5, max_pages=3)


def test_paginate_uses_correct_entity_in_select() -> None:
    fetch, queries = _stub_fetch([{"QueryResponse": {"Customer": []}}])
    paginate_query(fetch, "Customer", max_results=100)
    assert queries == ["SELECT * FROM Customer STARTPOSITION 1 MAXRESULTS 100"]


def test_paginate_uses_max_results_default() -> None:
    fetch, queries = _stub_fetch([{"QueryResponse": {"Invoice": []}}])
    paginate_query(fetch, "Invoice")
    assert f"MAXRESULTS {MAX_RESULTS}" in queries[0]


def test_fetch_customers_uses_customer_entity() -> None:
    fetch, queries = _stub_fetch([{"QueryResponse": {"Customer": [{"Id": "c1"}]}}])
    assert fetch_customers(fetch) == [{"Id": "c1"}]
    assert "FROM Customer" in queries[0]


def test_fetch_invoices_uses_invoice_entity() -> None:
    fetch, queries = _stub_fetch([{"QueryResponse": {"Invoice": [{"Id": "i1"}]}}])
    assert fetch_invoices(fetch) == [{"Id": "i1"}]
    assert "FROM Invoice" in queries[0]


def test_write_cache_creates_parent_and_returns_count(tmp_path: Path) -> None:
    out = tmp_path / "nested" / "customers.json"
    n = write_cache([{"Id": "1"}, {"Id": "2"}], out)
    assert n == 2
    assert json.loads(out.read_text()) == [{"Id": "1"}, {"Id": "2"}]


def test_write_cache_writes_sorted_keys_for_stable_diffs(tmp_path: Path) -> None:
    out = tmp_path / "customers.json"
    write_cache([{"b": 2, "a": 1}], out)
    raw = out.read_text()
    assert raw.index('"a"') < raw.index('"b"')


def test_sync_writes_customers_invoices_and_meta(tmp_path: Path) -> None:
    pages = [
        {"QueryResponse": {"Customer": [{"Id": "c1"}, {"Id": "c2"}]}},
        {"QueryResponse": {"Invoice": [{"Id": "i1"}]}},
    ]
    fetch, _ = _stub_fetch(pages)
    meta = sync(
        fetch,
        realm_id="9341454763950398",
        cache_dir=tmp_path,
        now_iso="2026-05-06T11:00:00+00:00",
    )

    assert meta == {
        "synced_at": "2026-05-06T11:00:00+00:00",
        "customer_count": 2,
        "invoice_count": 1,
        "realm_id": "9341454763950398",
    }
    assert json.loads((tmp_path / "customers.json").read_text()) == [
        {"Id": "c1"},
        {"Id": "c2"},
    ]
    assert json.loads((tmp_path / "invoices.json").read_text()) == [{"Id": "i1"}]
    assert json.loads((tmp_path / "_meta.json").read_text()) == meta


def test_sync_creates_cache_dir_if_missing(tmp_path: Path) -> None:
    target = tmp_path / "fresh" / "qbo"
    pages = [
        {"QueryResponse": {"Customer": []}},
        {"QueryResponse": {"Invoice": []}},
    ]
    fetch, _ = _stub_fetch(pages)
    sync(fetch, realm_id="r1", cache_dir=target, now_iso="2026-05-06T11:00:00+00:00")
    assert (target / "customers.json").exists()
    assert (target / "invoices.json").exists()
    assert (target / "_meta.json").exists()


def test_qbo_creds_from_env_returns_creds_with_sandbox_base() -> None:
    env = {
        "QBO_CLIENT_ID": "cid",
        "QBO_CLIENT_SECRET": "csec",
        "QBO_SANDBOX_REFRESH_TOKEN": "rt",
        "QBO_SANDBOX_REALM_ID": "9341454763950398",
    }
    creds = QboCreds.from_env(env)
    assert creds.client_id == "cid"
    assert creds.refresh_token == "rt"
    assert creds.realm_id == "9341454763950398"
    assert creds.base_url == "https://sandbox-quickbooks.api.intuit.com"


def test_qbo_creds_from_env_raises_on_missing() -> None:
    env = {"QBO_CLIENT_ID": "cid"}
    with pytest.raises(RuntimeError, match="QBO_SANDBOX_REFRESH_TOKEN"):
        QboCreds.from_env(env)


def test_qbo_creds_from_env_treats_empty_string_as_missing() -> None:
    env = {
        "QBO_CLIENT_ID": "cid",
        "QBO_CLIENT_SECRET": "csec",
        "QBO_SANDBOX_REFRESH_TOKEN": "",
        "QBO_SANDBOX_REALM_ID": "r1",
    }
    with pytest.raises(RuntimeError, match="QBO_SANDBOX_REFRESH_TOKEN"):
        QboCreds.from_env(env)
