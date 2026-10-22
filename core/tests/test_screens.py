"""Screens S-02 to S-05 and the controls: access control, triage flow and validation (FR-20 to FR-27, BR-04..BR-07, BR-11)."""
from datetime import timedelta

from pathlib import Path

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from core.models import Alert, AlertAction, Chassis, ClusterEvent, CycleRun, ExcludedPeriod, ExperimentEntry, Job, ModelVersion, Node, NodeWindow, User
from core.tests.helpers import make_alert, make_config, make_model, make_node, make_user


def alert_column(html):
    """The list of alerts on the left of the Alerts pages, without the chassis map and the open alert beside it."""
    return html.split('class="sidecol listcol"')[1].split("</aside>")[0]


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
        html = self.client.get(reverse("nodes")).content.decode()           # the Nodes page lists every node with its state
        self.assertEqual(html.count('data-state="normal"'), 1)
        self.assertEqual(html.count('data-state="unusual"'), 1)
        self.assertEqual(html.count('data-state="review"'), 1)
        self.assertEqual(html.count('data-state="unknown"'), 2)
        self.assertLess(html.index('data-name="compute02"'), html.index('data-name="compute01"'))  # problems first

    def test_node_detail_renders_without_data(self):  # FR-21
        make_node("compute01")
        self.assertContains(self.client.get(reverse("node_detail", args=["compute01"])), "No readings have been scored")

    def test_unusual_metrics_respect_the_smallest_change_that_matters(self):  # FR-21: the page agrees with the detector
        config = make_config()
        config.content = {"detection": {"alert_threshold": 3.0, "min_effect": {"context_switch_rate": 2000}}}
        config.save()
        make_model(config)
        model = ModelVersion.objects.get(label="m1")
        model.baselines = {"baselines": {"compute01": {"IDLE": {"context_switch_rate__mean_5": [50.0, 2.0, 100]}}},
                           "pooled": {"context_switch_rate__mean_5": 1.0}}
        model.save()
        node = make_node("compute01")
        cyc = self.cycle()
        NodeWindow.objects.create(node=node, cycle=cyc, window_end=timezone.now(), quality="RELIABLE", load_state="IDLE",
                                  combined_score=1.0, features={"context_switch_rate__mean_5": 60.0})
        shown = self.client.get(reverse("node_detail", args=["compute01"])).context["unusual"]
        self.assertEqual(shown, [])  # 10 per second is far below the 2000 that would be worth waking up for
        config.content["detection"]["min_effect"] = {}
        config.save()
        shown = self.client.get(reverse("node_detail", args=["compute01"])).context["unusual"]
        self.assertEqual([m["name"] for m in shown], ["context_switch_rate"])  # without the floor it would be called unusual

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

    def test_a_swinging_metric_is_not_described_as_a_rise_in_the_metric(self):  # FR-19
        self.alert.explanation = {"summary": "test", "contributors": [
            {"metric": "mem_available_ratio", "feature": "mem_available_ratio__std_15", "compared_with": "own baseline",
             "observed": 0.2, "expected": 0.00002, "z": 9.0}]}
        self.alert.save()
        page = self.client.get(reverse("alert_detail", args=[self.alert.pk])).content.decode()
        self.assertIn("available memory variation 10,000× higher than normal", page)
        self.assertIn("Available memory (variation)", page)

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

    def test_an_alert_can_be_reviewed_without_acknowledging_first(self):  # BR-05 relaxed
        self.post("alert_close", {"outcome": "FALSE_ALARM"})
        self.alert.refresh_from_db()
        self.assertEqual((self.alert.status, self.alert.outcome), ("CLOSED", "FALSE_ALARM"))

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

    def test_one_click_review_from_the_list_returns_to_the_list(self):
        self.alert.status = "RESOLVED"
        self.alert.save()
        response = self.client.post(reverse("alert_quick", args=[self.alert.pk]),
                                    {"outcome": "FALSE_ALARM", "next": "/alerts/?status=review"}, follow=True)
        self.assertEqual(response.redirect_chain[-1][0], "/alerts/?status=review")
        self.assertContains(response, "marked as false alarm")
        self.alert.refresh_from_db()
        self.assertEqual((self.alert.status, self.alert.outcome), ("CLOSED", "FALSE_ALARM"))

    def test_one_click_review_will_not_redirect_to_another_site(self):
        response = self.client.post(reverse("alert_quick", args=[self.alert.pk]),
                                    {"outcome": "CONFIRMED", "next": "https://evil.example/phish"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], reverse("alert_list"))

    def test_one_click_review_needs_a_valid_outcome(self):
        response = self.client.post(reverse("alert_quick", args=[self.alert.pk]), {"outcome": "MAYBE"}, follow=True)
        self.assertContains(response, "Choose Real fault or False alarm")
        self.alert.refresh_from_db()
        self.assertEqual(self.alert.status, "OPEN")

    def test_bulk_review(self):
        a = make_alert(make_node("compute05"), status="RESOLVED")
        b = make_alert(make_node("compute06"), status="RESOLVED")
        c = make_alert(make_node("compute07"), status="RESOLVED")
        response = self.client.post(reverse("alert_bulk"), {"ids": [a.pk, b.pk], "outcome": "FALSE_ALARM"}, follow=True)
        self.assertContains(response, "2 alerts marked as false alarm")
        for alert, expected in ((a, "CLOSED"), (b, "CLOSED"), (c, "RESOLVED")):
            alert.refresh_from_db()
            self.assertEqual(alert.status, expected)
        self.assertEqual(AlertAction.objects.filter(alert=a).get().user, self.op)

    def test_bulk_review_needs_a_selection_and_an_outcome(self):
        self.assertContains(self.client.post(reverse("alert_bulk"), {"outcome": "CONFIRMED"}, follow=True), "Tick at least one alert")
        self.assertContains(self.client.post(reverse("alert_bulk"), {"ids": [self.alert.pk]}, follow=True), "Choose Real fault or False alarm")
        self.alert.refresh_from_db()
        self.assertEqual(self.alert.status, "OPEN")

    def test_bulk_review_requires_sign_in(self):
        self.client.logout()
        self.assertEqual(self.client.post(reverse("alert_bulk"), {"ids": [self.alert.pk], "outcome": "CONFIRMED"}).status_code, 302)
        self.alert.refresh_from_db()
        self.assertEqual(self.alert.status, "OPEN")

    def test_tabs_separate_active_recovered_and_reviewed_alerts(self):
        make_alert(make_node("compute05"), status="RESOLVED")
        make_alert(make_node("compute09"), status="CLOSED", outcome="FALSE_ALARM")
        listing = lambda tab: alert_column(self.client.get(reverse("alert_list") + f"?status={tab}").content.decode())  # noqa: E731
        active, review, closed = listing("live"), listing("review"), listing("closed")
        self.assertIn("compute01", active); self.assertNotIn("compute05", active); self.assertNotIn("compute09", active)
        self.assertIn("compute05", review); self.assertNotIn("compute01", review)
        self.assertIn("compute09", closed); self.assertNotIn("compute05", closed)
        self.assertIn("Recovered<span class=\"n\">1</span>", active)   # counts show without opening the tab

    def test_empty_active_tab_points_to_recovered_alerts(self):
        self.alert.status = "RESOLVED"
        self.alert.save()
        page = self.client.get(reverse("alert_list")).content.decode()
        self.assertIn("Nothing is happening right now", page)
        self.assertIn("waiting for an optional review", page)

    def test_a_recovered_alert_page_says_it_ended_by_itself_and_offers_review(self):
        from datetime import timedelta
        from django.utils import timezone
        self.alert.status = "RESOLVED"
        self.alert.last_seen_at = self.alert.opened_at + timedelta(minutes=25)
        self.alert.recovered_at = timezone.now()
        self.alert.resolved_reason = "recovered"
        self.alert.recurrences = 2
        self.alert.save()
        page = self.client.get(reverse("alert_detail", args=[self.alert.pk])).content.decode()
        for text in ("Ended by itself", "lasted 25 min", "came back 2 times", "Was it a real fault?", "Save review"):
            self.assertIn(text, page)
        self.assertNotIn("I'm looking into it", page)   # nothing to look into any more

    def test_an_alert_that_stopped_reporting_says_so(self):
        from django.utils import timezone
        self.alert.status, self.alert.resolved_reason, self.alert.recovered_at = "RESOLVED", "no_data", timezone.now()
        self.alert.save()
        self.assertContains(self.client.get(reverse("alert_detail", args=[self.alert.pk])), "because the node stopped reporting")

    def test_an_updated_alert_shows_its_latest_state(self):
        self.alert.explanation = {"summary": "x", "load_state": "IDLE", "persistence_windows": 3, "threshold": 3.0, "scores": {},
                                  "contributors": [{"metric": "cpu_busy", "observed": 0.5, "expected": 0.1, "z": 5.0, "compared_with": "own baseline"}],
                                  "latest": {"at": "2026-10-21T10:00:00+00:00", "load_state": "IDLE", "scores": {},
                                             "contributors": [{"metric": "cpu_busy", "observed": 0.9, "expected": 0.1, "z": 9.0, "compared_with": "own baseline"}]}}
        self.alert.save()
        page = self.client.get(reverse("alert_detail", args=[self.alert.pk])).content.decode()
        self.assertIn("90.0%", page)                         # now
        self.assertIn("How it looked when the alert opened", page)

    def test_overview_mentions_recovered_alerts_waiting_for_review(self):
        make_alert(make_node("compute05"), status="RESOLVED")
        page = self.client.get(reverse("home")).content.decode()
        self.assertIn("recovered alert", page)
        self.assertIn("Nothing is wrong right now", page)

    def test_get_does_not_change_an_alert(self):
        self.assertEqual(self.client.get(reverse("alert_acknowledge", args=[self.alert.pk])).status_code, 405)

    def test_list_filters(self):
        make_alert(make_node("compute09"), status="CLOSED", outcome="FALSE_ALARM")
        live = alert_column(self.client.get(reverse("alert_list")).content.decode())
        self.assertIn("compute01", live)
        self.assertNotIn("compute09", live)
        self.assertIn("compute09", alert_column(self.client.get(reverse("alert_list") + "?status=all").content.decode()))


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
        html = alert_column(self.client.get(reverse("alert_list") + "?node=compute02").content.decode())
        self.assertIn("compute02", html)
        self.assertNotIn("compute01", html)

    def test_filter_by_date_range_and_bad_date(self):  # FR-22
        today = timezone.now().date().isoformat()
        self.assertContains(self.client.get(reverse("alert_list") + f"?from={today}&to={today}"), "compute01")
        self.assertNotContains(self.client.get(reverse("alert_list") + "?from=2020-01-01&to=2020-01-02"), "compute01")
        self.assertContains(self.client.get(reverse("alert_list") + "?from=banana"), "not a date")


