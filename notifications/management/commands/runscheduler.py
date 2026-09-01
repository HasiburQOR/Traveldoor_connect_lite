"""
Run the TravelDoor Connect background worker (SRS 1.2 "Scheduled jobs",
NFR-4: reminders/notifications must fire even with no admin browser open).

Usage:
    python manage.py runscheduler

Keeps running until interrupted (Ctrl+C). Every tick it:
  * auto-closes expired events                      (FR-1.4)
  * sends due visitor reminder emails               (FR-5.2)
  * raises host/admin in-app notifications          (FR-6)
  * retries failed email deliveries                 (NFR-3)
"""
import logging
import signal
import time

from apscheduler.schedulers.background import BackgroundScheduler
from django.conf import settings
from django.core.management.base import BaseCommand

from notifications.jobs import run_all_jobs

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Run background jobs (reminders, in-app notifications, auto-close, email retries)."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="Run a single tick and exit.")
        parser.add_argument("--interval", type=int, default=settings.SCHEDULER_INTERVAL_SECONDS,
                            help="Seconds between ticks (default from settings).")

    def handle(self, *args, **options):
        if options["once"]:
            results = run_all_jobs()
            self.stdout.write(self.style.SUCCESS(f"Tick complete: {results}"))
            return

        scheduler = BackgroundScheduler(timezone=settings.TIME_ZONE or "UTC")
        scheduler.add_job(
            run_all_jobs,
            trigger="interval",
            seconds=max(options["interval"], 10),
            id="traveldoor_jobs",
            max_instances=1,
            coalesce=True,
            next_run_time=None,  # run immediately on start via the manual call below
        )
        scheduler.start()
        self.stdout.write(self.style.SUCCESS(
            f"Scheduler running every {options['interval']}s — press Ctrl+C to stop."
        ))
        run_all_jobs()  # immediate first tick

        def _stop(signum, frame):
            scheduler.shutdown(wait=False)
            raise SystemExit(0)

        signal.signal(signal.SIGINT, _stop)
        try:
            while True:
                time.sleep(3600)
        except (KeyboardInterrupt, SystemExit):
            scheduler.shutdown(wait=False)
            self.stdout.write("Scheduler stopped.")
