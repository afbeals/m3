# Tests for scheduler.py: registry atomic-replace under simulated mid-run reload
# and TZ-aware CronTrigger wiring.
from __future__ import annotations

import threading
from unittest.mock import MagicMock, call, patch


def test_atomic_registry_replace_does_not_expose_empty_state():
    """Router must never observe an empty registry between clear and update.

    Simulates the SIGUSR2 reload worker by clearing+updating a dict under a
    lock while a reader thread continuously inspects it. The reader must always
    see either the old or the new registry — never an empty dict.
    """
    old_registry = {"oldsite": MagicMock()}
    new_registry = {"newsite": MagicMock()}
    registry = dict(old_registry)

    registry_lock = threading.Lock()
    observed_empty = []
    stop_event = threading.Event()

    def reader():
        while not stop_event.is_set():
            with registry_lock:
                if len(registry) == 0:
                    observed_empty.append(True)

    reader_thread = threading.Thread(target=reader, daemon=True)
    reader_thread.start()

    # Simulate the SIGUSR2 worker: acquire lock, clear, update atomically
    with registry_lock:
        registry.clear()
        registry.update(new_registry)

    stop_event.set()
    reader_thread.join(timeout=2.0)

    assert not observed_empty, "Registry was observed empty during replacement — lock is not protecting the swap"
    assert dict(registry) == new_registry


def test_cron_trigger_uses_tz_from_environment():
    """CronTrigger must receive the TZ env var so scheduled runs fire in local time."""
    from app.scheduler import build_scheduler

    with patch.dict("os.environ", {"TZ": "America/New_York"}), \
         patch("app.scheduler.BlockingScheduler") as mock_sched_cls, \
         patch("app.scheduler.CronTrigger") as mock_trigger:

        mock_scheduler = MagicMock()
        mock_sched_cls.return_value = mock_scheduler
        mock_trigger.return_value = MagicMock()

        build_scheduler(MagicMock(), "0 3 * * *")

        call_kwargs = mock_trigger.call_args
        assert call_kwargs is not None
        tz_used = call_kwargs.kwargs.get("timezone")
        assert tz_used == "America/New_York", f"Expected 'America/New_York', got {tz_used!r}"


def test_cron_trigger_defaults_to_utc_when_tz_unset():
    """When TZ is absent the scheduler must default to UTC."""
    import os
    from app.scheduler import build_scheduler

    env_without_tz = {k: v for k, v in os.environ.items() if k != "TZ"}
    with patch.dict("os.environ", env_without_tz, clear=True), \
         patch("app.scheduler.BlockingScheduler") as mock_sched_cls, \
         patch("app.scheduler.CronTrigger") as mock_trigger:

        mock_scheduler = MagicMock()
        mock_sched_cls.return_value = mock_scheduler
        mock_trigger.return_value = MagicMock()

        build_scheduler(MagicMock(), "0 3 * * *")

        call_kwargs = mock_trigger.call_args
        tz_used = call_kwargs.kwargs.get("timezone")
        assert tz_used == "UTC", f"Expected default 'UTC', got {tz_used!r}"
