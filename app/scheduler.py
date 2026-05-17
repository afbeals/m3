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
#     docker exec pm kill -USR1 1
# -----------------------------------------------------------------------------

import logging
import platform
import signal
import threading

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

logger = logging.getLogger(__name__)


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
    if len(parts) != 5:
        raise ValueError(f"RUN_SCHEDULE must be a 5-field cron expression, got: {schedule!r}")

    minute, hour, day, month, day_of_week = parts
    trigger = CronTrigger(
        minute=minute,
        hour=hour,
        day=day,
        month=month,
        day_of_week=day_of_week,
    )

    scheduler.add_job(
        run_fn,
        trigger=trigger,
        id="scheduled_run",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
    )

    # SIGUSR1 is not available on Windows — only register the handler on Unix systems.
    if platform.system() != "Windows":
        _trigger_event = threading.Event()

        def _handle_sigusr1(signum, frame):
            _trigger_event.set()

        def _watcher():
            while True:
                _trigger_event.wait()
                _trigger_event.clear()
                logger.info("SIGUSR1 received — scheduling immediate run")
                try:
                    scheduler.add_job(run_fn, id="sigusr1_trigger", replace_existing=True)
                except Exception as exc:
                    logger.warning("SIGUSR1: could not schedule run: %s", exc)

        watcher_thread = threading.Thread(target=_watcher, daemon=True)
        watcher_thread.start()

        signal.signal(signal.SIGUSR1, _handle_sigusr1)
        logger.debug("SIGUSR1 handler registered (Unix only)")
    else:
        logger.debug("Skipping SIGUSR1 handler (not supported on Windows)")

    return scheduler


def start_scheduler(run_fn, schedule: str) -> None:
    """
    Build and start a blocking scheduler. Blocks until the container is stopped.
    Convenience wrapper around build_scheduler() for callers that don't need
    a reference to the scheduler (e.g. when the web UI is disabled).
    """
    scheduler = build_scheduler(run_fn, schedule)
    logger.info("Scheduler started. Next run scheduled via: %s", schedule)
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Scheduler stopped")
