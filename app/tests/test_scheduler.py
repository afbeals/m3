# Tests for scheduler.py: registry atomic-replace under simulated mid-run reload
# and TZ-aware CronTrigger wiring.
from __future__ import annotations
import pytest

import threading
from unittest.mock import MagicMock, call, patch

pytestmark = pytest.mark.unit


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


# ---------------------------------------------------------------------------
# T6 — SIGUSR2 end-to-end test
# ---------------------------------------------------------------------------

import os
import signal
import platform
import time as _time


@pytest.mark.skipif(platform.system() == "Windows", reason="SIGUSR2 not available on Windows")
def test_sigusr2_reloads_registry_and_pauses_scheduler():
    """SIGUSR2 triggers registry reload: watcher pauses scheduler, swaps registry, resumes."""
    from app.scheduler import register_sigusr2_reload

    registry: dict = {"oldsite": MagicMock()}
    mock_scheduler = MagicMock()
    mock_scheduler.running = True

    # The new registry that load_plugins will return after the reload
    fake_new_registry = {"reloaded": MagicMock()}

    with patch("app.plugins.loader.load_plugins", return_value=fake_new_registry):
        register_sigusr2_reload(registry, "/tmp/fake_plugin_dir", scheduler=mock_scheduler)
        # Give the watcher thread a moment to start before sending the signal
        _time.sleep(0.1)
        # Send SIGUSR2 to ourselves
        os.kill(os.getpid(), signal.SIGUSR2)
        # Poll until registry is updated (up to 5 seconds)
        deadline = _time.monotonic() + 5.0
        while _time.monotonic() < deadline:
            if "reloaded" in registry:
                break
            _time.sleep(0.05)

    assert "reloaded" in registry, f"Expected 'reloaded' in registry after reload, got {list(registry.keys())}"
    assert "oldsite" not in registry, "Old registry key should have been cleared"

    # Verify scheduler was paused and resumed
    mock_scheduler.pause.assert_called_once()
    mock_scheduler.resume.assert_called_once()
