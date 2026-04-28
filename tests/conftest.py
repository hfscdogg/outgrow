"""Block all socket access during tests.

Phase 0 has no live integrations; every external call should raise
`NotImplementedError`. We harden the contract by replacing the entire
``socket`` module's primary connection primitives at conftest import
time. Any test that accidentally tries to open a socket fails loudly.
"""

from __future__ import annotations

import socket


class NetworkBlockedError(RuntimeError):
    """Raised when test code attempts to open a socket."""


def _refuse(*_args: object, **_kwargs: object) -> socket.socket:
    raise NetworkBlockedError(
        "network access is disabled in tests; integrations must raise "
        "NotImplementedError until Phase 1+ credentials land."
    )


socket.socket = _refuse  # type: ignore[assignment]
socket.create_connection = _refuse  # type: ignore[assignment]
