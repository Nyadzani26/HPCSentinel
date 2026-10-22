"""Daily, weekly and monthly reports (stage 3). Days are South African days (settings.TIME_ZONE), not UTC days."""
import csv
import io
from datetime import datetime, timedelta, timezone as tz
from unittest import mock
from zoneinfo import ZoneInfo

from django.test import TestCase
from django.urls import reverse

from core.localtime import local
from core.models import Alert, CycleRun, ModelVersion, NodeWindow, Report, User
from core.services import reports
from core.tests.helpers import make_alert, make_config, make_model, make_node, make_user

SAST = ZoneInfo("Africa/Johannesburg")
NOW = datetime(2026, 10, 21, 14, 30, tzinfo=tz.utc)        # a Wednesday, 16:30 in South Africa
DAY = datetime(2026, 10, 20, tzinfo=SAST)                  # Tuesday: the finished day under test, midnight to midnight local time


def cycle(at, reachable=True):
    return CycleRun.objects.create(mode="LIVE", started_at=at, source_reachable=reachable, config=make_config())


def window(node, at, quality="RELIABLE", score=1.0):
    return NodeWindow.objects.create(node=node, cycle=cycle(at), window_end=at, quality=quality, load_state="IDLE", combined_score=score)


def alert(node, opened, minutes=20, status="RESOLVED", outcome=None, metric="cpu_busy", recurrences=0, reason="recovered"):
    a = make_alert(node, status=status, outcome=outcome)
    a.opened_at, a.last_seen_at = opened, opened + timedelta(minutes=minutes)
    a.recovered_at = a.last_seen_at + timedelta(minutes=10) if status != "OPEN" else None
    a.recurrences, a.resolved_reason = recurrences, reason if status != "OPEN" else ""
    a.closed_at = a.recovered_at if status == "CLOSED" else None
    a.explanation = {"load_state": "IDLE", "contributors": [{"metric": metric, "observed": 1, "expected": 0, "z": 5}]}
    a.save()
    return a


class PeriodTests(TestCase):
    def test_periods_start_on_day_monday_and_month_boundaries(self):
        self.assertEqual(reports.floor_period("daily", NOW), datetime(2026, 10, 21, tzinfo=SAST))
        self.assertEqual(reports.floor_period("weekly", NOW), datetime(2026, 10, 19, tzinfo=SAST))
        self.assertEqual(reports.floor_period("monthly", NOW), datetime(2026, 10, 1, tzinfo=SAST))

    def test_a_day_starts_at_south_african_midnight_which_is_22_00_the_evening_before_in_utc(self):
        start = reports.floor_period("daily", NOW)
        self.assertEqual(start.astimezone(tz.utc), datetime(2026, 10, 20, 22, 0, tzinfo=tz.utc))
        # 00:30 on Wednesday in South Africa is still Tuesday in UTC, but it belongs to Wednesday's report
        early = datetime(2026, 10, 20, 22, 30, tzinfo=tz.utc)
        self.assertEqual(reports.floor_period("daily", early), start)
        self.assertEqual(reports.label_for("daily", reports.floor_period("daily", early)), "Wednesday 21 October 2026")

    def test_next_and_previous_periods_agree(self):
        for kind in ("daily", "weekly", "monthly"):
            start = reports.floor_period(kind, NOW)
            self.assertEqual(reports.previous_period(kind, reports.next_period(kind, start)), start)
        self.assertEqual(reports.next_period("monthly", datetime(2026, 12, 1, tzinfo=SAST)), datetime(2027, 1, 1, tzinfo=SAST))
        self.assertEqual(reports.previous_period("monthly", datetime(2026, 1, 1, tzinfo=SAST)), datetime(2025, 12, 1, tzinfo=SAST))

    def test_labels_are_readable(self):
        self.assertEqual(reports.label_for("daily", DAY), "Tuesday 20 October 2026")
        self.assertEqual(reports.label_for("weekly", datetime(2026, 10, 19, tzinfo=SAST)), "Week of 19 October to 25 October 2026")
        self.assertEqual(reports.label_for("monthly", datetime(2026, 10, 1, tzinfo=SAST)), "October 2026")


