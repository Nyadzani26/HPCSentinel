"""The first branches of the detection cycle (Figure 3): outage handling and node sync."""
import copy
from pathlib import Path

import yaml
from django.test import TestCase

from core.models import ConfigVersion, CycleRun, Node
from core.pipeline.cycle import DetectionCycle

EXAMPLE = Path(__file__).resolve().parents[2] / "config" / "hpcsentinel.example.yml"


class FakeClient:
    def __init__(self, reachable=True, nodes=()):
        self.reachable, self.nodes = reachable, nodes

    def is_reachable(self):
        return self.reachable

    def query_instant(self, query, label="instance"):
        return {n: 1.0 for n in self.nodes}


def config():
    cfg = yaml.safe_load(EXAMPLE.read_text())
    cfg["cluster"]["node_name_regex"] = r"^10\.10\.10\.1(?P<n>\d\d):9100$"
    cfg["cluster"]["node_name_template"] = "compute{n}"
    return cfg


class CycleTests(TestCase):
    def test_unreachable_source_is_recorded_and_creates_nothing(self):  # NFR-08
        cycle = DetectionCycle(config(), FakeClient(reachable=False, nodes=["10.10.10.101:9100"])).run()
        self.assertFalse(cycle.source_reachable)
        self.assertIsNotNone(cycle.finished_at)
        self.assertEqual(Node.objects.count(), 0)

    def test_reachable_source_registers_nodes_once(self):
        client = FakeClient(nodes=["10.10.10.101:9100", "10.10.10.102:9100", "192.168.0.9:9100"])
        DetectionCycle(config(), client).run()
        DetectionCycle(config(), client).run()
        self.assertEqual(sorted(Node.objects.values_list("name", flat=True)), ["compute01", "compute02"])
        self.assertEqual(CycleRun.objects.filter(source_reachable=True).count(), 2)

    def test_configuration_is_versioned(self):  # BR-10
        cfg = config()
        DetectionCycle(cfg, FakeClient()).run()
        DetectionCycle(cfg, FakeClient()).run()
        self.assertEqual(ConfigVersion.objects.count(), 1)
        changed = copy.deepcopy(cfg)
        changed["detection"]["persistence_windows"] = 4
        cycle = DetectionCycle(changed, FakeClient()).run()
        self.assertEqual(ConfigVersion.objects.count(), 2)
        self.assertEqual(cycle.config.version_no, 2)
        self.assertEqual(ConfigVersion.objects.filter(is_active=True).count(), 1)
