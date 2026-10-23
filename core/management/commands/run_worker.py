"""Run the detection worker: the scheduler that drives the detection cycle (Figure 8)."""
import logging

from apscheduler.schedulers.blocking import BlockingScheduler
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from core.config import ConfigError, load_config
from core.pipeline.cycle import DetectionCycle
from core.pipeline.jobs import JobError, process_pending
from core.services import autolearn
from core.services import notify as notify_service
from core.services import reports as reports_service
from core.services.prometheus import PrometheusError
from core.services.settings_store import effective_config, make_client

log = logging.getLogger(__name__)


class Worker:
    """Holds the file configuration and applies settings changed in the web interface each tick."""

    def __init__(self, file_cfg: dict):
        self.file_cfg = file_cfg
        self.cycle = None

    def current(self):
        cfg = effective_config(self.file_cfg)
        return cfg, make_client(cfg)

    def tick(self):
        cfg, client = self.current()
        if self.cycle is None:
            self.cycle = DetectionCycle(cfg, client)
        self.cycle.cfg, self.cycle.client = cfg, client
        return self.cycle.run()

    def jobs(self):
        cfg, client = self.current()
        return process_pending(cfg, client)

    def reports(self):
        """Write the daily, weekly and monthly reports for every finished period that does not have one yet."""
        try:
            made = reports_service.generate_due()
            if made:
                log.info("Reports written: %s", ", ".join(str(r) for r in made))
        except Exception:  # noqa: BLE001 - never stop the worker
            log.exception("Report generation failed.")

    def notify(self):
        """Email what is new (see services/notify). A mail problem is logged and never reaches detection."""
        try:
            notify_service.sweep()
        except Exception:  # noqa: BLE001 - never stop the worker
            log.exception("Email alerts failed.")

    def learn(self):
        """Zero-touch learning: decide whether to learn now (first model, better coverage, or weekly refresh)."""
        cfg, client = self.current()
        try:
            state = autolearn.step(cfg, client)
            if state.get("status") == "learned":
                log.info("Auto-learning: %s", state["message"])
        except (JobError, PrometheusError) as exc:
            log.warning("Auto-learning skipped: %s", exc)
        except Exception:  # noqa: BLE001 - never stop the worker
            log.exception("Auto-learning failed.")


class Command(BaseCommand):
    help = "Runs the detection worker."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="Run a single cycle and exit.")

    def handle(self, *args, **options):
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
        try:
            file_cfg = load_config(settings.HPCSENTINEL_CONFIG)
            worker = Worker(file_cfg)
            cfg, _ = worker.current()
        except (ConfigError, PrometheusError) as exc:
            raise CommandError(str(exc))

        if options["once"]:
            worker.jobs()
            worker.tick()
            worker.notify()
            worker.learn()
            worker.reports()
            return

        scheduler = BlockingScheduler(timezone="UTC")
        interval = cfg["detection"]["retrieval_interval_seconds"]
        scheduler.add_job(worker.tick, "interval", seconds=interval, max_instances=1, coalesce=True)
        # Requests made in the web application (train, replay, evaluate) are picked up here (D-3).
        scheduler.add_job(worker.jobs, "interval", seconds=10, max_instances=1, coalesce=True)
        # The platform decides for itself when to learn; no administrator action is needed (see services/autolearn).
        scheduler.add_job(worker.learn, "interval", minutes=15, max_instances=1, coalesce=True)
        scheduler.add_job(worker.reports, "interval", minutes=60, max_instances=1, coalesce=True)
        # Email alerts are a separate step after detection: a slow or failing mail server cannot hold up a cycle.
        scheduler.add_job(worker.notify, "interval", seconds=60, max_instances=1, coalesce=True)
        log.info("Worker started; one cycle every %s seconds against %s.", interval, cfg["prometheus"]["url"])
        worker.tick()  # first cycle immediately
        worker.learn()  # and a first look at whether there is enough data to learn from
        worker.reports()  # and write any report that came due while the worker was stopped
        try:
            scheduler.start()
        except (KeyboardInterrupt, SystemExit):
            log.info("Worker stopped.")
