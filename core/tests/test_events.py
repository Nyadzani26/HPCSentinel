"""Cluster-wide events (stage 4): many nodes unusual at once become one event, not many alerts."""
from datetime import datetime, timedelta, timezone as tz

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from core.models import Alert, ClusterEvent, CycleRun, Node, NodeWindow
from core.pipeline.engine import AlertManager, apply_alert_rules
from core.services import events
from core.tests.helpers import make_config, make_model, make_node, make_user
from core.tests.test_pipeline import config

T0 = datetime(2026, 10, 20, 14, 0, tzinfo=tz.utc)


class EventRuleTests(TestCase):
    def cfg(self, **kw):
        return config(persistence_windows=2, alert_threshold=3.0, **kw)

    def test_the_rule_needs_both_a_minimum_and_a_share_of_the_nodes(self):
        cfg = self.cfg()
        self.assertTrue(events.is_event(15, 48, cfg))            # 31%
        self.assertFalse(events.is_event(14, 48, cfg))           # 29%
        self.assertFalse(events.is_event(4, 8, cfg))             # half the nodes, but fewer than five
        self.assertFalse(events.is_event(0, 0, cfg))

    def test_the_thresholds_come_from_the_configuration(self):
        cfg = self.cfg(cluster_event_fraction=0.5, cluster_event_min_nodes=2)
        self.assertTrue(events.is_event(2, 4, cfg))
        self.assertFalse(events.is_event(2, 5, cfg))


class ClusterCycleTests(TestCase):
    """The same code the live cycle uses, driven with scripted scores for twelve nodes."""

    def setUp(self):
        from core.services.config_store import ensure_config_version
        self.cfg = config(persistence_windows=2, alert_threshold=3.0)
        self.config_version = ensure_config_version(self.cfg)
        self.model = make_model(self.config_version)
        self.nodes = [Node.objects.create(name=f"compute{i:02d}", source_label=f"c{i}") for i in range(1, 13)]
        self.groups = {n.name: "default" for n in self.nodes}
        self.manager = AlertManager(self.cfg, self.config_version, self.model, Alert.Mode.LIVE)
        self.minute = 0

    def entry(self, metric="cpu_busy"):
        return {"quality": "RELIABLE", "state": "IDLE", "features": {}, "result": {
            "baseline": 5.0, "peer": 4.0, "model": 4.0, "combined": 4.5,
            "baseline_detail": {f"{metric}__mean_15": (6.0, 0.02, 0.9)}, "peer_detail": {}}}

    def cycle(self, scores, metric="cpu_busy"):
        """`scores` maps node name to a combined score; nodes not listed are normal."""
        at = T0 + timedelta(minutes=self.minute)
        self.minute += 2
        cycle = CycleRun.objects.create(mode="LIVE", started_at=at, source_reachable=True, config=self.config_version)
        windows = []
        for n in self.nodes:
            windows.append(NodeWindow.objects.create(node=n, cycle=cycle, window_end=at, quality="RELIABLE", load_state="IDLE",
                                                     combined_score=scores.get(n.name, 1.0)))
        entries = {n.name: self.entry(metric) for n in self.nodes}
        return apply_alert_rules(self.manager, windows, entries, self.groups, at, self.cfg)

    def names(self, count):
        return {n.name: 5.0 for n in self.nodes[:count]}

    def test_a_few_unusual_nodes_alert_one_by_one(self):
        for _ in range(2):
            self.cycle(self.names(3))
        self.assertEqual(Alert.objects.count(), 3)
        self.assertEqual(ClusterEvent.objects.count(), 0)

    def test_many_nodes_at_once_make_one_event_and_no_alerts(self):
        for _ in range(4):
            self.cycle(self.names(10))                          # 10 of 12 at once
        self.assertEqual(Alert.objects.count(), 0)
        event = ClusterEvent.objects.get()
        self.assertEqual((event.peak_nodes, event.peak_reliable, len(event.nodes), event.is_active), (10, 12, 10, True))
        self.assertEqual(list(event.causes), ["cpu_busy"])

    def test_the_event_ends_after_a_few_quiet_cycles(self):
        for _ in range(3):
            self.cycle(self.names(10))
        for _ in range(events.QUIET_CYCLES):
            self.cycle({})
        event = ClusterEvent.objects.get()
        self.assertFalse(event.is_active)
        self.assertEqual(event.ended_at, event.last_seen_at)

    def test_a_node_still_unusual_after_the_event_alerts_straight_away(self):
        for _ in range(3):
            self.cycle(self.names(10))                          # event; nothing alerts
        still = {"compute01": 5.0}
        for _ in range(events.QUIET_CYCLES):
            self.cycle(still)
        self.assertEqual(list(Alert.objects.values_list("node__name", flat=True)), ["compute01"])

    def test_an_alert_that_already_existed_keeps_updating_during_an_event(self):
        for _ in range(2):
            self.cycle({"compute01": 5.0})
        alert = Alert.objects.get()
        self.cycle(self.names(10) | {"compute01": 8.0})
        alert.refresh_from_db()
        self.assertEqual(alert.peak_score, 8.0)
        self.assertEqual(Alert.objects.count(), 1)

    def test_a_second_event_after_the_first_has_ended_is_a_new_event(self):
        for _ in range(2):
            self.cycle(self.names(10))
        for _ in range(events.QUIET_CYCLES):
            self.cycle({})
        for _ in range(2):
            self.cycle(self.names(10))
        self.assertEqual(ClusterEvent.objects.count(), 2)