class BuildTests(TestCase):
    def setUp(self):
        self.n1, self.n2 = make_node("compute01"), make_node("compute02")
        t = DAY + timedelta(hours=8)
        for i in range(30):                                   # an hour of data on two nodes, plus a node that is off
            window(self.n1, t + timedelta(minutes=2 * i))
            window(self.n2, t + timedelta(minutes=2 * i), quality="NOT_LEARNED", score=None)
        window(make_node("compute03"), t, quality="UNRELIABLE", score=None)
        alert(self.n1, DAY + timedelta(hours=9), 30, status="CLOSED", outcome="CONFIRMED", recurrences=2)
        alert(self.n1, DAY + timedelta(hours=11), 10, status="CLOSED", outcome="FALSE_ALARM", metric="mem_available_ratio")
        alert(self.n2, DAY + timedelta(hours=12), 20, metric="cpu_busy", reason="no_data")
        alert(self.n2, DAY + timedelta(hours=13), 5, status="OPEN")
        alert(self.n2, DAY - timedelta(hours=5), 5)           # the day before
        alert(self.n2, DAY + timedelta(days=1, hours=1), 5)   # the day after
        self.d = reports.build("daily", DAY, DAY + timedelta(days=1), NOW)

    def test_alert_counts_only_cover_the_period(self):
        a = self.d["alerts"]
        self.assertEqual((a["opened"], a["still_active"], a["recovered_unreviewed"], a["reviewed"]), (4, 1, 1, 2))
        self.assertEqual((a["confirmed"], a["false_alarm"], a["came_back"], a["stopped_reporting"]), (1, 1, 1, 1))
        self.assertEqual(self.d["previous_opened"], 1)

    def test_precision_is_among_reviewed_alerts_only(self):
        self.assertEqual(self.d["alerts"]["precision_reviewed"], 0.5)

    def test_unreviewed_alerts_are_never_counted_as_right_or_wrong(self):
        d = reports.build("daily", DAY - timedelta(days=1), DAY, NOW)         # only one alert, nobody has reviewed it
        self.assertIsNone(d["alerts"]["precision_reviewed"])

    def test_durations_are_measured_on_the_reports_own_clock(self):
        # lasted 30, 10 and 20 minutes; the fourth alert was still active when the day ended at 00:00, 11 hours after 13:00
        self.assertEqual(self.d["alerts"]["longest_minutes"], 660.0)
        self.assertEqual(self.d["alerts"]["median_minutes"], 25.0)

    def test_node_and_cause_tables(self):
        top = self.d["by_node"][0]                                            # two alerts each; compute02 was unusual for longer
        self.assertEqual((top["node"], top["alerts"], top["minutes"]), ("compute02", 2, 680.0))
        self.assertEqual({r["node"]: r["unreviewed"] for r in self.d["by_node"]}, {"compute01": 0, "compute02": 2})
        self.assertEqual(self.d["by_metric"][0]["alerts"], 3)                 # CPU busy three times
        self.assertEqual(self.d["by_state"], {"IDLE": 4})

    def test_coverage_counts_hours_nodes_and_reliable_share(self):
        c = self.d["coverage"]
        self.assertEqual((c["nodes_seen"], c["hours_reporting"], c["hours_in_period"]), (2, 1, 24.0))
        self.assertEqual(c["node_hours"], 1.0)                                 # 30 reliable windows x 2 minutes
        self.assertEqual(c["reliable_share"], round(30 / 61, 3))

    def test_outages_are_counted(self):
        cycle(DAY + timedelta(hours=3), reachable=False)
        self.assertEqual(reports.build("daily", DAY, DAY + timedelta(days=1), NOW)["coverage"]["outage_cycles"], 1)

    def test_models_learned_in_the_period_are_listed(self):
        ModelVersion.objects.filter(label="m1").update(trained_at=DAY - timedelta(days=5))   # the helper model, long before
        ModelVersion.objects.create(label="auto-x", trained_at=DAY + timedelta(hours=10), train_start=DAY, train_end=DAY + timedelta(hours=9),
                                    baselines={"origin": "auto", "nodes": 48, "coverage": {"hours_per_node": 3.4}}, file_path="x",
                                    config=make_config())
        d = reports.build("daily", DAY, DAY + timedelta(days=1), NOW)
        self.assertEqual(d["learning"]["models"][0]["label"], "auto-x")
        self.assertEqual(d["learning"]["in_use"], "auto-x")

    def test_the_summary_speaks_plainly_and_is_honest_about_unreviewed_alerts(self):
        text = " ".join(self.d["summary"])
        for part in ("watched 2 nodes", "4 alerts were raised", "1 real fault", "1 false alarm", "50% real among those reviewed",
                     "still waiting for a quick review", "compute02", "cpu busy"):
            self.assertIn(part, text)

    def test_a_quiet_period_and_a_switched_off_cluster_are_reported_without_alarm(self):
        d = reports.build("daily", DAY - timedelta(days=3), DAY - timedelta(days=2), NOW)
        self.assertIn("did not receive usable data", d["summary"][0])
        self.assertIn("No alerts were raised", " ".join(d["summary"]))

    def test_a_report_read_back_from_the_database_keeps_its_south_african_day(self):
        start_utc = DAY.astimezone(tz.utc)                                   # how the database hands it back
        d = reports.build("daily", start_utc, start_utc + timedelta(days=1), NOW)
        self.assertEqual(d["label"], "Tuesday 20 October 2026")
        self.assertEqual(d["start"], "2026-10-20T00:00:00+02:00")

    def test_times_written_into_a_report_are_local(self):
        ModelVersion.objects.create(label="auto-y", trained_at=datetime(2026, 10, 20, 8, 0, tzinfo=tz.utc), train_start=DAY,
                                    train_end=DAY + timedelta(hours=9), baselines={"origin": "auto"}, file_path="y", config=make_config())
        d = reports.build("daily", DAY, DAY + timedelta(days=1), NOW)
        self.assertEqual(d["learning"]["models"][0]["trained_label"], "2026-10-20 10:00")     # 08:00 UTC

    def test_a_period_still_in_progress_is_cut_at_now_and_marked(self):
        d = reports.build("daily", datetime(2026, 10, 21, tzinfo=SAST), datetime(2026, 10, 22, tzinfo=SAST), NOW)
        self.assertTrue(d["partial"])
        self.assertEqual(d["coverage"]["hours_in_period"], 16.5)           # midnight to 16:30 South African time
        self.assertEqual(d["until_label"], "2026-10-21 16:30")
        self.assertIn("so far", " ".join(d["summary"]))