class NodesPageTests(TestCase):
    def setUp(self):
        self.client.force_login(make_user("viewer"))

    def test_the_nodes_page_needs_a_sign_in(self):
        self.client.logout()
        self.assertEqual(self.client.get(reverse("nodes")).status_code, 302)

    def test_every_page_has_a_nodes_tab(self):
        make_node("compute01")
        for name in ("home", "nodes", "alert_list", "reports"):
            self.assertContains(self.client.get(reverse(name)), f'href="{reverse("nodes")}"')

    def test_the_page_lists_each_node_with_filters_and_search(self):
        for name in ("compute01", "compute02"):
            make_node(name)
        page = self.client.get(reverse("nodes")).content.decode()
        self.assertIn('data-name="compute01"', page)
        self.assertIn('data-name="compute02"', page)
        self.assertIn('id="q"', page)                                    # search box
        self.assertIn('data-f="attention"', page)                        # filter buttons

    def test_the_page_names_each_nodes_chassis(self):
        Node.objects.create(name="compute01", source_label="c01", chassis=Chassis.objects.create(name="Chassis 1"))
        page = self.client.get(reverse("nodes")).content.decode()
        self.assertIn("Chassis 1", page)
        self.assertIn("<th", page.split("Chassis</th>")[0])               # the Chassis column exists

    def test_the_overview_no_longer_carries_the_node_table(self):
        make_node("compute01")
        page = self.client.get(reverse("home")).content.decode()
        self.assertNotIn('data-name="compute01"', page)
        self.assertIn(reverse("nodes"), page)


