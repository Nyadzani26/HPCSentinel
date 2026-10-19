"""Screens S-02 to S-05 and the controls: access control, triage flow and validation (FR-21 to FR-27, BR-04..BR-07, BR-11)."""
from datetime import timedelta

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from core.models import Alert, AlertAction, CycleRun, ExcludedPeriod, ExperimentEntry, Job, Node, NodeWindow, User
from core.tests.helpers import make_alert, make_config, make_model, make_node, make_user


class FleetScreenTests(TestCase):
    def setUp(self):
        self.op = make_user("op")
        self.client.force_login(self.op)

    def cycle(self, reachable=True, ago=0):
        return CycleRun.objects.create(mode="LIVE", started_at=timezone.now() - timedelta(minutes=ago),
                                       source_reachable=reachable, config=make_config())

    def test_no_cycle_yet_is_explained(self):
        self.assertContains(self.client.get(reverse("home")), "No detection cycle has run yet")

    def test_outage_banner_is_shown(self):  # FR-21, NFR-08
        self.cycle(reachable=False)
        self.assertContains(self.client.get(reverse("home")), "Data source unavailable")

    def test_tile_states(self):  # FR-21
        config, model = make_config(), make_model(make_config())
        cyc = self.cycle()
        for name, quality in [("compute01", "RELIABLE"), ("compute02", "RELIABLE"), ("compute03", "RELIABLE"),
                              ("compute04", "UNRELIABLE"), ("compute05", "NOT_LEARNED")]:
            node = make_node(name)
            NodeWindow.objects.create(node=node, cycle=cyc, window_end=timezone.now(), quality=quality, load_state="IDLE", combined_score=1.0)
        make_alert(Node.objects.get(name="compute02"), config=config, model=model)
        a = make_alert(Node.objects.get(name="compute03"), config=config, model=model)
        a.acknowledge()
        html = self.client.get(reverse("home")).content.decode()
        self.assertEqual(html.count('class="tile normal"'), 1)
        self.assertEqual(html.count('class="tile unusual"'), 1)
        self.assertEqual(html.count('class="tile review"'), 1)
        self.assertEqual(html.count('class="tile unknown"'), 2)

    def test_node_detail_renders_without_data(self):  # FR-22
        make_node("compute01")
        self.assertContains(self.client.get(reverse("node_detail", args=["compute01"])), "No windows have been scored")

    def test_unknown_node_is_404(self):
        self.assertEqual(self.client.get(reverse("node_detail", args=["nope"])).status_code, 404)


class TriageTests(TestCase):
    def setUp(self):
        self.op = make_user("op")
        self.admin = make_user("boss", role=User.Role.ADMINISTRATOR)
        self.alert = make_alert()
        self.client.force_login(self.op)

    def post(self, name, data=None):
        return self.client.post(reverse(name, args=[self.alert.pk]), data or {}, follow=True)

    def test_alert_pages_require_sign_in(self):  # FR-01
        self.client.logout()
        self.assertEqual(self.client.get(reverse("alert_detail", args=[self.alert.pk])).status_code, 302)
        self.assertEqual(self.client.post(reverse("alert_acknowledge", args=[self.alert.pk])).status_code, 302)
        self.alert.refresh_from_db()
        self.assertEqual(self.alert.status, "OPEN")

    def test_detail_shows_the_explanation(self):  # FR-19, FR-23
        self.assertContains(self.client.get(reverse("alert_detail", args=[self.alert.pk])), "Why this alert was raised")

    def test_full_triage_flow_records_who_and_when(self):  # FR-24..FR-27, BR-11
        self.post("alert_acknowledge")
        self.post("alert_close", {"outcome": "CONFIRMED", "note": "Bad DIMM replaced"})
        self.alert.refresh_from_db()
        self.assertEqual((self.alert.status, self.alert.outcome), ("CLOSED", "CONFIRMED"))
        actions = list(AlertAction.objects.filter(alert=self.alert).order_by("created_at").values_list("action", "user__username"))
        self.assertEqual(actions, [("ACKNOWLEDGE", "op"), ("CLOSE", "op")])

    def test_closing_needs_an_outcome(self):  # BR-06
        self.post("alert_acknowledge")
        response = self.post("alert_close", {"note": "no outcome"})
        self.assertContains(response, "Choose Confirmed or False alarm.")
        self.alert.refresh_from_db()
        self.assertEqual(self.alert.status, "ACKNOWLEDGED")

    def test_cannot_close_an_unacknowledged_alert(self):  # BR-05
        response = self.post("alert_close", {"outcome": "FALSE_ALARM"})
        self.assertContains(response, "must be acknowledged")
        self.alert.refresh_from_db()
        self.assertEqual(self.alert.status, "OPEN")

    def test_operator_cannot_reopen(self):  # BR-04, BR-07
        self.post("alert_acknowledge")
        self.post("alert_close", {"outcome": "FALSE_ALARM"})
        response = self.post("alert_reopen")
        self.assertContains(response, "Only an Administrator")
        self.alert.refresh_from_db()
        self.assertEqual(self.alert.status, "CLOSED")

    def test_administrator_can_reopen_and_closed_alerts_refuse_notes(self):  # BR-07
        self.post("alert_acknowledge")
        self.post("alert_close", {"outcome": "FALSE_ALARM"})
        self.assertContains(self.post("alert_note", {"note": "late"}), "cannot be changed")
        self.client.force_login(self.admin)
        self.post("alert_reopen", {"note": "new evidence"})
        self.alert.refresh_from_db()
        self.assertEqual((self.alert.status, self.alert.outcome), ("ACKNOWLEDGED", None))

    def test_get_does_not_change_an_alert(self):
        self.assertEqual(self.client.get(reverse("alert_acknowledge", args=[self.alert.pk])).status_code, 405)

    def test_list_filters(self):
        make_alert(make_node("compute09"), status="CLOSED", outcome="FALSE_ALARM")
        live = self.client.get(reverse("alert_list")).content.decode()
        self.assertIn("compute01", live)
        self.assertNotIn("compute09", live)
        self.assertIn("compute09", self.client.get(reverse("alert_list") + "?status=all").content.decode())


