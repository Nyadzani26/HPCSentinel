"""Run the detection worker: the scheduler that drives the detection cycle (Figure 8)."""
import logging

from apscheduler.schedulers.blocking import BlockingScheduler
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from core.config import ConfigError, load_config
from core.pipeline.cycle import DetectionCycle
from core.pipeline.jobs import JobError, process_pending, relearn
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

    def relearn(self):
        cfg, client = self.current()
        try:
            model = relearn(cfg, client)
            log.info("Scheduled re-learning produced %s.", model.label)
        except (JobError, PrometheusError) as exc:
            log.warning("Scheduled re-learning skipped: %s", exc)
        except Exception:  # noqa: BLE001 - never stop the worker
            log.exception("Scheduled re-learning failed.")


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
            return

        scheduler = BlockingScheduler(timezone="UTC")
        interval = cfg["detection"]["retrieval_interval_seconds"]
        scheduler.add_job(worker.tick, "interval", seconds=interval, max_instances=1, coalesce=True)
        # Requests made in the web application (train, replay, evaluate) are picked up here (D-3).
        scheduler.add_job(worker.jobs, "interval", seconds=10, max_instances=1, coalesce=True)
        hours = int(cfg["detection"].get("relearn_hours", 168))
        if hours > 0:
            scheduler.add_job(worker.relearn, "interval", hours=hours, max_instances=1, coalesce=True)
        log.info("Worker started; one cycle every %s seconds against %s.", interval, cfg["prometheus"]["url"])
        worker.tick()  # first cycle immediately
        try:
            scheduler.start()
        except (KeyboardInterrupt, SystemExit):
            log.info("Worker stopped.")
