# app/utils.py
#
# Small shared utilities used across multiple modules.

from __future__ import annotations

import logging
import os
import sys
import threading
import time
from typing import Callable, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


def call_with_timeout(fn: Callable[[], T], timeout_secs: float, description: str = "call") -> T:
    """Call fn() in a daemon thread; raise TimeoutError if it doesn't return within timeout_secs.

    The thread is not killed on timeout (Python limitation) — it continues until fn()'s own
    I/O timeout fires or it returns. The timeout is a *reporting* boundary, not a kill signal.
    Raises the exception from fn() directly if it throws before the timeout expires.
    """
    result_holder: list = []
    exc_holder: list = []

    def _worker():
        try:
            result_holder.append(fn())
        except Exception as exc:
            exc_holder.append(exc)

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    t.join(timeout=timeout_secs)
    if t.is_alive():
        raise TimeoutError(f"{description} exceeded {timeout_secs:.0f}s")
    if exc_holder:
        raise exc_holder[0]
    return result_holder[0] if result_holder else None  # type: ignore[return-value]


def retry_with_backoff(
    fn: Callable[[], T],
    *,
    max_attempts: int,
    backoff_base: float,
    description: str = "operation",
    reraise_on: tuple[type[Exception], ...] = (),
) -> T:
    """
    Call fn() up to max_attempts times, sleeping backoff_base**attempt seconds
    between failures.  Returns the first successful result.  Re-raises the last
    exception if all attempts are exhausted.

    reraise_on: tuple of exception types to re-raise immediately without retrying.
    Subclasses of types in reraise_on are also re-raised (Python isinstance semantics).

    Note: uses time.sleep() which blocks the calling thread. This is intentional
    for m3's single-threaded run() path. Do not call from async code.
    """
    last_exc: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return fn()
        except reraise_on:
            raise
        except Exception as exc:
            last_exc = exc
            if attempt < max_attempts:
                wait = backoff_base ** attempt
                logger.warning(
                    "%s attempt %d/%d failed (%s); retrying in %.0fs",
                    description, attempt, max_attempts, exc, wait,
                )
                time.sleep(wait)
    raise last_exc  # type: ignore[misc]  # always set after at least one attempt


def atomic_replace(src: str, dst: str) -> None:
    """Rename src to dst atomically, with a Windows-safe retry on PermissionError.

    On Linux/macOS os.replace() is always atomic and never raises PermissionError
    for this use case — the retry loop adds zero overhead on those platforms.

    On Windows, os.replace() raises PermissionError (WinError 32) when another
    process (e.g. Plex scanner) has the destination file open. Retrying with short
    exponential backoff recovers transparently in most cases.
    """
    for attempt in range(5):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if sys.platform != "win32" or attempt == 4:
                raise
            time.sleep(0.05 * (2 ** attempt))
