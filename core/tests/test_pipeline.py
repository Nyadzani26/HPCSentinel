"""The detection pipeline end to end on synthetic metrics (FR-08 to FR-20, BR-01 to BR-03, NFR-08)."""
import tempfile
from datetime import timedelta
from pathlib import Path

import pandas as pd
import yaml
from django.test import TestCase, override_settings

from core.models import (Alert, CycleRun, EvaluationRun, ExcludedPeriod, ExperimentEntry, Job, ModelVersion, Node,
                         NodeWindow)
from core.pipeline import features as feat
from core.pipeline import jobs
from core.pipeline.cycle import DetectionCycle
from core.pipeline.engine import AlertManager
from core.pipeline.jobs import JobError
from core.tests.helpers import make_user
from core.tests.synthetic import T0, SyntheticClient

EXAMPLE = Path(__file__).resolve().parents[2] / "config" / "hpcsentinel.example.yml"


def config(**detection):
    cfg = yaml.safe_load(EXAMPLE.read_text())
    cfg["cluster"]["node_name_regex"] = r"^10\.10\.10\.1(?P<n>\d\d):9100$"
    cfg["cluster"]["node_name_template"] = "compute{n}"
    cfg["detection"].update(detection)
    return cfg


def make_nodes(client, cfg):
    DetectionCycle(cfg, client).sync_nodes()


class FeatureTests(TestCase):
    def frame(self, minutes=200, gap=None):
        idx = pd.date_range(T0, periods=minutes, freq="1min", tz="UTC")
        frame = pd.DataFrame({"cpu_busy": 0.02, "mem_available_ratio": 0.9, "load_per_core": 0.02}, index=idx)
        if gap:
            frame.loc[idx[gap[0]]:idx[gap[1]], :] = float("nan")
        return frame

    def test_idle_and_loaded_states(self):  # FR-08, BR-09
        cfg = config()
        frame = self.frame()
        frame.loc[frame.index[60]:, "cpu_busy"] = 0.9
        table = feat.feature_table(frame, cfg)
        self.assertEqual(table[feat.STATE_COL].iloc[50], "IDLE")
        self.assertEqual(table[feat.STATE_COL].iloc[190], "LOADED")

    def test_a_change_of_load_state_is_a_transition_and_is_not_scored(self):  # BR-02, BR-09
        frame = self.frame()
        frame.loc[frame.index[100]:, "cpu_busy"] = 0.9
        table = feat.feature_table(frame, config())
        self.assertEqual(table[feat.STATE_COL].iloc[110], "TRANSITION")
        self.assertEqual(table[feat.QUALITY_COL].iloc[110], "UNRELIABLE")

    def test_window_features_are_named_by_span(self):  # FR-09
        table = feat.feature_table(self.frame(), config())
        for name in ("cpu_busy__mean_5", "cpu_busy__mean_15", "cpu_busy__mean_60", "cpu_busy__std_15"):
            self.assertIn(name, table.columns)

    def test_missing_data_makes_a_window_unreliable(self):  # FR-10, BR-02
        table = feat.feature_table(self.frame(gap=(100, 130)), config())
        self.assertEqual(table[feat.QUALITY_COL].iloc[95], "RELIABLE")
        self.assertEqual(table[feat.QUALITY_COL].iloc[131], "UNRELIABLE")

    def test_first_minutes_are_unreliable(self):
        table = feat.feature_table(self.frame(), config())
        self.assertEqual(table[feat.QUALITY_COL].iloc[3], "UNRELIABLE")


