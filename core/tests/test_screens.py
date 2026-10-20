"""Screens S-02 to S-05 and the controls: access control, triage flow and validation (FR-20 to FR-27, BR-04..BR-07, BR-11)."""
from datetime import timedelta

from pathlib import Path

from django.test import TestCase, override_settings
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
        self.assertContains(self.client.get(reverse("home")), "Get started")

    def test_outage_banner_is_shown(self):  # FR-20, NFR-08
        self.cycle(reachable=False)
        self.assertContains(self.client.get(reverse("home")), "Data is out of date")

    def test_tile_states(self):  # FR-20
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
        self.assertEqual(html.count('data-state="normal"'), 1)
        self.assertEqual(html.count('data-state="unusual"'), 1)
        self.assertEqual(html.count('data-state="review"'), 1)
        self.assertEqual(html.count('data-state="unknown"'), 2)
        self.assertLess(html.index('data-name="compute02"'), html.index('data-name="compute01"'))  # problems first

    def test_node_detail_renders_without_data(self):  # FR-21
        make_node("compute01")
        self.assertContains(self.client.get(reverse("node_detail", args=["compute01"])), "No readings have been scored")

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

    def test_detail_shows_the_explanation(self):  # FR-19, FR-22
        self.assertContains(self.client.get(reverse("alert_detail", args=[self.alert.pk])), "What changed")

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


EXAMPLE = str(Path(__file__).resolve().parents[2] / "config" / "hpcsentinel.example.yml")


@override_settings(HPCSENTINEL_CONFIG=EXAMPLE, PROMETHEUS_URL="")
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

    def test_a_period_ending_in_the_future_is_rejected(self):
        # A year typed as 2027 once produced a model "learned from" a period that had not happened yet.
        self.client.force_login(self.admin)
        response = self.client.post(reverse("controls"), {"action": "train", "start": "2026-10-01T00:00", "end": "2099-10-03T00:00"})
        self.assertContains(response, "cannot be in the future")
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


    def test_settings_are_validated_and_saved(self):  # FR-04, FR-18, FR-27
        from core.models import PlatformSetting
        self.client.force_login(self.admin)
        ok = {"action": "settings", "prometheus_url": "http://prom:9090", "alert_threshold": "4.5", "persistence_windows": "4"}
        self.client.post(reverse("controls"), ok)
        self.assertEqual(PlatformSetting.objects.get(key="alert_threshold").value, 4.5)
        self.assertEqual(PlatformSetting.objects.get(key="alert_threshold").updated_by, self.admin)
        for bad in ({"prometheus_url": "not a url"}, {"alert_threshold": "0"}, {"persistence_windows": "50"}):
            response = self.client.post(reverse("controls"), {**ok, **bad})
            self.assertEqual(response.status_code, 200)
        self.assertEqual(PlatformSetting.objects.get(key="persistence_windows").value, 4)
        self.assertEqual(PlatformSetting.objects.get(key="prometheus_url").value, "http://prom:9090")

    def test_effective_config_applies_saved_settings(self):  # FR-18
        from core.config import load_config
        from core.services import settings_store
        self.client.force_login(self.admin)
        self.client.post(reverse("controls"), {"action": "settings", "prometheus_url": "http://prom:9090", "alert_threshold": "5", "persistence_windows": "2"})
        cfg = settings_store.effective_config(load_config(EXAMPLE))
        self.assertEqual((cfg["detection"]["alert_threshold"], cfg["detection"]["persistence_windows"], cfg["prometheus"]["url"]),
                         (5.0, 2, "http://prom:9090"))

    def test_a_sensitivity_choice_sets_the_numbers(self):
        from core.models import PlatformSetting
        self.client.force_login(self.admin)
        base = {"action": "settings", "prometheus_url": "http://prom:9090"}
        for choice, expected in (("relaxed", (4.0, 4)), ("balanced", (3.0, 3)), ("sensitive", (2.5, 2))):
            # the typed numbers are ignored when a preset is chosen
            self.client.post(reverse("controls"), {**base, "sensitivity": choice, "alert_threshold": "9", "persistence_windows": "9"})
            got = (PlatformSetting.objects.get(key="alert_threshold").value, PlatformSetting.objects.get(key="persistence_windows").value)
            self.assertEqual(got, expected, choice)
        self.client.post(reverse("controls"), {**base, "sensitivity": "custom", "alert_threshold": "3.7", "persistence_windows": "5"})
        self.assertEqual(PlatformSetting.objects.get(key="alert_threshold").value, 3.7)

    def test_custom_sensitivity_still_needs_numbers(self):
        self.client.force_login(self.admin)
        response = self.client.post(reverse("controls"), {"action": "settings", "prometheus_url": "http://prom:9090", "sensitivity": "custom"})
        self.assertContains(response, "Enter a number")

    def test_the_current_sensitivity_is_shown_as_selected(self):
        self.client.force_login(self.admin)
        self.client.post(reverse("controls"), {"action": "settings", "prometheus_url": "http://prom:9090", "sensitivity": "sensitive"})
        page = self.client.get(reverse("controls")).content.decode()
        self.assertRegex(page, r'value="sensitive"[^>]*checked|checked[^>]*value="sensitive"')

    def test_learn_now_asks_the_worker_to_learn_automatically(self):
        self.client.force_login(self.admin)
        response = self.client.post(reverse("controls"), {"action": "learn_now"}, follow=True)
        job = Job.objects.get()
        self.assertEqual((job.type, job.status, job.parameters), ("TRAIN", "PENDING", {"auto": True}))
        self.assertContains(response, "Learning started")

    def test_the_learning_tab_explains_where_things_stand(self):
        self.client.force_login(self.admin)
        page = self.client.get(reverse("controls"))
        self.assertContains(page, "has not looked at the data yet")
        self.assertContains(page, "Learn again now")
        # the manual tools are still there, but under Advanced and described as optional
        self.assertContains(page, "Learn from a specific period")
        self.assertContains(page, "Everything here is optional")

    def test_connection_test_reports_failure(self):  # FR-04
        from unittest import mock
        self.client.force_login(self.admin)
        with mock.patch("core.services.prometheus.PrometheusClient.is_reachable", return_value=False):
            response = self.client.post(reverse("controls"), {"action": "test_connection", "prometheus_url": "http://prom:9090"})
        self.assertContains(response, "did not answer")

    def test_readiness_report(self):  # FR-06
        from unittest import mock
        self.client.force_login(self.admin)
        fake = {"a:9100": 1.0, "b:9100": 1.0, "c:9100": 1.0}
        with mock.patch("core.services.prometheus.PrometheusClient.is_reachable", return_value=True), \
             mock.patch("core.services.prometheus.PrometheusClient.query_instant", return_value=fake):
            response = self.client.post(reverse("controls"), {"action": "readiness"})
        self.assertContains(response, "Suspect")
        self.assertContains(response, "Every node reports the same value")


