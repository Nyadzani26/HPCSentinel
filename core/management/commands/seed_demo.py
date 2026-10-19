"""Fill the platform with demonstration data from a synthetic cluster.

For a demonstration without a cluster. It builds a small fleet of synthetic nodes, trains
a model on three normal days, then runs the real live cycle every two minutes for the last
hours with two faults injected, so the fleet overview, an open alert with its explanation,
and the triage screens all have something to show. Everything goes through the same code
as a live deployment; only the data source is synthetic.
"""
from datetime import timedelta

import yaml
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from core.config import load_config
from core.demo_data import SyntheticClient
from core.models import Alert, ModelVersion, Node
from core.pipeline import jobs
from core.pipeline.cycle import DetectionCycle
from core.services.config_store import get_system_user


class Command(BaseCommand):
    help = "Seeds demonstration data from a synthetic cluster (never contacts Prometheus)."

    def add_arguments(self, parser):
        parser.add_argument("--nodes", type=int, default=12)
        parser.add_argument("--hours", type=int, default=6, help="Hours of live cycles to run.")
        parser.add_argument("--yes", action="store_true", help="Confirm that existing platform data may be mixed with demo data.")

    def handle(self, *args, **opts):
        if Node.objects.exists() and not opts["yes"]:
            raise CommandError("The platform already has nodes. Run with --yes to add demonstration data anyway.")
        try:
            cfg = load_config(settings.HPCSENTINEL_CONFIG)
        except Exception:  # noqa: BLE001 - fall back to the shipped example
            cfg = yaml.safe_load(open("config/hpcsentinel.example.yml"))
        cfg["cluster"].update({"selector": 'job="demo"', "node_name_regex": r"^10\.10\.10\.1(?P<n>\d\d):9100$",
                               "node_name_template": "compute{n}"})
        end = timezone.now().replace(second=0, microsecond=0)
        start = end - timedelta(hours=opts["hours"])
        faults = [
            ("10.10.10.103:9100", end - timedelta(minutes=45), end + timedelta(hours=2), "memory"),
            ("10.10.10.106:9100", start + timedelta(hours=1), start + timedelta(hours=2), "network"),
        ]
        client = SyntheticClient(cfg, n_nodes=opts["nodes"], faults=faults)
        cycle = DetectionCycle(cfg, client)
        cycle.sync_nodes()
        get_system_user()
        self.stdout.write("Training on three normal days of synthetic data...")
        normal = SyntheticClient(cfg, n_nodes=opts["nodes"])
        jobs.train(cfg, normal, start - timedelta(days=4), start - timedelta(days=1), label="demo-model", user=get_system_user())
        self.stdout.write(f"Running {opts['hours'] * 30} live cycles...")
        t = start
        while t <= end:
            cycle.run(now=t)
            t += timedelta(minutes=2)
        self.stdout.write(self.style.SUCCESS(
            f"Done: {Node.objects.count()} nodes, model {ModelVersion.objects.last().label}, "
            f"{Alert.objects.filter(mode='LIVE').count()} live alerts. Open the Fleet and Alerts pages."))