@override_settings(MODEL_STORE=tempfile.mkdtemp())
class PipelineTests(TestCase):
    """Trains on two normal days and replays a third day that contains known faults."""

    @classmethod
    def setUpTestData(cls):
        cls.user = make_user("admin1", role="ADMINISTRATOR")
        cls.cfg = config(persistence_windows=3, alert_threshold=3.0)
        day3 = T0 + timedelta(days=2)
        cls.fault_start, cls.fault_end = day3 + timedelta(hours=11), day3 + timedelta(hours=12)
        cls.prom = SyntheticClient(
            cls.cfg, n_nodes=8,
            faults=[("10.10.10.103:9100", cls.fault_start, cls.fault_end, "memory")],
        )
        make_nodes(cls.prom, cls.cfg)

    def trained(self):
        return jobs.train(self.cfg, self.prom, T0, T0 + timedelta(days=2), user=self.user)

    def test_training_stores_a_model_version(self):  # FR-28, BR-10
        model = self.trained()
        self.assertTrue(Path(model.file_path).exists())
        self.assertEqual(model.baselines["nodes"], 8)
        self.assertEqual(model.config.version_no, 1)

    def test_training_rejects_a_short_or_reversed_period(self):
        with self.assertRaises(JobError):
            jobs.train(self.cfg, self.prom, T0, T0 + timedelta(hours=1))
        with self.assertRaises(JobError):
            jobs.train(self.cfg, self.prom, T0 + timedelta(days=1), T0)

    def test_replay_detects_an_injected_fault_with_an_explanation(self):  # FR-17..FR-20
        model = self.trained()
        day3 = T0 + timedelta(days=2)
        jobs.replay(self.cfg, self.prom, model, day3, day3 + timedelta(hours=16), stride_minutes=2)
        alerts = Alert.objects.filter(mode=Alert.Mode.REPLAY)
        hit = alerts.filter(node__name="compute03", opened_at__gte=self.fault_start,
                            opened_at__lte=self.fault_end + timedelta(minutes=30))
        self.assertEqual(hit.count(), 1, [(a.node.name, a.opened_at) for a in alerts])
        explanation = hit.get().explanation
        self.assertIn(explanation["contributors"][0]["metric"],
                      {"mem_available_ratio", "major_fault_rate", "swap_used_bytes", "load_per_core"})
        for key in ("baseline", "peer", "model", "combined"):
            self.assertIn(key, explanation["scores"])
        self.assertEqual(explanation["persistence_windows"], 3)
        self.assertIn("history", explanation)
        # no alert on a node that had no fault
        self.assertEqual(alerts.exclude(node__name="compute03").count(), 0,
                         [(a.node.name, a.opened_at, a.explanation["summary"]) for a in alerts.exclude(node__name="compute03")])

    def test_evaluation_reports_event_level_metrics(self):  # FR-29, FR-30
        model = self.trained()
        day3 = T0 + timedelta(days=2)
        ExperimentEntry.objects.create(node=Node.objects.get(name="compute03"), anomaly_type="MEMORY",
                                       start_at=self.fault_start, end_at=self.fault_end, imported_by=self.user)
        jobs.replay(self.cfg, self.prom, model, day3, day3 + timedelta(hours=16), stride_minutes=2)
        job = Job.objects.create(type=Job.Type.EVALUATE, requested_by=self.user)
        ev = jobs.evaluate(job, model, day3, day3 + timedelta(hours=16))
        self.assertEqual((ev.episodes_total, ev.episodes_detected), (1, 1))
        self.assertLess(ev.median_delay_seconds, 15 * 60)
        self.assertEqual(ev.false_alerts_per_node_week, 0)

    def test_excluded_periods_are_not_learned_from(self):  # FR-16, BR-03
        ExcludedPeriod.objects.create(start_at=T0 + timedelta(hours=5), end_at=T0 + timedelta(hours=8),
                                      reason="benchmark", created_by=self.user)
        frames = jobs._node_frames(jobs._history(self.prom, self.cfg, T0, T0 + timedelta(hours=12)))
        node, frame = next(iter(frames.items()))
        kept = jobs._drop_excluded(frame)
        self.assertFalse(((kept.index > pd.Timestamp(T0 + timedelta(hours=5))) &
                          (kept.index <= pd.Timestamp(T0 + timedelta(hours=9)))).any())

    def test_live_cycle_before_training_marks_nodes_not_learned(self):  # FR-11
        from django.utils import timezone
        client = SyntheticClient(self.cfg, n_nodes=8)
        client.query_range = lambda q, s, e, step, label="instance": SyntheticClient.query_range(
            client, q, T0.timestamp() + 86400, T0.timestamp() + 86400 + 3600, step, label)
        # shift "now" so the retrieved hour lines up with the synthetic data
        import core.pipeline.cycle as cycle_module
        fixed = T0 + timedelta(days=1, hours=1)
        original = cycle_module.timezone.now
        cycle_module.timezone.now = lambda: fixed
        try:
            cycle = DetectionCycle(self.cfg, client).run()
        finally:
            cycle_module.timezone.now = original
        self.assertEqual(NodeWindow.objects.filter(cycle=cycle).count(), 8)
        self.assertFalse(Alert.objects.exists())
        self.assertEqual(set(NodeWindow.objects.filter(cycle=cycle).values_list("quality", flat=True)) - {"NOT_LEARNED", "UNRELIABLE"}, set())


class ExperimentImportTests(TestCase):
    def setUp(self):
        self.user = make_user("admin2", role="ADMINISTRATOR")
        Node.objects.create(name="compute01", source_label="a")

    def test_valid_file_is_imported(self):
        n = jobs.import_experiments("node,anomaly_type,start,end,intensity\ncompute01,cpu,2026-10-02T10:00:00Z,2026-10-02T11:00:00Z,high\n", self.user)
        self.assertEqual(n, 1)
        self.assertEqual(ExperimentEntry.objects.get().anomaly_type, "CPU")

    def test_invalid_row_rejects_the_whole_file(self):
        text = ("node,anomaly_type,start,end\ncompute01,cpu,2026-10-02T10:00:00Z,2026-10-02T11:00:00Z\n"
                "compute99,cpu,2026-10-02T10:00:00Z,2026-10-02T11:00:00Z\n")
        with self.assertRaises(JobError):
            jobs.import_experiments(text, self.user)
        self.assertEqual(ExperimentEntry.objects.count(), 0)

    def test_end_before_start_is_rejected(self):
        with self.assertRaises(JobError):
            jobs.import_experiments("node,anomaly_type,start,end\ncompute01,cpu,2026-10-02T11:00:00Z,2026-10-02T10:00:00Z\n", self.user)

    def test_missing_columns_are_rejected(self):
        with self.assertRaises(JobError):
            jobs.import_experiments("node,start\ncompute01,2026-10-02T10:00:00Z\n", self.user)


