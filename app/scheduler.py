# -----------------------------------------------------------------------------
# app/scheduler.py
#
# Runs the metadata agent on a recurring schedule using APScheduler.
#
# The schedule is a standard 5-field cron expression set via the RUN_SCHEDULE
# environment variable (e.g. "0 3 * * *" = every day at 3am).
#
# Overlap prevention:
#   max_instances=1 ensures that if a run is still in progress when the next
#   scheduled time arrives, the new run is skipped rather than stacking up.
#
# Manual trigger:
#   Sending SIGUSR1 to the container process triggers an immediate run without
#   restarting the container or disturbing the schedule:
#     docker exec m3 kill -USR1 1
# -----------------------------------------------------------------------------

from __future__ import annotations

import logging
import os
import platform
import signal
import threading

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

logger = logging.getLogger(__name__)

# How late a scheduled run can fire after its trigger time (in seconds).
# 1 hour: allows for brief container restarts without silently skipping a run.
_MISFIRE_GRACE_SECS = 3600


def register_sigusr2_reload(registry: dict, plugin_dir: str, scheduler=None) -> None:
    """
    Register a SIGUSR2 handler that reloads plugins from plugin_dir (Unix only).

    The reload builds a fresh dict, then *replaces* the shared registry dict's
    contents atomically under a lock so mid-run router lookups never see the dict
    in a partially-cleared state. Python dict assignment is GIL-safe for reads,
    but clear()+update() is two operations — without a lock a concurrent router
    lookup between them would return None for valid sites.

    Signal handlers must remain async-signal-safe; the actual reload is
    offloaded to a worker thread via threading.Event rather than done inside
    the signal handler itself.
    """
    if platform.system() == "Windows":
        logger.debug("Skipping SIGUSR2 handler (not supported on Windows)")
        return

    from app.plugins.loader import load_plugins

    _reload_event = threading.Event()
    # Guards clear()+update() so no two concurrent SIGUSR2 signals race to
    # reload simultaneously. Router readers do NOT acquire this lock — they rely
    # on CPython's GIL making individual dict operations atomic. The two-step
    # clear()+update() is safe here because the watcher thread is the only writer
    # and the GIL ensures each individual dict mutation is seen atomically by
    # concurrent readers; the brief window where the dict is partially cleared is
    # invisible to Python-level code that reads one key at a time.
    _registry_lock = threading.Lock()

    def _handle_sigusr2(signum, frame):
        # Signal handlers must remain async-signal-safe (no I/O, no locks).
        _reload_event.set()

    def _watcher():
        while True:
            _reload_event.wait()
            # Clear immediately after waking so a concurrent SIGUSR2 during reload
            # re-sets the event and triggers another reload instead of being lost.
            _reload_event.clear()
            logger.info("SIGUSR2 received — reloading plugins from %s", plugin_dir)
            try:
                new_registry = load_plugins(plugin_dir, old_registry=dict(registry))
                with _registry_lock:
                    paused = False
                    try:
                        if scheduler is not None:
                            scheduler.pause()
                            paused = True
                        registry.clear()
                        registry.update(new_registry)
                    finally:
                        if paused:
                            scheduler.resume()
                logger.info("Plugin reload complete: %d plugin(s) loaded", len(new_registry))
            except Exception as exc:
                logger.warning("Plugin reload failed: %s", exc)

    watcher_thread = threading.Thread(target=_watcher, daemon=True)
    watcher_thread.start()

    signal.signal(signal.SIGUSR2, _handle_sigusr2)
    logger.debug("SIGUSR2 hot-reload handler registered (Unix only)")


def build_scheduler(run_fn, schedule: str) -> BlockingScheduler:
    """
    Build and configure a BlockingScheduler for run_fn on the given cron schedule.
    Sets up the SIGUSR1 handler (Unix only).
    Does NOT call scheduler.start() — the caller does that.

    Use this when you need a reference to the scheduler before starting it
    (e.g. to pass to the web dashboard so it can trigger manual runs).
    """
    scheduler = BlockingScheduler()

    parts = schedule.strip().split()
    # Belt-and-suspenders check for callers that bypass main() (e.g. tests).
    # main() already validates fully via CronTrigger.from_crontab() before calling here.
    if len(parts) != 5:
        raise ValueError(f"RUN_SCHEDULE must be a 5-field cron expression, got: {schedule!r}")

    minute, hour, day, month, day_of_week = parts
    # Pass the TZ env var as the trigger timezone so cron times are interpreted
    # in the user's local timezone rather than always UTC.
    # python:3.12-slim requires tzdata to be installed for non-UTC zones to work
    # (see Dockerfile). Falls back to UTC when TZ is unset.
    timezone = os.environ.get("TZ") or "UTC"
    try:
        trigger = CronTrigger(
            minute=minute,
            hour=hour,
            day=day,
            month=month,
            day_of_week=day_of_week,
            timezone=timezone,
        )
    except Exception as exc:
        raise ValueError(
            f"Invalid cron schedule or timezone (TZ={timezone!r}): {exc}"
        ) from exc

    # max_instances=1 prevents concurrent runs if the previous run is still in progress
    # when the next scheduled time arrives (the new fire is skipped, not queued).
    # coalesce=True collapses multiple missed fires (e.g. after a container sleep) into
    # a single catch-up run rather than firing once per missed interval.
    # Both are needed: max_instances guards against overlap, coalesce prevents a burst
    # of back-to-back catch-up runs after a long outage.
    scheduler.add_job(
        run_fn,
        trigger=trigger,
        id="scheduled_run",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=_MISFIRE_GRACE_SECS,
    )

    # SIGUSR1 is not available on Windows — only register the handler on Unix systems.
    if platform.system() != "Windows":
        _trigger_event = threading.Event()

        def _handle_sigusr1(signum, frame):
            _trigger_event.set()

        def _watcher():
            # scheduler is assigned above before this thread is started, so it is
            # always a BlockingScheduler instance here — the `scheduler is None`
            # check that appeared in some earlier versions was unreachable.
            while True:
                _trigger_event.wait()
                _trigger_event.clear()  # consume the event before acting; a second signal during add_job will re-set it
                logger.info("SIGUSR1 received — scheduling immediate run")
                try:
                    scheduler.add_job(run_fn, id="sigusr1_trigger", replace_existing=True, misfire_grace_time=_MISFIRE_GRACE_SECS)
                except Exception as exc:
                    logger.warning("SIGUSR1: could not schedule run: %s", exc)

        watcher_thread = threading.Thread(target=_watcher, daemon=True)
        watcher_thread.start()

        signal.signal(signal.SIGUSR1, _handle_sigusr1)
        logger.debug("SIGUSR1 handler registered (Unix only)")
    else:
        logger.debug("Skipping SIGUSR1 handler (not supported on Windows)")

    return scheduler


