"""Tiny retry/backoff helper for transient broker errors (429s, resets).

Retries only on errors that look transient (rate limits, timeouts,
temporary connection issues); re-raises everything else immediately so
real problems (bad creds, rejected orders) fail fast.
"""
from __future__ import annotations

import time
from typing import Callable, TypeVar

T = TypeVar("T")

_TRANSIENT_MARKERS = (
    "429", "rate limit", "too many requests", "timeout", "timed out",
    "temporarily", "503", "502", "connection reset", "connection aborted",
)


def is_transient(err: Exception) -> bool:
    msg = str(err).lower()
    return any(m in msg for m in _TRANSIENT_MARKERS)


def with_retry(fn: Callable[[], T], *, attempts: int = 3, base_delay: float = 0.5, sleep=time.sleep) -> T:
    """Call fn(), retrying transient failures with exponential backoff."""
    last: Exception | None = None
    for i in range(attempts):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001 — re-raised below if not transient/last
            last = e
            if not is_transient(e) or i == attempts - 1:
                raise
            sleep(base_delay * (2 ** i))
    assert last is not None
    raise last