class JobRunnerTests(TestCase):
    def test_a_failing_job_is_recorded_and_does_not_raise(self):  # NFR-08
        user = make_user("admin3", role="ADMINISTRATOR")
        job = Job.objects.create(type=Job.Type.TRAIN, requested_by=user, parameters={"start": "2026-10-02T00:00:00Z", "end": "2026-10-01T00:00:00Z"})
        jobs.run_job(job, config(), SyntheticClient(config()))
        job.refresh_from_db()
        self.assertEqual(job.status, Job.Status.FAILED)
        self.assertIn("after its start", job.message)


class AlertRuleTests(TestCase):
    """BR-01 (persistence), BR-08 (one live alert per node) and recounting after closure."""

    def setUp(self):
        from core.services.config_store import ensure_config_version
        from core.tests.helpers import make_model
        self.cfg = config(persistence_windows=3, alert_threshold=3.0)
        self.config_version = ensure_config_version(self.cfg)
        self.model = make_model(self.config_version)
        self.node = Node.objects.create(name="compute01", source_label="a")
        self.entry = {"quality": "RELIABLE", "state": "IDLE", "features": {}, "result": {
            "baseline": 5.0, "peer": 4.0, "model": 4.0, "combined": 4.5, "baseline_detail": {
                "cpu_busy__mean_15": (6.0, 0.02, 0.9)}, "peer_detail": {}}}
        self.t = T0
        self.manager = AlertManager(self.cfg, self.config_version, self.model, Alert.Mode.LIVE)

    def window(self, score, minutes):
        cycle = CycleRun.objects.create(mode="LIVE", started_at=T0 + timedelta(minutes=minutes), source_reachable=True,
                                        config=self.config_version)
        w = NodeWindow.objects.create(node=self.node, cycle=cycle, window_end=T0 + timedelta(minutes=minutes),
                                      quality="RELIABLE", load_state="IDLE", combined_score=score)
        return self.manager.evaluate(self.node, w, "default", self.entry, T0 + timedelta(minutes=minutes))

    def test_two_abnormal_windows_raise_nothing(self):  # BR-01
        self.assertIsNone(self.window(4.5, 0))
        self.assertIsNone(self.window(4.5, 2))
        self.assertEqual(Alert.objects.count(), 0)

    def test_three_consecutive_abnormal_windows_raise_one_alert(self):  # BR-01
        self.window(4.5, 0); self.window(4.5, 2)
        alert = self.window(4.5, 4)
        self.assertIsNotNone(alert)
        self.assertEqual(alert.status, Alert.Status.OPEN)
        self.assertEqual(alert.windows.count(), 3)

    def test_a_normal_window_resets_the_count(self):  # BR-01
        for minutes, score in [(0, 4.5), (2, 4.5), (4, 1.0), (6, 4.5), (8, 4.5)]:
            self.assertIsNone(self.window(score, minutes))
        self.assertEqual(Alert.objects.count(), 0)

    def test_a_continuing_fault_does_not_create_a_second_alert(self):  # BR-08
        for minutes in (0, 2, 4, 6, 8, 10):
            self.window(5.0 if minutes else 4.5, minutes)
        self.assertEqual(Alert.objects.count(), 1)
        self.assertEqual(Alert.objects.get().peak_score, 5.0)

    def test_after_closure_three_new_windows_are_needed(self):  # BR-01, BR-05
        for minutes in (0, 2, 4):
            self.window(4.5, minutes)
        alert = Alert.objects.get()
        alert.status = Alert.Status.CLOSED
        alert.outcome = Alert.Outcome.FALSE_ALARM
        alert.closed_at = T0 + timedelta(minutes=5)
        alert.save()
        self.assertIsNone(self.window(4.5, 6))
        self.assertIsNone(self.window(4.5, 8))
        self.assertIsNotNone(self.window(4.5, 10))

    def test_unreliable_windows_never_alert(self):  # BR-02
        cycle = CycleRun.objects.create(mode="LIVE", started_at=T0, source_reachable=True, config=self.config_version)
        w = NodeWindow.objects.create(node=self.node, cycle=cycle, window_end=T0, quality="UNRELIABLE", combined_score=9.0)
        self.assertFalse(self.manager.is_abnormal(w))

    def test_the_explanation_names_baseline_observed_and_deviation(self):  # FR-19
        self.window(4.5, 0); self.window(4.5, 2)
        alert = self.window(4.5, 4)
        top = alert.explanation["contributors"][0]
        self.assertEqual((top["metric"], top["observed"], top["expected"]), ("cpu_busy", 0.9, 0.02))
        self.assertEqual(top["deviation_pct"], 4400.0)
        self.assertEqual(alert.explanation["load_state"], "IDLE")
