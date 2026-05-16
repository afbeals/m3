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

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

logger = logging.getLogger(__name__)


def start_scheduler(run_fn, schedule: str) -> None:
    """
    Start a blocking scheduler that calls run_fn on the given cron schedule.
    This function blocks indefinitely until the container is stopped.
    """
    scheduler = BlockingScheduler()

    # Split the 5-field cron expression into its component parts
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
        max_instances=1,       # never run two instances at the same time
        coalesce=True,         # if multiple triggers fire while paused, run once not many
        misfire_grace_time=3600,  # if the scheduler wakes up late, still run if within 1 hour
    )

    # SIGUSR1 is not available on Windows — only register the handler on Unix systems.
    # On Windows, use `--once` via `docker exec` to trigger a manual run instead.
    if platform.system() != "Windows":
        def _handle_sigusr1(signum, frame):
            logger.info("Received SIGUSR1 — triggering immediate run")
            run_fn()

        signal.signal(signal.SIGUSR1, _handle_sigusr1)
        logger.debug("SIGUSR1 handler registered (Unix only)")
    else:
        logger.debug("Skipping SIGUSR1 handler (not supported on Windows)")

    logger.info("Scheduler started. Next run scheduled via: %s", schedule)
    try:
        # Blocks here until KeyboardInterrupt (Ctrl+C) or container stop signal
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Scheduler stopped")
