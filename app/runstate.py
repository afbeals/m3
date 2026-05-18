from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


class RunState:
    """Thread-safe flag tracking whether a run is currently in progress.

    The scheduler thread calls start()/stop() around each run() invocation.
    The web server thread calls snapshot() from route handlers.  All three
    methods are protected by a single lock so reads and writes are always
    consistent across threads.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._running = False
        self._started_at: datetime | None = None

    def start(self) -> None:
        with self._lock:
            if self._running:
                # Guard against double-start from concurrent /trigger/run or signal races.
                logger.warning("RunState.start() called while already running; ignoring")
                return
            self._running = True
            self._started_at = datetime.now(timezone.utc)

    def stop(self) -> None:
        with self._lock:
            self._running = False
            self._started_at = None

    def snapshot(self) -> dict:
        """Return a point-in-time view of run state.

        Keys: running (bool), started_at (ISO str | None), elapsed_seconds (int | None).
        Always safe to call from any thread without holding the lock externally.
        """
        with self._lock:
            if not self._running or self._started_at is None:
                return {"running": False, "started_at": None, "elapsed_seconds": None}
            elapsed = int((datetime.now(timezone.utc) - self._started_at).total_seconds())
            return {
                "running": True,
                "started_at": self._started_at.isoformat(),
                "elapsed_seconds": elapsed,
            }