class EventScreenTests(TestCase):
    def setUp(self):
        self.client.force_login(make_user("op"))
        started = timezone.now() - timedelta(hours=2)
        self.event = ClusterEvent.objects.create(started_at=started, last_seen_at=started + timedelta(minutes=8), peak_nodes=47, peak_reliable=47,
                                                 nodes=[f"compute{i:02d}" for i in range(1, 48)], causes={"cpu_busy": 47})

    def test_the_alerts_page_shows_the_event_instead_of_forty_seven_alerts(self):
        self.event.ended_at = None
        self.event.save()
        page = self.client.get(reverse("alert_list")).content.decode()
        self.assertIn("Cluster-wide event", page)
        self.assertIn("47 of 47 nodes", page)

    def test_event_page_lists_nodes_and_cause(self):
        page = self.client.get(reverse("event_detail", args=[self.event.pk])).content.decode()
        for text in ("47 of 47", "compute47", "CPU busy"):
            self.assertIn(text, page)

    def test_the_overview_mentions_a_recent_event(self):
        self.assertIn("cluster-wide", self.client.get(reverse("home")).content.decode().lower())


class EventReportTests(TestCase):
    def test_reports_list_events_and_count_them_apart_from_alerts(self):
        from core.services import reports
        day = datetime(2026, 10, 20, tzinfo=tz.utc)
        ClusterEvent.objects.create(started_at=day + timedelta(hours=14, minutes=28), last_seen_at=day + timedelta(hours=14, minutes=36),
                                    ended_at=day + timedelta(hours=14, minutes=36), peak_nodes=47, peak_reliable=47,
                                    nodes=["compute01"], causes={"cpu_busy": 40, "load_per_core": 7})
        d = reports.build("daily", day, day + timedelta(days=1), day + timedelta(days=2))
        self.assertEqual(d["events"][0]["peak_nodes"], 47)
        self.assertEqual(d["events"][0]["cause"], "CPU busy")
        self.assertEqual(d["events"][0]["minutes"], 8.0)
        self.assertEqual(d["alerts"]["opened"], 0)
        self.assertIn("1 cluster-wide event", " ".join(d["summary"]))
        self.assertIn("counted as events, not as individual alerts", " ".join(d["summary"]))


class EventConfigTests(TestCase):
    def test_bad_event_settings_are_rejected(self):
        from core.config import ConfigError, validate
        for bad in ({"cluster_event_fraction": 0}, {"cluster_event_fraction": 1.5}, {"cluster_event_min_nodes": 1},
                    {"cluster_event_min_nodes": True}):
            with self.assertRaises(ConfigError):
                validate(config(**bad))
        validate(config(cluster_event_fraction=1, cluster_event_min_nodes=2))
