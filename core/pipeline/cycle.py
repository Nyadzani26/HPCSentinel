"""The automatic detection cycle (workflow 1, Figures 3 and 4).

Each run checks that the data source is reachable and records the cycle either way
(NFR-08). When it is reachable the cycle keeps the node registry in step with Prometheus,
reads the last hour of metrics, checks data quality, derives load state and features,
scores every node with the active model, stores one window per node and lets the alert
manager apply the persistence rule (BR-01). The scoring and alert code is shared with
replay (D-5).
"""
import logging

import pandas as pd
from django.utils import timezone

from core.config import node_name
from core.models import Alert, CycleRun, ModelVersion, Node, NodeWindow
from core.pipeline import features as feat
from core.pipeline.detector import Detector
from core.pipeline.engine import AlertManager, DEFAULT_GROUP, record_window, rows_at, score_rows
from core.services.config_store import ensure_config_version
from core.services.prometheus import PrometheusClient, PrometheusError

log = logging.getLogger(__name__)


class DetectionCycle:
    LOOKBACK_SECONDS = 70 * 60  # 60 minutes of features plus a margin

    def __init__(self, cfg: dict, client: PrometheusClient):
        self.cfg = cfg
        self.client = client
        self._detector: tuple[int, Detector] | None = None

    def active_model(self):
        """The newest trained model and its detector, or (None, None) before first training."""
        model = ModelVersion.objects.order_by("-trained_at", "-pk").first()
        if model is None:
            return None, None
        if self._detector is None or self._detector[0] != model.pk:
            try:
                self._detector = (model.pk, Detector.load(model.file_path))
            except (OSError, KeyError, EOFError) as exc:
                log.error("Model %s could not be loaded: %s", model.label, exc)
                return None, None
        return model, self._detector[1]

    def score_fleet(self, cycle: CycleRun, now=None) -> int:
        """Retrieve, score and store one window per node; returns the number of alerts raised."""
        now = pd.Timestamp(now or timezone.now()).floor("min")
        frames_by_label = feat.fetch_frames(self.client, self.cfg, now.timestamp() - self.LOOKBACK_SECONDS,
                                            now.timestamp(), step=60)
        nodes = list(Node.objects.filter(is_active=True).select_related("group"))
        by_label = {n.source_label: n for n in nodes}
        frames = {by_label[label].name: frame for label, frame in frames_by_label.items() if label in by_label}
        groups = {n.name: (n.group.name if n.group else DEFAULT_GROUP) for n in nodes}
        model, detector = self.active_model()
        rows = rows_at(frames, self.cfg, now)
        for node in nodes:  # a node with no data at all is recorded as unreliable
            rows.setdefault(node.name, {"quality": "UNRELIABLE", "state": None, "features": {}, "result": None})
        entries = score_rows(detector, rows, groups)
        manager = AlertManager(self.cfg, cycle.config, model, Alert.Mode.LIVE)
        by_name = {n.name: n for n in nodes}
        NodeWindow.objects.bulk_create(
            [record_window(cycle, by_name[name], entry, now.to_pydatetime(), model) for name, entry in entries.items()
             if name in by_name]
        )
        raised = 0
        active = set(Alert.objects.filter(mode=Alert.Mode.LIVE, status__in=Alert.ACTIVE).values_list("node_id", flat=True))
        for window in NodeWindow.objects.filter(cycle=cycle).select_related("node"):
            if manager.is_abnormal(window):
                raised += bool(manager.evaluate(window.node, window, groups[window.node.name],
                                                entries[window.node.name], now.to_pydatetime()))
            elif window.node_id in active:
                manager.settle(window.node, window)
        return raised

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

    def run(self, now=None) -> CycleRun:
        """Run one cycle. `now` is for the demo and tests; production always uses the clock."""
        injected = now
        now = now or timezone.now()
        config_version = ensure_config_version(self.cfg)
        cycle = CycleRun.objects.create(
            mode=CycleRun.Mode.LIVE,
            started_at=now,
            source_reachable=self.client.is_reachable(),
            config=config_version,
        )
        if not cycle.source_reachable:
            # NFR-08: a missing data source is recorded as such; no node alerts are raised.
            log.warning("Prometheus is not reachable; cycle %s recorded as an outage.", cycle.pk)
        else:
            try:
                self.sync_nodes()
                self.score_fleet(cycle, now)
            except PrometheusError as exc:
                # The cycle is kept; the failure is logged and the next cycle tries again (NFR-08).
                log.error("Cycle %s could not complete: %s", cycle.pk, exc)
        cycle.finished_at = now if injected is not None else timezone.now()
        cycle.save(update_fields=["finished_at"])
        return cycle
