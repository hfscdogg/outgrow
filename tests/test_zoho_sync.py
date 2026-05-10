"""Zoho CRM nightly read-only sync tests.

Sockets are blocked at conftest import time, so every test injects a
fake ``PageFetcher``. Live API contact would raise ``NetworkBlockedError``.
"""

from __future__ import annotations

import io
import json
import urllib.error
from pathlib import Path
from typing import Any

import pytest

from sync import zoho_crm
from sync.zoho_crm import (
    CONTACT_FIELDS,
    DEAL_FIELDS,
    ZohoCreds,
    fetch_contacts,
    fetch_deals,
    paginate,
    sync,
    write_cache,
)


def _stub_fetch(pages_by_path: dict[str, list[dict[str, Any]]]):
    """Return a ``PageFetcher`` that serves prebuilt pages per module path."""
    counters: dict[str, int] = {}

    def fetch(module_path: str, _params: dict[str, Any]) -> dict[str, Any]:
        idx = counters.get(module_path, 0)
        pages = pages_by_path[module_path]
        if idx >= len(pages):
            raise AssertionError(f"unexpected extra page request for {module_path}")
        counters[module_path] = idx + 1
        return pages[idx]

    return fetch


def test_paginate_walks_until_more_records_false() -> None:
    pages = [
        {
            "data": [{"id": "1"}, {"id": "2"}],
            "info": {"more_records": True, "next_page_token": "tok1"},
        },
        {"data": [{"id": "3"}], "info": {"more_records": False}},
    ]
    fetch = _stub_fetch({"/crm/v3/Contacts": pages})
    rows = paginate(fetch, "/crm/v3/Contacts", CONTACT_FIELDS)
    assert [r["id"] for r in rows] == ["1", "2", "3"]


def test_paginate_stops_on_first_page_when_no_more_records() -> None:
    pages = [{"data": [{"id": "x"}], "info": {"more_records": False}}]
    fetch = _stub_fetch({"/crm/v3/Deals": pages})
    rows = paginate(fetch, "/crm/v3/Deals", DEAL_FIELDS)
    assert rows == [{"id": "x"}]


def test_paginate_handles_empty_payload() -> None:
    pages = [{"data": [], "info": {"more_records": False}}]
    fetch = _stub_fetch({"/crm/v3/Contacts": pages})
    assert paginate(fetch, "/crm/v3/Contacts", CONTACT_FIELDS) == []


def test_paginate_handles_missing_info_key() -> None:
    # Real Zoho returns a 204 No Content as ``{"data": [], "info": {...}}`` via
    # ``make_zoho_get``; defend against a malformed payload that omits ``info``.
    pages = [{"data": [{"id": "1"}]}]
    fetch = _stub_fetch({"/crm/v3/Contacts": pages})
    assert paginate(fetch, "/crm/v3/Contacts", CONTACT_FIELDS) == [{"id": "1"}]


def test_paginate_aborts_when_more_records_never_flips() -> None:
    pages = [
        {"data": [{"id": str(i)}], "info": {"more_records": True, "next_page_token": f"tok{i}"}}
        for i in range(5)
    ]
    fetch = _stub_fetch({"/crm/v3/Contacts": pages})
    with pytest.raises(RuntimeError, match="exceeded 3 pages"):
        paginate(fetch, "/crm/v3/Contacts", CONTACT_FIELDS, max_pages=3)


def test_paginate_first_request_omits_page_token() -> None:
    seen: list[dict[str, Any]] = []

    def fetch(module_path: str, params: dict[str, Any]) -> dict[str, Any]:
        seen.append(params)
        return {"data": [], "info": {"more_records": False}}

    paginate(fetch, "/crm/v3/Contacts", CONTACT_FIELDS, per_page=50)
    assert seen == [{"fields": CONTACT_FIELDS, "per_page": 50}]
    assert "page_token" not in seen[0]
    assert "page" not in seen[0]  # offset pagination caps at 2000 — must use token


