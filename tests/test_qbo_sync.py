"""QBO sandbox nightly read-only sync tests.

Sockets are blocked at conftest import time, so every test injects a
fake ``QueryFetcher``. Live API contact would raise ``NetworkBlockedError``.
"""

from __future__ import annotations

import io
import json
import urllib.error
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

import nacl.encoding
import nacl.public
import pytest

from sync import qbo
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


def _empty_invoice_pages(n: int) -> list[dict[str, Any]]:
    """N short pages — the cheap path each empty month bucket takes."""
    return [{"QueryResponse": {"Invoice": []}} for _ in range(n)]


def test_fetch_invoices_uses_invoice_entity() -> None:
    # Two-year window (2026 + 2027) = 24 month buckets. First month has
    # one invoice; remaining 23 months empty.
    pages = [{"QueryResponse": {"Invoice": [{"Id": "i1"}]}}, *_empty_invoice_pages(23)]
    fetch, queries = _stub_fetch(pages)
    rows = fetch_invoices(fetch, today=date(2026, 5, 29), start_year=2026)
    assert rows == [{"Id": "i1"}]
    assert all("FROM Invoice" in q for q in queries)
    assert len(queries) == 24


def test_fetch_invoices_buckets_by_month_with_where_clause() -> None:
    # Single-year window (start_year=2025, today=2025-12-31) = 12 month
    # buckets, plus 12 for trailing 2026 = 24. All empty except Jan 2025.
    pages = [
        {"QueryResponse": {"Invoice": [{"Id": "jan1"}, {"Id": "jan2"}]}},
        *_empty_invoice_pages(23),
    ]
    fetch, queries = _stub_fetch(pages)
    rows = fetch_invoices(fetch, today=date(2025, 12, 31), start_year=2025)
    assert [r["Id"] for r in rows] == ["jan1", "jan2"]
    # Jan: TxnDate >= '2025-01-01' AND TxnDate <= '2025-01-31'
    assert "TxnDate >= '2025-01-01'" in queries[0]
    assert "TxnDate <= '2025-01-31'" in queries[0]
    # Feb: 28-day month (2025 is not a leap year).
    assert "TxnDate >= '2025-02-01'" in queries[1]
    assert "TxnDate <= '2025-02-28'" in queries[1]
    # Dec: 31-day month.
    assert "TxnDate >= '2025-12-01'" in queries[11]
    assert "TxnDate <= '2025-12-31'" in queries[11]
    # Trailing year starts fresh in Jan 2026.
    assert "TxnDate >= '2026-01-01'" in queries[12]


def test_fetch_invoices_handles_leap_february() -> None:
    # 2024 is a leap year — Feb has 29 days. Regression guard: a hardcoded
    # 28 would silently miss Feb 29 invoices.
    fetch, queries = _stub_fetch(_empty_invoice_pages(24))  # 2024 + 2025
    fetch_invoices(fetch, today=date(2024, 12, 31), start_year=2024)
    feb = queries[1]
    assert "TxnDate >= '2024-02-01'" in feb
    assert "TxnDate <= '2024-02-29'" in feb


def test_fetch_invoices_paginates_within_a_single_month() -> None:
    # One month worth of MAX_RESULTS+1 rows: two pages within the bucket,
    # then 23 empty buckets for the remaining months.
    pages = [
        {"QueryResponse": {"Invoice": [{"Id": str(i)} for i in range(MAX_RESULTS)]}},
        {"QueryResponse": {"Invoice": [{"Id": "tail"}]}},
        *_empty_invoice_pages(23),
    ]
    fetch, queries = _stub_fetch(pages)
    rows = fetch_invoices(fetch, today=date(2025, 12, 31), start_year=2025)
    assert len(rows) == MAX_RESULTS + 1
    # First Jan page at STARTPOSITION 1, second Jan page at STARTPOSITION 1001.
    assert "STARTPOSITION 1 " in queries[0]
    assert "TxnDate <= '2025-01-31'" in queries[0]
    assert f"STARTPOSITION {MAX_RESULTS + 1} " in queries[1]
    assert "TxnDate <= '2025-01-31'" in queries[1]
    # Feb (next month) starts fresh at STARTPOSITION 1.
    assert "STARTPOSITION 1 " in queries[2]
    assert "TxnDate >= '2025-02-01'" in queries[2]


