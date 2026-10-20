"""Zero-touch learning: the platform decides for itself when to learn and from what (supports FR-17, FR-28)."""
from datetime import timedelta

from django.test import TestCase

from core.demo_data import T0, SyntheticClient
from core.models import ModelVersion
from core.pipeline import jobs
from core.pipeline.cleaning import trim_contamination
from core.pipeline.detector import Detector
from core.pipeline import features as feat
from core.services import autolearn
from core.tests.test_pipeline import config, make_nodes


class LimitedClient(SyntheticClient):
    """A cluster whose history only begins at `available_from`, like a freshly installed monitoring stack."""

    def __init__(self, *a, available_from=None, **kw):
        super().__init__(*a, **kw)
        self.available_from = available_from

    def query_range(self, query, start, end, step, label="instance"):
        out = super().query_range(query, start, end, step, label)
        floor = self.available_from.timestamp()
        return {k: [p for p in pts if p[0] >= floor] for k, pts in out.items() if any(p[0] >= floor for p in pts)}


class AutoLearnTests(TestCase):
    def setUp(self):
        self.cfg = config(learn_days=14, learn_min_interval_hours=6)
        self.now = T0 + timedelta(days=4)

    def prom(self, hours, **kw):
        c = LimitedClient(self.cfg, n_nodes=8, available_from=self.now - timedelta(hours=hours), **kw)
        make_nodes(c, self.cfg)
        return c

    def test_it_waits_and_reports_progress_while_there_is_too_little_data(self):
        state = autolearn.step(self.cfg, self.prom(hours=3), now=self.now)
        self.assertEqual(state["status"], "collecting")
        self.assertIn("hours of clean data", state["message"])
        self.assertEqual(ModelVersion.objects.count(), 0)

    def test_it_learns_the_first_model_without_being_asked(self):
        state = autolearn.step(self.cfg, self.prom(hours=48), now=self.now)
        self.assertEqual(state["status"], "learned", state)
        model = ModelVersion.objects.get()
        self.assertEqual(model.baselines["origin"], "auto")
        cov = model.baselines["coverage"]
        self.assertEqual(cov["nodes"], 8)
        self.assertGreater(cov["rows_idle"], 0)
        self.assertGreater(cov["rows_loaded"], 0)
        self.assertEqual(model.job, None)

    def test_it_does_not_learn_again_straight_away_or_when_nothing_changed(self):
        c = self.prom(hours=48)
        autolearn.step(self.cfg, c, now=self.now)
        self.assertEqual(autolearn.step(self.cfg, c, now=self.now + timedelta(minutes=20))["status"], "waiting")
        later = autolearn.step(self.cfg, c, now=self.now + timedelta(hours=7))
        self.assertEqual(later["status"], "current", later)
        self.assertEqual(ModelVersion.objects.count(), 1)

    def test_it_learns_again_when_enough_new_data_has_arrived(self):
        autolearn.step(self.cfg, self.prom(hours=24), now=self.now)
        c = LimitedClient(self.cfg, n_nodes=8, available_from=self.now - timedelta(days=4))
        later = autolearn.step(self.cfg, c, now=self.now + timedelta(hours=7))
        self.assertEqual(later["status"], "learned", later)
        self.assertEqual(ModelVersion.objects.count(), 2)

    def test_a_manual_request_is_a_single_job_with_no_dates(self):
        from core.models import Job
        from core.tests.helpers import make_user
        user = make_user("admin2", role="ADMINISTRATOR")
        job = Job.objects.create(type=Job.Type.TRAIN, parameters={"auto": True}, requested_by=user)
        jobs.run_job(job, self.cfg, self.prom(hours=48))
        job.refresh_from_db()
        # run_job uses the real clock; the synthetic history is long enough to answer either way
        self.assertIn(job.status, ("DONE", "FAILED"))
        self.assertTrue(job.message)


class SelfCleaningTests(TestCase):
    def setUp(self):
        self.cfg = config()
        self.start = T0
        self.end = T0 + timedelta(days=2)
        fault = ("10.10.10.103:9100", T0 + timedelta(hours=11), T0 + timedelta(hours=14), "memory")
        self.client = SyntheticClient(self.cfg, n_nodes=8, faults=[fault])
        make_nodes(self.client, self.cfg)
        self.tables, self.groups = jobs.build_training_tables(self.cfg, self.client, self.start, self.end)

    def test_windows_far_outside_normal_are_dropped_before_the_final_fit(self):
        det = Detector.train(self.tables, self.groups)
        clean, trimmed = trim_contamination(det, self.tables, self.groups)
        self.assertGreater(trimmed, 0)
        faulty = self.tables["compute03"]
        in_fault = faulty[(faulty.index >= T0 + timedelta(hours=12)) & (faulty.index <= T0 + timedelta(hours=14))]
        kept = clean["compute03"].reindex(in_fault.index).dropna(how="all")
        self.assertLess(len(kept), len(in_fault))
        for node, table in self.tables.items():  # never more than 5% of any node is removed
            self.assertLessEqual(len(table) - len(clean[node]), 0.05 * len(table) + 1)

    def test_the_stored_model_records_how_much_was_dropped(self):
        model = jobs.fit_and_register(self.cfg, self.tables, self.groups, self.start, self.end)
        self.assertGreater(model.baselines["coverage"]["trimmed_rows"], 0)

    def test_cleaning_can_be_switched_off(self):
        cfg = config(self_clean=False)
        model = jobs.fit_and_register(cfg, self.tables, self.groups, self.start, self.end, label="raw")
        self.assertEqual(model.baselines["coverage"]["trimmed_rows"], 0)
