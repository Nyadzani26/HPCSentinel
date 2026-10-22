"""Times are shown in South African time (settings.TIME_ZONE), while everything stored and calculated stays UTC."""
import re
from datetime import datetime, timedelta, timezone as tz
from pathlib import Path
from unittest import mock

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from core import forms
from core.localtime import stamp, zone_label, zone_name
from core.models import Alert, ClusterEvent, CycleRun, Node, NodeWindow, User
from core.pipeline import jobs
from core.tests.helpers import make_alert, make_config, make_node, make_user

EXAMPLE = str(Path(__file__).resolve().parents[2] / "config" / "hpcsentinel.example.yml")
EVENING = datetime(2026, 10, 21, 17, 56, tzinfo=tz.utc)             # 19:56 in South Africa


def without_scripts(html):
    """The page text without script blocks, where the chart code keeps a plain "UTC" fallback for browsers that cannot do zones."""
    return re.sub(r"<script.*?</script>", "", html, flags=re.S)


class ZoneTests(TestCase):
    def test_the_default_is_south_african_time(self):
        self.assertEqual(zone_name(), "Africa/Johannesburg")
        self.assertEqual(zone_label(), "SAST")
        self.assertEqual(stamp(EVENING), "2026-10-21 19:56")

    @override_settings(TIME_ZONE="Europe/London")
    def test_another_site_can_choose_its_own_zone(self):
        self.assertEqual(zone_name(), "Europe/London")
        self.assertEqual(stamp(datetime(2026, 7, 1, 12, 0, tzinfo=tz.utc)), "2026-07-01 13:00")
        self.assertEqual(zone_label(datetime(2026, 7, 1, 12, 0, tzinfo=tz.utc)), "BST")

    def test_a_time_typed_into_a_form_is_south_african_time(self):
        form = forms.TrainForm(data={"start": "2026-10-20T08:00", "end": "2026-10-20T10:00"})
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["start"].astimezone(tz.utc), datetime(2026, 10, 20, 6, 0, tzinfo=tz.utc))

    def test_times_with_no_zone_in_a_file_are_still_read_as_utc(self):
        """The cluster scripts write UTC, so an experiment log must not shift when the display zone changes."""
        self.assertEqual(jobs.parse_time("2026-10-21 14:30"), datetime(2026, 10, 21, 14, 30, tzinfo=tz.utc))
        self.assertEqual(jobs.parse_time("2026-10-21T14:30:00+02:00"), datetime(2026, 10, 21, 12, 30, tzinfo=tz.utc))

    def test_a_learned_model_is_named_after_the_time_people_see(self):
        with mock.patch("core.pipeline.jobs.train") as train:
            jobs.relearn({"detection": {"relearn_days": 14}}, client=None, now=datetime(2026, 10, 21, 14, 35, tzinfo=tz.utc))
        self.assertEqual(train.call_args.kwargs["label"], "auto-20261021-1635")


class ScreenTests(TestCase):
    def setUp(self):
        self.client.force_login(make_user("viewer"))

    def test_an_event_page_shows_south_african_time(self):
        event = ClusterEvent.objects.create(started_at=EVENING, last_seen_at=EVENING + timedelta(minutes=16), ended_at=EVENING + timedelta(minutes=16),
                                            peak_nodes=24, peak_reliable=47, nodes=["compute01"], causes={"cpu_busy": 3})
        page = self.client.get(reverse("event_detail", args=[event.pk])).content.decode()
        self.assertIn("19:56 SAST", page)
        self.assertIn("20:12 SAST", page)
        self.assertNotIn("17:56", page)

    def test_an_alert_page_shows_south_african_time(self):
        alert = make_alert()
        alert.opened_at = EVENING
        alert.save()
        page = self.client.get(reverse("alert_detail", args=[alert.pk])).content.decode()
        self.assertIn("21 Oct 2026, 19:56 SAST", page)

    def test_the_map_and_charts_are_told_which_zone_to_draw_in(self):
        node = Node.objects.create(name="compute01", source_label="c1")
        cycle = CycleRun.objects.create(mode="LIVE", started_at=timezone.now(), source_reachable=True, config=make_config())
        NodeWindow.objects.create(node=node, cycle=cycle, window_end=cycle.started_at, quality="RELIABLE", load_state="IDLE", combined_score=1.0)
        home = self.client.get(reverse("home")).content.decode()
        self.assertIn('data-tz="Africa/Johannesburg" data-tz-label="SAST"', home)
        node_page = self.client.get(reverse("node_detail", args=["compute01"])).content.decode()
        self.assertIn("'Africa/Johannesburg'", node_page)

    def test_no_page_still_labels_a_time_as_utc(self):
        alert = make_alert()
        event = ClusterEvent.objects.create(started_at=EVENING, last_seen_at=EVENING, ended_at=EVENING, peak_nodes=9, peak_reliable=12, nodes=[])
        urls = [reverse("home"), reverse("alert_list") + "?status=all", reverse("alert_detail", args=[alert.pk]), reverse("event_detail", args=[event.pk]),
                reverse("node_detail", args=[alert.node.name]), reverse("reports"), reverse("report_now", args=["daily"]), reverse("report_now", args=["weekly"])]
        for url in urls:
            text = without_scripts(self.client.get(url).content.decode())
            self.assertIsNone(re.search(r"\bUTC\b", text), url)


@override_settings(HPCSENTINEL_CONFIG=EXAMPLE, PROMETHEUS_URL="")
class SettingsPageTests(TestCase):
    def test_the_date_fields_start_at_south_african_times_and_say_so(self):
        self.client.force_login(make_user("boss", role=User.Role.ADMINISTRATOR))
        make_node("compute01")
        noon_utc = datetime(2026, 10, 21, 12, 30, tzinfo=tz.utc)                         # 14:30 in South Africa
        with mock.patch("django.utils.timezone.now", return_value=noon_utc):
            page = self.client.get(reverse("controls")).content.decode()
        self.assertIn('value="2026-10-19T14:00"', page)                                  # two days before 14:00 local, not 12:00
        self.assertIn("From (SAST)", page)
        self.assertIsNone(re.search(r"\bUTC\b", without_scripts(page)))