class GenerationTests(TestCase):
    def test_nothing_is_generated_before_the_platform_has_run(self):
        self.assertEqual(reports.generate_due(NOW), [])

    def test_every_finished_period_with_data_gets_one_report_and_never_two(self):
        cycle(datetime(2026, 10, 19, 10, tzinfo=tz.utc))
        cycle(datetime(2026, 10, 20, 10, tzinfo=tz.utc))
        first = reports.generate_due(NOW)
        kinds = sorted((r.kind, local(r.period_start).date().isoformat()) for r in first)
        self.assertEqual(kinds, [("daily", "2026-10-19"), ("daily", "2026-10-20")])   # the week and month have not finished
        self.assertEqual(reports.generate_due(NOW), [])
        self.assertEqual(Report.objects.count(), 2)

    def test_a_week_and_a_month_are_written_once_they_have_finished(self):
        cycle(datetime(2026, 10, 14, 10, tzinfo=tz.utc))
        later = datetime(2026, 11, 2, 6, tzinfo=tz.utc)                                # a Monday, after October ended
        kinds = {(r.kind, local(r.period_start).date().isoformat()) for r in reports.generate_due(later)}
        self.assertIn(("weekly", "2026-10-12"), kinds)
        self.assertIn(("monthly", "2026-10-01"), kinds)
        self.assertNotIn(("monthly", "2026-11-01"), kinds)

    def test_days_with_no_data_get_no_report(self):
        cycle(datetime(2026, 10, 14, 10, tzinfo=tz.utc))
        cycle(datetime(2026, 10, 20, 10, tzinfo=tz.utc))
        days = {local(r.period_start).date().isoformat() for r in reports.generate_due(NOW) if r.kind == "daily"}
        self.assertEqual(days, {"2026-10-14", "2026-10-20"})

    def test_a_saved_report_is_frozen_until_rebuilt(self):
        cycle(DAY + timedelta(hours=9))
        node = make_node("compute01")
        a = alert(node, DAY + timedelta(hours=9))
        report = reports.generate("daily", DAY, NOW)
        a.status, a.outcome, a.closed_at = "CLOSED", "CONFIRMED", NOW
        a.save()
        self.assertEqual(Report.objects.get().data["alerts"]["reviewed"], 0)
        reports.generate("daily", DAY, NOW, replace=True)
        report.refresh_from_db()
        self.assertEqual(report.data["alerts"]["reviewed"], 1)


class OldReportTests(TestCase):
    """Reports written when days were counted in UTC are replaced, not left to overlap the local days."""

    def test_a_report_that_starts_at_utc_midnight_is_removed_and_a_local_one_is_kept(self):
        old = Report.objects.create(kind="daily", period_start=datetime(2026, 10, 20, tzinfo=tz.utc), period_end=datetime(2026, 10, 21, tzinfo=tz.utc),
                                    generated_at=NOW, data={})
        keep = Report.objects.create(kind="daily", period_start=DAY, period_end=DAY + timedelta(days=1), generated_at=NOW, data={})
        weekly_old = Report.objects.create(kind="weekly", period_start=datetime(2026, 10, 19, tzinfo=tz.utc), period_end=datetime(2026, 10, 26, tzinfo=tz.utc),
                                           generated_at=NOW, data={})
        self.assertEqual(reports.drop_misaligned(), 2)
        self.assertEqual(list(Report.objects.all()), [keep])
        self.assertFalse(Report.objects.filter(pk__in=[old.pk, weekly_old.pk]).exists())
        self.assertEqual(reports.drop_misaligned(), 0)                       # nothing more to do the second time

    def test_the_worker_replaces_them_with_reports_for_local_days(self):
        cycle(datetime(2026, 10, 20, 10, tzinfo=tz.utc))
        Report.objects.create(kind="daily", period_start=datetime(2026, 10, 20, tzinfo=tz.utc), period_end=datetime(2026, 10, 21, tzinfo=tz.utc),
                              generated_at=NOW, data={})
        made = reports.generate_due(NOW)
        starts = {r.period_start.astimezone(tz.utc) for r in Report.objects.filter(kind="daily")}
        self.assertEqual(starts, {datetime(2026, 10, 19, 22, 0, tzinfo=tz.utc)})        # 20 October, South African time
        self.assertEqual([r.kind for r in made], ["daily"])


