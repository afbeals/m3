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

    When timeout_secs is 0 or negative, fn() is called directly with no timeout (waits indefinitely for fn() to return).
    """
    # When timeout_secs is 0 or negative, call fn() directly in the calling thread (no timeout).
    if timeout_secs <= 0:
        return fn()

    result_holder: list = []
    exc_holder: list = []

    def _worker():
        try:
            result_holder.append(fn())
        except BaseException as exc:
            if not isinstance(exc, Exception):
                logger.critical(
                    "call_with_timeout: worker caught non-Exception BaseException %r in %r",
                    exc, getattr(fn, '__name__', repr(fn)),
                )
            exc_holder.append(exc)

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    t.join(timeout=timeout_secs)
    if t.is_alive():
        logger.warning(
            "call_with_timeout: function %r timed out after %ss — worker thread will continue running",
            getattr(fn, '__name__', repr(fn)), timeout_secs,
        )
        raise TimeoutError(f"{description} exceeded {timeout_secs:.0f}s")
    if exc_holder:
        raise exc_holder[0]
    if not result_holder:
        # This should never happen — means fn() returned without a result or exception
        raise RuntimeError(
            f"call_with_timeout: worker thread completed without a result or exception "
            f"(timeout_secs={timeout_secs!r}). This is a bug."
        )
    return result_holder[0]


def retry_with_backoff(
    fn: Callable[[], T],
    *,
    max_attempts: int,
    backoff_base: float,
    max_wait: float = 60.0,
    description: str = "operation",
    reraise_on: tuple[type[Exception], ...] = (),
) -> T:
    """
    Call fn() up to max_attempts times, sleeping backoff_base**attempt seconds
    between failures (capped at max_wait seconds).  Returns the first successful
    result.  Re-raises the last exception if all attempts are exhausted.

    reraise_on: tuple of exception types to re-raise immediately without retrying.
    Subclasses of types in reraise_on are also re-raised (Python isinstance semantics).

    max_wait: maximum number of seconds to sleep between attempts (default 60.0).
    The exponential backoff is clamped to this value so very large backoff_base
    values or many attempts don't produce unbounded sleep times.

    Note: uses time.sleep() which blocks the calling thread. This is intentional
    for m3's single-threaded run() path. Do not call from async code.
    """
    if max_attempts < 1:
        raise ValueError(f"max_attempts must be >= 1, got {max_attempts!r}")
    if backoff_base < 0:
        raise ValueError(f"backoff_base must be >= 0, got {backoff_base!r}")
    last_exc: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return fn()
        except reraise_on:
            raise
        except Exception as exc:
            last_exc = exc
            if attempt < max_attempts:
                wait = max(min(backoff_base ** attempt, max_wait), 0.1)
                logger.warning(
                    "%s attempt %d/%d failed (%s); retrying in %.0fs",
                    description, attempt, max_attempts, exc, wait,
                )
                time.sleep(wait)
    assert last_exc is not None, "retry loop completed without capturing an exception (this is a bug)"
    raise last_exc


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
