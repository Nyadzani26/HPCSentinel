"""The automatic detection cycle (workflow 1, Figures 3 and 4).

This first version implements the first branch of the cycle: it checks that the data
source is reachable, records the cycle either way, and keeps the node registry in step
with Prometheus. Retrieval of metrics, quality checks, features, scoring and alerting
are added to this class in the following build steps.
"""
import logging

from django.utils import timezone

from core.config import build_query, node_name
from core.models import CycleRun, Node
from core.services.config_store import ensure_config_version
from core.services.prometheus import PrometheusClient, PrometheusError

log = logging.getLogger(__name__)


class DetectionCycle:
    def __init__(self, cfg: dict, client: PrometheusClient):
        self.cfg = cfg
        self.client = client

    def sync_nodes(self):
        """Create a Node row for every node Prometheus reports and is not yet known (FR-05)."""
        label = self.cfg["cluster"]["node_label"]
        query = f"up{{{self.cfg['cluster']['selector']}}}"
        created = 0
        for source_label in self.client.query_instant(query, label=label):
            name = node_name(self.cfg, source_label)
            if name is None:
                log.warning("Node label %r does not match node_name_regex; skipped.", source_label)
                continue
            _, was_created = Node.objects.get_or_create(name=name, defaults={"source_label": source_label})
            created += was_created
        return created

    def run(self) -> CycleRun:
        config_version = ensure_config_version(self.cfg)
        cycle = CycleRun.objects.create(
            mode=CycleRun.Mode.LIVE,
            started_at=timezone.now(),
            source_reachable=self.client.is_reachable(),
            config=config_version,
        )
        if not cycle.source_reachable:
            # NFR-08: a missing data source is recorded as such; no node alerts are raised.
            log.warning("Prometheus is not reachable; cycle %s recorded as an outage.", cycle.pk)
        else:
            try:
                self.sync_nodes()
            except PrometheusError as exc:
                log.error("Node registry could not be updated: %s", exc)
        cycle.finished_at = timezone.now()
        cycle.save(update_fields=["finished_at"])
        return cycle
