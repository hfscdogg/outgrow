"""Shared HTTP helper for sync modules: retry-with-backoff on transient errors.

Intuit's QBO API has been intermittently slow at scale — single SSL reads
during pagination can stall past urllib's read timeout, killing the whole
sync. Wraps ``urlopen`` + ``resp.read()`` in a retry loop so one flaky
response doesn't take the workflow down.

Retries only on transport-level failures (``TimeoutError`` and
``urllib.error.URLError`` excluding its ``HTTPError`` subclass). Lets
``HTTPError`` propagate so callers' existing ``except HTTPError`` blocks
still surface the server's response body.

Idempotency: every QBO + Zoho call site that uses this helper is a GET or
a token-refresh POST. GETs are read-only; refresh-POSTs hit Intuit's /
Zoho's OAuth provider, which tolerates retry within the rotated-token
24h grace period. No data-mutating API call uses this helper.
"""

from __future__ import annotations

import time
import urllib.error
import urllib.request
from collections.abc import Callable

DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_BACKOFF_SECONDS: tuple[float, ...] = (2.0, 4.0)


def urlopen_read_with_retry(
    req: urllib.request.Request,
    *,
    timeout: float,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    backoff_seconds: tuple[float, ...] = DEFAULT_BACKOFF_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[int, bytes]:
    """Open ``req``, read its body, return ``(status, body_bytes)``.

    Retries the whole open+read on ``TimeoutError`` and non-``HTTPError``
    ``URLError`` up to ``max_attempts`` times, sleeping per
    ``backoff_seconds`` between attempts (last entry repeats if attempts
    > len(backoff)). Lets ``HTTPError`` propagate unchanged.
    """
    last_exc: BaseException | None = None
    for attempt in range(max_attempts):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError:
            raise
        except (TimeoutError, urllib.error.URLError) as exc:
            last_exc = exc
            if attempt + 1 >= max_attempts:
                break
            delay = backoff_seconds[min(attempt, len(backoff_seconds) - 1)]
            sleep(delay)
    assert last_exc is not None  # noqa: S101  -- loop exits only via break or return
    raise last_exc