def test_fetch_invoices_empty_months_pass_through() -> None:
    # start_year=2026, today=2026-05-29 → 24 month buckets all empty.
    fetch, queries = _stub_fetch(_empty_invoice_pages(24))
    rows = fetch_invoices(fetch, today=date(2026, 5, 29), start_year=2026)
    assert rows == []
    assert len(queries) == 24


def _overload_response(reason: str = "year-end surge") -> Exception:
    """Build the same RuntimeError shape ``paginate_query`` raises on Intuit's
    ``HTTP 500`` + code ``10000`` envelope so the adaptive splitter sees the
    real wire signature it's matching on.
    """
    return RuntimeError(
        f'QBO query failed (HTTP 500): {{"Fault":{{"Error":[{{"code":"10000",'
        f'"Message":"An application error has occurred ({reason})"}}]}}}}'
    )


def _adaptive_fetch(
    *,
    overflow_when: Callable[[str], bool],
    rows_for: Callable[[str], list[dict[str, Any]]],
) -> tuple[Callable[[str], dict[str, Any]], list[str]]:
    """Fetcher that raises Intuit's overload error on queries matching
    ``overflow_when``, otherwise returns the rows from ``rows_for``.
    """
    seen: list[str] = []

    def fetch(query: str) -> dict[str, Any]:
        seen.append(query)
        if overflow_when(query):
            raise _overload_response()
        return {"QueryResponse": {"Invoice": rows_for(query)}}

    return fetch, seen


def test_fetch_invoices_splits_month_on_overload() -> None:
    # December 2025 overflows as a whole month; after halving (Dec 1-16 vs
    # Dec 17-31) both halves come back fine.
    def overflow(q: str) -> bool:
        return "TxnDate >= '2025-12-01'" in q and "TxnDate <= '2025-12-31'" in q

    def rows_for(q: str) -> list[dict[str, Any]]:
        if "'2025-12-01'" in q and "'2025-12-16'" in q:
            return [{"Id": "dec-a"}]
        if "'2025-12-17'" in q and "'2025-12-31'" in q:
            return [{"Id": "dec-b"}]
        return []

    fetch, queries = _adaptive_fetch(overflow_when=overflow, rows_for=rows_for)
    # Single-year window: Jan-Dec 2025 (12 buckets) + Jan-Dec 2026 trailing (12).
    rows = fetch_invoices(fetch, today=date(2025, 12, 31), start_year=2025)
    ids = {r["Id"] for r in rows}
    assert ids == {"dec-a", "dec-b"}
    # December attempted as a whole month, then the two halves.
    assert any("'2025-12-01'" in q and "'2025-12-31'" in q for q in queries)
    assert any("'2025-12-01'" in q and "'2025-12-16'" in q for q in queries)
    assert any("'2025-12-17'" in q and "'2025-12-31'" in q for q in queries)


def test_fetch_invoices_recursively_splits_until_safe() -> None:
    # December 2025 overflows whole, AND its first half (1-16) also overflows.
    # Only the 1-8 / 9-16 quarter-windows succeed.
    def overflow(q: str) -> bool:
        is_dec_full = "'2025-12-01'" in q and "'2025-12-31'" in q
        is_dec_first_half = "'2025-12-01'" in q and "'2025-12-16'" in q
        return is_dec_full or is_dec_first_half

    fetch, queries = _adaptive_fetch(
        overflow_when=overflow,
        rows_for=lambda q: [{"Id": "dec"}] if "'2025-12-" in q else [],
    )
    rows = fetch_invoices(fetch, today=date(2025, 12, 31), start_year=2025)
    # Three December windows survive: 12-01..12-08, 12-09..12-16, 12-17..12-31.
    assert sum(1 for r in rows if r["Id"] == "dec") == 3
    assert any("'2025-12-01'" in q and "'2025-12-08'" in q for q in queries)
    assert any("'2025-12-09'" in q and "'2025-12-16'" in q for q in queries)


