"""Run the detection worker: the scheduler that drives the detection cycle (Figure 8)."""
import logging

from apscheduler.schedulers.blocking import BlockingScheduler
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from core.config import ConfigError, load_config
from core.pipeline.cycle import DetectionCycle
from core.pipeline.jobs import process_pending
from core.services.prometheus import PrometheusClient, PrometheusError

log = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Runs the detection worker."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="Run a single cycle and exit.")

    def handle(self, *args, **options):
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
        try:
            cfg = load_config(settings.HPCSENTINEL_CONFIG)
            url = settings.PROMETHEUS_URL or cfg["prometheus"].get("url", "")
            client = PrometheusClient(url, timeout=cfg["prometheus"]["timeout_seconds"])
        except (ConfigError, PrometheusError) as exc:
            raise CommandError(str(exc))

        cycle = DetectionCycle(cfg, client)
        if options["once"]:
            process_pending(cfg, client)
            cycle.run()
            return

        interval = cfg["detection"]["retrieval_interval_seconds"]
        scheduler = BlockingScheduler(timezone="UTC")
        scheduler.add_job(cycle.run, "interval", seconds=interval, max_instances=1, coalesce=True)
        # Requests made in the web application (train, replay, evaluate) are picked up here (D-3).
        scheduler.add_job(lambda: process_pending(cfg, client), "interval", seconds=10, max_instances=1, coalesce=True)
        log.info("Worker started; one cycle every %s seconds against %s.", interval, url)
        cycle.run()  # first cycle immediately
        try:
            scheduler.start()
        except (KeyboardInterrupt, SystemExit):
            log.info("Worker stopped.")