class UserManagementTests(TestCase):
    def setUp(self):
        self.admin = make_user("boss", role=User.Role.ADMINISTRATOR)
        self.client.force_login(self.admin)

    def test_operator_cannot_manage_users(self):  # BR-04
        self.client.force_login(make_user("op"))
        self.assertEqual(self.client.get(reverse("users")).status_code, 403)

    def test_create_user_with_role_and_password_rules(self):  # FR-03, NFR-02
        self.client.post(reverse("users"), {"username": "newop", "password": "a-long-enough-pass", "role": "OPERATOR"})
        self.assertEqual(User.objects.get(username="newop").role, "OPERATOR")
        self.assertNotEqual(User.objects.get(username="newop").password, "a-long-enough-pass")
        response = self.client.post(reverse("users"), {"username": "weak", "password": "short", "role": "OPERATOR"})
        self.assertContains(response, "too short")
        self.assertFalse(User.objects.filter(username="weak").exists())

    def test_duplicate_username_is_rejected(self):
        response = self.client.post(reverse("users"), {"username": "BOSS", "password": "a-long-enough-pass", "role": "OPERATOR"})
        self.assertContains(response, "already in use")

    def test_deactivated_user_cannot_sign_in_and_last_admin_is_protected(self):  # FR-03
        op = make_user("op")
        self.client.post(reverse("user_update", args=[op.pk]), {"action": "deactivate"})
        op.refresh_from_db()
        self.assertFalse(op.is_active)
        self.assertFalse(self.client.__class__().login(username="op", password="correct-horse-battery"))
        response = self.client.post(reverse("user_update", args=[self.admin.pk]), {"action": "deactivate"}, follow=True)
        self.assertContains(response, "cannot deactivate")
        self.admin.refresh_from_db()
        self.assertTrue(self.admin.is_active)

    def test_role_change(self):
        op = make_user("op")
        self.client.post(reverse("user_update", args=[op.pk]), {"action": "ADMINISTRATOR"})
        op.refresh_from_db()
        self.assertEqual(op.role, "ADMINISTRATOR")


class AlertFilterTests(TestCase):
    def setUp(self):
        self.client.force_login(make_user("op"))
        make_alert(make_node("compute01"))
        make_alert(make_node("compute02"))

    def test_filter_by_node(self):  # FR-22
        html = self.client.get(reverse("alert_list") + "?node=compute02").content.decode()
        self.assertIn("compute02", html)
        self.assertNotIn("compute01", html)

    def test_filter_by_date_range_and_bad_date(self):  # FR-22
        today = timezone.now().date().isoformat()
        self.assertContains(self.client.get(reverse("alert_list") + f"?from={today}&to={today}"), "compute01")
        self.assertNotContains(self.client.get(reverse("alert_list") + "?from=2020-01-01&to=2020-01-02"), "compute01")
        self.assertContains(self.client.get(reverse("alert_list") + "?from=banana"), "not a date")