def test_fetch_invoices_raises_clear_error_when_single_day_overflows() -> None:
    # Every window overflows — splitting bottoms out at a single day and
    # surfaces a clear error rather than recursing forever.
    fetch, _ = _adaptive_fetch(overflow_when=lambda _q: True, rows_for=lambda _q: [])
    with pytest.raises(RuntimeError, match="overflows the result-set wall even as a single day"):
        fetch_invoices(fetch, today=date(2026, 1, 31), start_year=2026)


def test_fetch_invoices_propagates_non_overload_errors() -> None:
    # A 4xx or non-10000 5xx must NOT be swallowed by the adaptive splitter.
    def fetch(_q: str) -> dict[str, Any]:
        raise RuntimeError("QBO query failed (HTTP 401): {} | query=...")

    with pytest.raises(RuntimeError, match="HTTP 401"):
        fetch_invoices(fetch, today=date(2026, 1, 31), start_year=2026)


def test_paginate_query_appends_where_clause() -> None:
    fetch, queries = _stub_fetch([{"QueryResponse": {"Invoice": []}}])
    paginate_query(fetch, "Invoice", where="TxnDate >= '2024-01-01'")
    assert queries == [
        "SELECT * FROM Invoice WHERE TxnDate >= '2024-01-01' "
        f"STARTPOSITION 1 MAXRESULTS {MAX_RESULTS}"
    ]


def test_paginate_query_runaway_within_where_clause_mentions_filter() -> None:
    pages = [{"QueryResponse": {"Customer": [{"Id": str(i)} for i in range(5)]}} for _ in range(10)]
    fetch, _ = _stub_fetch(pages)
    with pytest.raises(RuntimeError, match="within filter 'Active = true'"):
        paginate_query(fetch, "Customer", where="Active = true", max_results=5, max_pages=3)


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
        # Invoice fetch is month-bucketed: 2026 + 2027 trailing year = 24 months.
        # One invoice in Jan 2026; remaining 23 months empty.
        {"QueryResponse": {"Invoice": [{"Id": "i1"}]}},
        *_empty_invoice_pages(23),
    ]
    fetch, _ = _stub_fetch(pages)
    meta = sync(
        fetch,
        realm_id="9341454763950398",
        cache_dir=tmp_path,
        now_iso="2026-05-06T11:00:00+00:00",
        today=date(2026, 5, 6),
        start_year=2026,
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
        # 2026 + 2027 trailing year = 24 month buckets, all empty.
        *_empty_invoice_pages(24),
    ]
    fetch, _ = _stub_fetch(pages)
    sync(
        fetch,
        realm_id="r1",
        cache_dir=target,
        now_iso="2026-05-06T11:00:00+00:00",
        today=date(2026, 5, 6),
        start_year=2026,
    )
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


def test_qbo_creds_from_env_returns_prod_creds_when_toggle_true() -> None:
    env = {
        "OUTGROW_USE_QBO_PROD": "true",
        "QBO_PROD_CLIENT_ID": "prod-cid",
        "QBO_PROD_CLIENT_SECRET": "prod-csec",
        "QBO_PROD_REFRESH_TOKEN": "prod-rt",
        "QBO_PROD_REALM_ID": "prod-realm",
        # Sandbox vars present — should NOT be picked when prod toggle is on.
        "QBO_CLIENT_ID": "sandbox-cid",
        "QBO_SANDBOX_REFRESH_TOKEN": "sandbox-rt",
    }
    creds = QboCreds.from_env(env)
    assert creds.client_id == "prod-cid"
    assert creds.client_secret == "prod-csec"
    assert creds.refresh_token == "prod-rt"
    assert creds.realm_id == "prod-realm"
    assert creds.base_url == qbo.PRODUCTION_BASE
    assert creds.mode == qbo.MODE_PRODUCTION


