"""Tests for ``sync/_http.py`` retry-with-backoff helper.

Validates the helper retries on transport errors (``TimeoutError``,
non-``HTTPError`` ``URLError``) but propagates ``HTTPError`` so callers'
existing ``except HTTPError`` paths still surface server response bodies.

No live sockets — ``urlopen`` is monkeypatched per test. The conftest's
socket guard would catch a regression anyway.
"""

from __future__ import annotations

import io
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

import pytest

from sync._http import urlopen_read_with_retry


class _FakeResponse:
    """Minimal stand-in for ``http.client.HTTPResponse`` as a context manager."""

    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self._body = io.BytesIO(body)

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body.read()


def _fake_urlopen_factory(behaviors: list[Any]) -> Callable[..., _FakeResponse]:
    """Build a fake urlopen that pops one behavior per call.

    Each behavior is either an exception instance (raised) or a
    ``_FakeResponse`` (returned).
    """
    calls: list[int] = []

    def fake_urlopen(*_args: object, **_kwargs: object) -> _FakeResponse:
        calls.append(1)
        nxt = behaviors.pop(0)
        if isinstance(nxt, BaseException):
            raise nxt
        return nxt

    fake_urlopen.calls = calls  # type: ignore[attr-defined]
    return fake_urlopen


def _no_sleep(_: float) -> None:
    return None


def _req() -> urllib.request.Request:
    return urllib.request.Request("http://example.invalid/test")


def test_returns_immediately_on_first_success(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _fake_urlopen_factory([_FakeResponse(200, b'{"ok": true}')])
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    status, body = urlopen_read_with_retry(_req(), timeout=5.0, sleep=_no_sleep)
    assert status == 200
    assert body == b'{"ok": true}'
    assert len(fake.calls) == 1  # type: ignore[attr-defined]


def test_retries_on_timeout_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _fake_urlopen_factory(
        [
            TimeoutError("The read operation timed out"),
            TimeoutError("The read operation timed out"),
            _FakeResponse(200, b'{"ok": true}'),
        ]
    )
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    sleeps: list[float] = []
    status, body = urlopen_read_with_retry(
        _req(),
        timeout=5.0,
        sleep=sleeps.append,
    )
    assert status == 200
    assert body == b'{"ok": true}'
    assert len(fake.calls) == 3  # type: ignore[attr-defined]
    assert sleeps == [2.0, 4.0]  # default backoff between the three attempts


def test_retries_on_urlerror_but_not_httperror(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _fake_urlopen_factory(
        [
            urllib.error.URLError("connection reset"),
            _FakeResponse(200, b"ok"),
        ]
    )
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    status, body = urlopen_read_with_retry(_req(), timeout=5.0, sleep=_no_sleep)
    assert status == 200
    assert body == b"ok"
    assert len(fake.calls) == 2  # type: ignore[attr-defined]


def test_httperror_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    """HTTPError = server gave a real response; caller's except-HTTPError
    needs the original exception to surface response body.
    """
    http_err = urllib.error.HTTPError(
        url="http://example.invalid/test",
        code=500,
        msg="Server Error",
        hdrs=None,  # type: ignore[arg-type]
        fp=io.BytesIO(b'{"error": "boom"}'),
    )
    fake = _fake_urlopen_factory([http_err])
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urlopen_read_with_retry(_req(), timeout=5.0, sleep=_no_sleep)
    assert exc_info.value.code == 500
    assert len(fake.calls) == 1  # type: ignore[attr-defined]  -- no retry attempted


def test_raises_last_exception_when_all_attempts_exhausted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _fake_urlopen_factory(
        [
            TimeoutError("attempt 1"),
            TimeoutError("attempt 2"),
            TimeoutError("attempt 3"),
        ]
    )
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    with pytest.raises(TimeoutError, match="attempt 3"):
        urlopen_read_with_retry(_req(), timeout=5.0, sleep=_no_sleep)
    assert len(fake.calls) == 3  # type: ignore[attr-defined]


def test_custom_max_attempts_and_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _fake_urlopen_factory([TimeoutError("x"), TimeoutError("x"), _FakeResponse(204, b"")])
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    sleeps: list[float] = []
    status, body = urlopen_read_with_retry(
        _req(),
        timeout=5.0,
        max_attempts=5,
        backoff_seconds=(0.1, 0.2, 0.4),
        sleep=sleeps.append,
    )
    assert status == 204
    assert body == b""
    assert sleeps == [0.1, 0.2]