class ReportScreenTests(TestCase):
    def setUp(self):
        self.op = make_user("op")
        self.admin = make_user("boss", User.Role.ADMINISTRATOR)
        self.client.force_login(self.op)
        node = make_node("compute01")
        cycle(DAY + timedelta(hours=9))
        alert(node, DAY + timedelta(hours=9), 25, recurrences=1)
        self.report = reports.generate("daily", DAY, NOW)

    def test_pages_need_sign_in(self):
        self.client.logout()
        for url in (reverse("reports"), reverse("report_detail", args=[self.report.pk]), reverse("report_now", args=["weekly"]),
                    reverse("report_csv", args=[self.report.pk, "alerts"])):
            self.assertEqual(self.client.get(url).status_code, 302, url)

    def test_the_list_shows_reports_and_so_far_shortcuts(self):
        page = self.client.get(reverse("reports")).content.decode()
        for text in ("Tuesday 20 October 2026", "Today so far", "This week so far", "This month so far", "1 to review"):
            self.assertIn(text, page)

    def test_the_report_reads_in_plain_language_and_can_be_printed(self):
        page = self.client.get(reverse("report_detail", args=[self.report.pk])).content.decode()
        for text in ("Daily report", "In short", "1 alert was raised", "compute01", "Print or save as PDF", "Alerts (CSV)", "never labels its own alerts"):
            self.assertIn(text, page)
        self.assertNotIn("Rebuild", page)                                      # administrators only

    def test_only_administrators_can_rebuild(self):
        url = reverse("report_regenerate", args=[self.report.pk])
        self.assertEqual(self.client.post(url).status_code, 403)
        self.client.force_login(self.admin)
        self.assertEqual(self.client.post(url).status_code, 302)
        self.assertContains(self.client.get(reverse("report_detail", args=[self.report.pk])), "Rebuild")

    def test_a_so_far_report_renders_even_with_no_data(self):
        for kind in ("daily", "weekly", "monthly"):
            self.assertContains(self.client.get(reverse("report_now", args=[kind])), "so far")
        self.assertEqual(self.client.get("/reports/now/yearly/").status_code, 404)

    def test_csv_downloads(self):
        response = self.client.get(reverse("report_csv", args=[self.report.pk, "alerts"]))
        self.assertEqual(response["Content-Type"], "text/csv; charset=utf-8")
        self.assertIn("attachment", response["Content-Disposition"])
        rows = list(csv.reader(io.StringIO(response.content.decode())))
        self.assertEqual(rows[0][:3], ["alert", "node", "opened_sast"])        # the heading says which time zone the times are in
        self.assertEqual((rows[1][1], rows[1][2], rows[1][5], rows[1][8]), ("compute01", "2026-10-20 09:00", "25.0", "1"))
        nodes = list(csv.reader(io.StringIO(self.client.get(reverse("report_csv", args=[self.report.pk, "nodes"])).content.decode())))
        self.assertEqual(nodes[1][:2], ["compute01", "1"])
        self.assertEqual(self.client.get(f"/reports/{self.report.pk}/secrets.csv").status_code, 404)

    def test_csv_text_cannot_become_a_spreadsheet_formula(self):
        self.assertEqual(reports._safe("=HYPERLINK(1)"), "'=HYPERLINK(1)")
        self.assertEqual(reports._safe("compute01"), "compute01")

    def test_the_sidebar_links_to_reports(self):
        self.assertContains(self.client.get(reverse("reports")), 'href="/reports/"')


class WorkerReportTests(TestCase):
    def test_the_worker_writes_due_reports_and_survives_a_failure(self):
        from core.management.commands.run_worker import Worker
        from core.tests.test_cycle import config
        worker = Worker(config())
        with mock.patch("core.services.reports.generate_due", return_value=[]) as gen:
            worker.reports()
        gen.assert_called_once()
        with mock.patch("core.services.reports.generate_due", side_effect=RuntimeError("boom")):
            worker.reports()                                                   # must not raise