def test_qbo_creds_from_env_returns_sandbox_creds_when_toggle_absent() -> None:
    env = {
        "QBO_CLIENT_ID": "cid",
        "QBO_CLIENT_SECRET": "csec",
        "QBO_SANDBOX_REFRESH_TOKEN": "rt",
        "QBO_SANDBOX_REALM_ID": "r1",
    }
    creds = QboCreds.from_env(env)
    assert creds.base_url == qbo.SANDBOX_BASE
    assert creds.mode == qbo.MODE_SANDBOX


def test_qbo_creds_from_env_returns_sandbox_when_toggle_empty_string() -> None:
    """GitHub Actions interpolates an unset `vars.OUTGROW_USE_QBO_PROD` as
    an empty string. Must be treated as 'sandbox', not as 'enable prod'."""
    env = {
        "OUTGROW_USE_QBO_PROD": "",
        "QBO_CLIENT_ID": "cid",
        "QBO_CLIENT_SECRET": "csec",
        "QBO_SANDBOX_REFRESH_TOKEN": "rt",
        "QBO_SANDBOX_REALM_ID": "r1",
    }
    creds = QboCreds.from_env(env)
    assert creds.mode == qbo.MODE_SANDBOX


def test_qbo_creds_from_env_returns_sandbox_when_toggle_false_string() -> None:
    env = {
        "OUTGROW_USE_QBO_PROD": "false",
        "QBO_CLIENT_ID": "cid",
        "QBO_CLIENT_SECRET": "csec",
        "QBO_SANDBOX_REFRESH_TOKEN": "rt",
        "QBO_SANDBOX_REALM_ID": "r1",
    }
    creds = QboCreds.from_env(env)
    assert creds.mode == qbo.MODE_SANDBOX


def test_qbo_creds_from_env_prod_toggle_raises_on_missing_prod_creds() -> None:
    env = {
        "OUTGROW_USE_QBO_PROD": "true",
        # Missing QBO_PROD_CLIENT_SECRET and QBO_PROD_REALM_ID.
        "QBO_PROD_CLIENT_ID": "prod-cid",
        "QBO_PROD_REFRESH_TOKEN": "prod-rt",
    }
    with pytest.raises(RuntimeError, match=r"production credentials.*QBO_PROD_CLIENT_SECRET"):
        QboCreds.from_env(env)


def test_qbo_creds_from_env_prod_toggle_treats_empty_prod_creds_as_missing() -> None:
    env = {
        "OUTGROW_USE_QBO_PROD": "true",
        "QBO_PROD_CLIENT_ID": "prod-cid",
        "QBO_PROD_CLIENT_SECRET": "",
        "QBO_PROD_REFRESH_TOKEN": "prod-rt",
        "QBO_PROD_REALM_ID": "prod-realm",
    }
    with pytest.raises(RuntimeError, match="QBO_PROD_CLIENT_SECRET"):
        QboCreds.from_env(env)


def test_refresh_access_token_surfaces_oauth_400_body(monkeypatch) -> None:
    def fake_urlopen(req, timeout):  # noqa: ARG001
        raise urllib.error.HTTPError(
            url=req.full_url,
            code=400,
            msg="Bad Request",
            hdrs=None,  # type: ignore[arg-type]
            fp=io.BytesIO(b'{"error":"invalid_grant"}'),
        )

    monkeypatch.setattr(qbo.urllib.request, "urlopen", fake_urlopen)
    creds = qbo.QboCreds(
        client_id="cid",
        client_secret="csec",
        refresh_token="rt",
        realm_id="r1",
    )
    with pytest.raises(RuntimeError, match=r"HTTP 400.*invalid_grant"):
        qbo.refresh_access_token(creds)


