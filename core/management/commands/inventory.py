from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from core.config import ConfigError, load_config
from core.models import Alert, ModelVersion
from core.services import inventory, settings_store
from django.conf import settings


class Command(BaseCommand):
    help = "Show how much clean idle and loaded data Prometheus holds for each node, and what the platform has learned so far."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=30, help="how far back to look (default 30)")
        parser.add_argument("--start", help="ISO start time (UTC), overrides --days")
        parser.add_argument("--end", help="ISO end time (UTC), default now")

    def handle(self, *args, **opts):
        try:
            cfg = settings_store.effective_config(load_config(settings.HPCSENTINEL_CONFIG))
        except ConfigError as exc:
            raise CommandError(str(exc))
        end = opts["end"] or timezone.now()
        start = opts["start"] or (timezone.now() - timedelta(days=opts["days"]))
        client = settings_store.make_client(cfg)
        if not client.is_reachable():
            raise CommandError("Prometheus is not reachable.")
        self.stdout.write(inventory.render(inventory.collect(cfg, client, start, end)))
        self.stdout.write("\nModels:")
        for m in ModelVersion.objects.order_by("trained_at"):
            self.stdout.write(f"  {m.label}: learned from {m.train_start:%Y-%m-%d %H:%M} to {m.train_end:%Y-%m-%d %H:%M} UTC, "
                              f"{m.baselines.get('nodes')} nodes, {m.baselines.get('rows')} rows, forests {m.baselines.get('forests')}")
        self.stdout.write("\nLive alerts by status and outcome:")
        for status, outcome in Alert.objects.filter(mode=Alert.Mode.LIVE).values_list("status", "outcome").distinct():
            n = Alert.objects.filter(mode=Alert.Mode.LIVE, status=status, outcome=outcome).count()
            self.stdout.write(f"  {status} {outcome or ''}: {n}")