def test_paginate_threads_next_page_token_through_subsequent_requests() -> None:
    """Regression for HTTP 400 DISCRETE_PAGINATION_LIMIT_EXCEEDED. Zoho caps
    offset pagination (`page=N`) at 2000 records. We use cursor pagination via
    `next_page_token` -> `page_token` instead, which is unlimited."""
    seen: list[dict[str, Any]] = []
    pages = [
        {"data": [{"id": "1"}], "info": {"more_records": True, "next_page_token": "tok-a"}},
        {"data": [{"id": "2"}], "info": {"more_records": True, "next_page_token": "tok-b"}},
        {"data": [{"id": "3"}], "info": {"more_records": False}},
    ]
    counter = [0]

    def fetch(module_path: str, params: dict[str, Any]) -> dict[str, Any]:
        seen.append(params)
        idx = counter[0]
        counter[0] += 1
        return pages[idx]

    rows = paginate(fetch, "/crm/v3/Contacts", CONTACT_FIELDS, per_page=200)
    assert [r["id"] for r in rows] == ["1", "2", "3"]
    assert "page_token" not in seen[0]
    assert seen[1]["page_token"] == "tok-a"
    assert seen[2]["page_token"] == "tok-b"
    assert all("page" not in p for p in seen)


def test_paginate_stops_when_more_records_true_but_no_token() -> None:
    """Defensive: malformed Zoho response with more_records=true and no
    next_page_token should stop with rows-so-far rather than spin forever."""
    pages = [
        {"data": [{"id": "1"}], "info": {"more_records": True}},
    ]
    fetch = _stub_fetch({"/crm/v3/Contacts": pages})
    rows = paginate(fetch, "/crm/v3/Contacts", CONTACT_FIELDS)
    assert rows == [{"id": "1"}]


def test_fetch_contacts_uses_contacts_module() -> None:
    fetch = _stub_fetch(
        {"/crm/v3/Contacts": [{"data": [{"id": "c1"}], "info": {"more_records": False}}]}
    )
    assert fetch_contacts(fetch) == [{"id": "c1"}]


def test_fetch_deals_uses_deals_module() -> None:
    fetch = _stub_fetch(
        {"/crm/v3/Deals": [{"data": [{"id": "d1"}], "info": {"more_records": False}}]}
    )
    assert fetch_deals(fetch) == [{"id": "d1"}]


def test_write_cache_creates_parent_and_returns_count(tmp_path: Path) -> None:
    out = tmp_path / "nested" / "contacts.json"
    n = write_cache([{"id": "1"}, {"id": "2"}], out)
    assert n == 2
    payload = json.loads(out.read_text())
    assert payload == [{"id": "1"}, {"id": "2"}]


def test_write_cache_writes_sorted_keys_for_stable_diffs(tmp_path: Path) -> None:
    out = tmp_path / "contacts.json"
    write_cache([{"b": 2, "a": 1}], out)
    raw = out.read_text()
    assert raw.index('"a"') < raw.index('"b"')


def test_sync_writes_contacts_deals_and_meta(tmp_path: Path) -> None:
    pages_by_path = {
        "/crm/v3/Contacts": [
            {"data": [{"id": "c1"}, {"id": "c2"}], "info": {"more_records": False}},
        ],
        "/crm/v3/Deals": [
            {"data": [{"id": "d1"}], "info": {"more_records": False}},
        ],
    }
    fetch = _stub_fetch(pages_by_path)
    meta = sync(fetch, cache_dir=tmp_path, now_iso="2026-05-06T11:00:00+00:00")

    assert meta == {
        "synced_at": "2026-05-06T11:00:00+00:00",
        "contact_count": 2,
        "deal_count": 1,
    }
    assert json.loads((tmp_path / "contacts.json").read_text()) == [
        {"id": "c1"},
        {"id": "c2"},
    ]
    assert json.loads((tmp_path / "deals.json").read_text()) == [{"id": "d1"}]
    assert json.loads((tmp_path / "_meta.json").read_text()) == meta


def test_sync_creates_cache_dir_if_missing(tmp_path: Path) -> None:
    target = tmp_path / "fresh" / "zoho"
    fetch = _stub_fetch(
        {
            "/crm/v3/Contacts": [{"data": [], "info": {"more_records": False}}],
            "/crm/v3/Deals": [{"data": [], "info": {"more_records": False}}],
        }
    )
    sync(fetch, cache_dir=target, now_iso="2026-05-06T11:00:00+00:00")
    assert (target / "contacts.json").exists()
    assert (target / "deals.json").exists()
    assert (target / "_meta.json").exists()