def test_make_qbo_query_surfaces_api_error_body(monkeypatch) -> None:
    def fake_urlopen(req, timeout):  # noqa: ARG001
        raise urllib.error.HTTPError(
            url=req.full_url,
            code=401,
            msg="Unauthorized",
            hdrs=None,  # type: ignore[arg-type]
            fp=io.BytesIO(b'{"Fault":{"Error":[{"code":"3200","Message":"Token expired"}]}}'),
        )

    monkeypatch.setattr(qbo.urllib.request, "urlopen", fake_urlopen)
    fetch = qbo.make_qbo_query(access_token="bogus", base_url=qbo.SANDBOX_BASE, realm_id="r1")
    with pytest.raises(RuntimeError, match=r"HTTP 401.*Token expired"):
        fetch("SELECT * FROM Customer")


# ---- refresh_access_token returns (access, refresh) tuple --------------------


class _FakeResp:
    def __init__(self, payload: dict[str, Any], status: int = 200) -> None:
        self._payload = json.dumps(payload).encode()
        self.status = status

    def read(self) -> bytes:
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *args) -> None:
        return None


def test_refresh_access_token_returns_rotated_refresh_token(monkeypatch) -> None:
    """Intuit returns a new refresh_token on every refresh; we have to
    capture it so the persistence layer can write it back."""

    def fake_urlopen(req, timeout):  # noqa: ARG001
        return _FakeResp({"access_token": "AT-new", "refresh_token": "RT-new"})

    monkeypatch.setattr(qbo.urllib.request, "urlopen", fake_urlopen)
    creds = qbo.QboCreds(
        client_id="cid", client_secret="csec", refresh_token="RT-old", realm_id="r1"
    )
    access, refresh = qbo.refresh_access_token(creds)
    assert access == "AT-new"
    assert refresh == "RT-new"


def test_refresh_access_token_falls_back_to_old_when_response_omits_refresh(monkeypatch) -> None:
    """If Intuit doesn't rotate (rare but possible), keep the old token so
    persistence is a no-op rather than nulling out the secret."""

    def fake_urlopen(req, timeout):  # noqa: ARG001
        return _FakeResp({"access_token": "AT-new"})  # no refresh_token

    monkeypatch.setattr(qbo.urllib.request, "urlopen", fake_urlopen)
    creds = qbo.QboCreds(
        client_id="cid", client_secret="csec", refresh_token="RT-old", realm_id="r1"
    )
    _, refresh = qbo.refresh_access_token(creds)
    assert refresh == "RT-old"


# ---- maybe_persist_refresh_token --------------------------------------------


def test_persist_skips_when_token_unchanged(monkeypatch) -> None:
    calls: list[Any] = []
    monkeypatch.setattr(qbo, "update_github_actions_secret", lambda **kw: calls.append(kw))
    persisted = qbo.maybe_persist_refresh_token(
        new_token="same", old_token="same", repo="o/r", pat="ghp_xxx"
    )
    assert persisted is False
    assert calls == []


def test_persist_skips_when_pat_missing() -> None:
    persisted = qbo.maybe_persist_refresh_token(
        new_token="new", old_token="old", repo="o/r", pat=None
    )
    assert persisted is False


def test_persist_skips_when_repo_missing() -> None:
    persisted = qbo.maybe_persist_refresh_token(
        new_token="new", old_token="old", repo=None, pat="ghp_xxx"
    )
    assert persisted is False


def test_persist_calls_github_api_when_token_rotated(monkeypatch) -> None:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(qbo, "update_github_actions_secret", lambda **kw: calls.append(kw))
    persisted = qbo.maybe_persist_refresh_token(
        new_token="RT-new",
        old_token="RT-old",
        secret_name="QBO_SANDBOX_REFRESH_TOKEN",
        repo="hfscdogg/outgrow",
        pat="ghp_xxx",
    )
    assert persisted is True
    assert len(calls) == 1
    assert calls[0]["repo"] == "hfscdogg/outgrow"
    assert calls[0]["name"] == "QBO_SANDBOX_REFRESH_TOKEN"
    assert calls[0]["value"] == "RT-new"
    assert calls[0]["pat"] == "ghp_xxx"