class AlertPageLayoutTests(TestCase):
    def setUp(self):
        self.client.force_login(make_user("op"))

    def test_the_first_alert_is_open_beside_the_list_with_the_chassis_map(self):
        alert = make_alert(make_node("compute01"))
        page = self.client.get(reverse("alert_list")).content.decode()
        self.assertIn("Was it a real fault?", page)
        self.assertIn('class="t bad sel"', page)                          # its node is outlined on the map
        self.assertIn(reverse("alert_detail", args=[alert.pk]), page)

    def test_an_alert_opened_from_another_tab_still_appears_in_the_list(self):
        alert = make_alert(make_node("compute05"), status="CLOSED", outcome="FALSE_ALARM")
        listing = alert_column(self.client.get(reverse("alert_detail", args=[alert.pk]) + "?status=live").content.decode())
        self.assertIn("compute05", listing)

    def test_node_and_event_pages_show_the_map(self):
        node = make_node("compute03")
        page = self.client.get(reverse("node_detail", args=["compute03"])).content.decode()
        self.assertIn("Chassis map", page)
        self.assertIn(" sel", page)
        event = ClusterEvent.objects.create(started_at=timezone.now(), last_seen_at=timezone.now(), peak_nodes=1, peak_reliable=1, nodes=["compute03"])
        page = self.client.get(reverse("event_detail", args=[event.pk])).content.decode()
        self.assertIn("Chassis map", page)
        self.assertIn("changed together", page)
        self.assertIsNotNone(node)