def test_zoho_creds_from_env_returns_creds_with_default_dc() -> None:
    env = {
        "ZOHO_CRM_REFRESH_TOKEN": "rt",
        "ZOHO_CRM_CLIENT_ID": "cid",
        "ZOHO_CRM_CLIENT_SECRET": "csec",
    }
    creds = ZohoCreds.from_env(env)
    assert creds.refresh_token == "rt"
    assert creds.client_id == "cid"
    assert creds.client_secret == "csec"
    assert creds.dc == "com"


def test_zoho_creds_from_env_honors_eu_dc() -> None:
    env = {
        "ZOHO_CRM_REFRESH_TOKEN": "rt",
        "ZOHO_CRM_CLIENT_ID": "cid",
        "ZOHO_CRM_CLIENT_SECRET": "csec",
        "ZOHO_CRM_DC": "eu",
    }
    assert ZohoCreds.from_env(env).dc == "eu"


def test_zoho_creds_from_env_raises_on_missing() -> None:
    env = {"ZOHO_CRM_REFRESH_TOKEN": "rt"}
    with pytest.raises(RuntimeError, match="ZOHO_CRM_CLIENT_ID"):
        ZohoCreds.from_env(env)


def test_zoho_creds_from_env_treats_empty_string_as_missing() -> None:
    env = {
        "ZOHO_CRM_REFRESH_TOKEN": "rt",
        "ZOHO_CRM_CLIENT_ID": "",
        "ZOHO_CRM_CLIENT_SECRET": "csec",
    }
    with pytest.raises(RuntimeError, match="ZOHO_CRM_CLIENT_ID"):
        ZohoCreds.from_env(env)


def test_refresh_access_token_surfaces_oauth_400_body(monkeypatch) -> None:
    """When Zoho returns 400 (invalid_code etc.), the body has to land in
    the RuntimeError message — otherwise debugging is `urllib.error.HTTPError`
    with no context."""

    def fake_urlopen(req, timeout):  # noqa: ARG001
        raise urllib.error.HTTPError(
            url=req.full_url,
            code=400,
            msg="Bad Request",
            hdrs=None,  # type: ignore[arg-type]
            fp=io.BytesIO(b'{"error":"invalid_code"}'),
        )

    monkeypatch.setattr(zoho_crm.urllib.request, "urlopen", fake_urlopen)
    creds = zoho_crm.ZohoCreds(refresh_token="rt", client_id="cid", client_secret="csec", dc="com")
    with pytest.raises(RuntimeError, match=r"HTTP 400.*invalid_code"):
        zoho_crm.refresh_access_token(creds)


def test_make_zoho_get_surfaces_api_error_body(monkeypatch) -> None:
    """Same defensive treatment for CRM data calls so a 401 unauthorised /
    403 scope-mismatch surfaces the response body."""

    def fake_urlopen(req, timeout):  # noqa: ARG001
        raise urllib.error.HTTPError(
            url=req.full_url,
            code=401,
            msg="Unauthorized",
            hdrs=None,  # type: ignore[arg-type]
            fp=io.BytesIO(b'{"code":"INVALID_TOKEN","status":"error"}'),
        )

    monkeypatch.setattr(zoho_crm.urllib.request, "urlopen", fake_urlopen)
    fetch = zoho_crm.make_zoho_get(access_token="bogus", dc="com")
    with pytest.raises(RuntimeError, match=r"HTTP 401.*INVALID_TOKEN"):
        fetch("/crm/v3/Contacts", {"per_page": 1})


def test_zoho_creds_from_env_treats_empty_dc_as_default() -> None:
    """GitHub Actions interpolates an unset `vars.ZOHO_CRM_DC` as an empty
    string rather than omitting the env var. Empty string must fall back
    to the ``com`` default — otherwise the OAuth URL becomes
    ``https://accounts.zoho./oauth/v2/token`` and DNS resolution fails
    with `[Errno -2] Name or service not known`."""
    env = {
        "ZOHO_CRM_REFRESH_TOKEN": "rt",
        "ZOHO_CRM_CLIENT_ID": "cid",
        "ZOHO_CRM_CLIENT_SECRET": "csec",
        "ZOHO_CRM_DC": "",
    }
    assert ZohoCreds.from_env(env).dc == "com"