def test_persist_swallows_api_failure(monkeypatch, caplog) -> None:
    """API failure must not crash the sync — the access token already
    worked, so the current run should still complete. Next run will
    surface staleness via 400 invalid_grant on its own refresh."""

    def boom(**_kw):
        raise RuntimeError("github 503")

    monkeypatch.setattr(qbo, "update_github_actions_secret", boom)
    with caplog.at_level("ERROR"):
        persisted = qbo.maybe_persist_refresh_token(
            new_token="RT-new", old_token="RT-old", repo="o/r", pat="ghp_xxx"
        )
    assert persisted is False
    assert any("Failed to persist" in rec.message for rec in caplog.records)


def test_persist_emits_gha_warning_annotation_when_pat_missing(monkeypatch, capsys) -> None:
    """Skip-on-missing-PAT must print a ::warning workflow command so the
    GH Actions UI shows the yellow annotation in the run summary.

    This is the failure mode that ate a day's pulse on May 24 — silent
    decay until invalid_grant hits 24h later. The annotation is what
    surfaces the problem before the grace expires.
    """
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    qbo.maybe_persist_refresh_token(new_token="RT-new", old_token="RT-old", repo="o/r", pat=None)
    captured = capsys.readouterr()
    assert "::warning title=QBO token rotation skipped::" in captured.out


def test_persist_emits_gha_error_annotation_on_api_failure(monkeypatch, capsys) -> None:
    monkeypatch.setenv("GITHUB_ACTIONS", "true")

    def boom(**_kw):
        raise RuntimeError("github 503")

    monkeypatch.setattr(qbo, "update_github_actions_secret", boom)
    qbo.maybe_persist_refresh_token(
        new_token="RT-new", old_token="RT-old", repo="o/r", pat="ghp_xxx"
    )
    captured = capsys.readouterr()
    assert "::error title=QBO token rotation failed::" in captured.out


def test_persist_does_not_emit_annotation_outside_github_actions(monkeypatch, capsys) -> None:
    """Local CLI / test runs shouldn't spam ::warning lines to stdout."""
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    qbo.maybe_persist_refresh_token(new_token="RT-new", old_token="RT-old", repo="o/r", pat=None)
    captured = capsys.readouterr()
    assert "::warning" not in captured.out
    assert "::error" not in captured.out


def test_persist_does_not_annotate_on_no_rotation(monkeypatch, capsys) -> None:
    """When Intuit didn't rotate, there's nothing to surface."""
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    qbo.maybe_persist_refresh_token(new_token="same", old_token="same", repo="o/r", pat="ghp_xxx")
    captured = capsys.readouterr()
    assert captured.out == ""


# ---- update_github_actions_secret --------------------------------------------


def test_update_github_secret_does_get_then_put(monkeypatch) -> None:
    """Two-step contract: GET public-key, then PUT encrypted_value+key_id."""
    real_pubkey_b64 = (
        nacl.public.PrivateKey.generate().public_key.encode(nacl.encoding.Base64Encoder).decode()
    )

    seen: list[tuple[str, str]] = []

    def fake_urlopen(req, timeout):  # noqa: ARG001
        seen.append((req.method or "", req.full_url or ""))
        if req.method == "GET":
            return _FakeResp({"key": real_pubkey_b64, "key_id": "kid-123"})
        body = json.loads(req.data)
        assert "encrypted_value" in body
        assert body["key_id"] == "kid-123"
        # value must not be the plaintext after libsodium encryption
        assert "RT-new" not in body["encrypted_value"]
        return _FakeResp({}, status=204)

    monkeypatch.setattr(qbo.urllib.request, "urlopen", fake_urlopen)
    qbo.update_github_actions_secret(
        repo="o/r",
        name="QBO_SANDBOX_REFRESH_TOKEN",
        value="RT-new",
        pat="ghp_xxx",
    )
    assert len(seen) == 2
    assert seen[0][0] == "GET"
    assert "actions/secrets/public-key" in seen[0][1]
    assert seen[1][0] == "PUT"
    assert "actions/secrets/QBO_SANDBOX_REFRESH_TOKEN" in seen[1][1]