class ControlsTests(TestCase):
    def setUp(self):
        self.admin = make_user("boss", role=User.Role.ADMINISTRATOR)
        self.op = make_user("op")
        make_node("compute01")

    def test_operator_is_refused(self):  # BR-04
        self.client.force_login(self.op)
        self.assertEqual(self.client.get(reverse("controls")).status_code, 403)
        self.assertEqual(self.client.post(reverse("controls"), {"action": "train"}).status_code, 403)

    def test_training_request_creates_a_job(self):  # FR-28, D-3
        self.client.force_login(self.admin)
        self.client.post(reverse("controls"), {"action": "train", "start": "2026-10-01T00:00", "end": "2026-10-03T00:00"})
        job = Job.objects.get()
        self.assertEqual((job.type, job.status, job.requested_by), ("TRAIN", "PENDING", self.admin))

    def test_reversed_period_is_rejected(self):
        self.client.force_login(self.admin)
        response = self.client.post(reverse("controls"), {"action": "train", "start": "2026-10-03T00:00", "end": "2026-10-01T00:00"})
        self.assertContains(response, "must be after its start")
        self.assertEqual(Job.objects.count(), 0)

    def test_excluded_period_is_saved_and_validated(self):  # FR-16, BR-03
        self.client.force_login(self.admin)
        self.client.post(reverse("controls"), {"action": "excluded", "start_at": "2026-10-01T10:00", "end_at": "2026-10-01T12:00", "reason": "HPL run"})
        self.assertEqual(ExcludedPeriod.objects.get().created_by, self.admin)
        response = self.client.post(reverse("controls"), {"action": "excluded", "start_at": "2026-10-02T12:00", "end_at": "2026-10-02T10:00", "reason": "x"})
        self.assertContains(response, "must be after its start")
        self.assertEqual(ExcludedPeriod.objects.count(), 1)

    def test_experiment_upload(self):  # FR-29
        from django.core.files.uploadedfile import SimpleUploadedFile
        self.client.force_login(self.admin)
        good = SimpleUploadedFile("log.csv", b"node,anomaly_type,start,end\ncompute01,cpu,2026-10-02T10:00:00Z,2026-10-02T11:00:00Z\n")
        self.client.post(reverse("controls"), {"action": "upload", "file": good})
        self.assertEqual(ExperimentEntry.objects.count(), 1)
        bad = SimpleUploadedFile("bad.csv", b"node,anomaly_type,start,end\nnope,cpu,2026-10-02T10:00:00Z,2026-10-02T11:00:00Z\n")
        response = self.client.post(reverse("controls"), {"action": "upload", "file": bad}, follow=True)
        self.assertContains(response, "unknown node")
        self.assertEqual(ExperimentEntry.objects.count(), 1)
