# app/utils.py
#
# Small shared utilities used across multiple modules.

from __future__ import annotations

import logging
import time
from typing import Callable, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


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
    for pm's single-threaded run() path. Do not call from async code.
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
