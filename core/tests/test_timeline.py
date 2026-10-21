"""The last-24-hours map on the Overview: one cell per node per quarter of an hour (core/services/timeline.py)."""
from datetime import datetime, timedelta, timezone as tz

from django.test import TestCase
from django.urls import reverse

from core.context import asset_version
from core.models import ClusterEvent, CycleRun, Node, NodeGroup, NodeWindow
from core.services.timeline import fleet_timeline
from core.tests.helpers import make_alert, make_config, make_node, make_user

NOW = datetime(2026, 10, 21, 17, 20, tzinfo=tz.utc)      # inside the 17:15 to 17:30 column
LEVEL = 3.0


class TimelineTests(TestCase):
    def setUp(self):
        self.config = make_config()
        self.nodes = {n: Node.objects.create(name=n, source_label=f"{n}:9100") for n in ("compute01", "compute02", "compute03")}

    def window(self, name, minutes_ago, score=1.0, quality="RELIABLE", mode="LIVE"):
        at = NOW - timedelta(minutes=minutes_ago)
        cycle = CycleRun.objects.create(mode=mode, started_at=at, source_reachable=True, config=self.config)
        NodeWindow.objects.create(node=self.nodes[name], cycle=cycle, window_end=at, quality=quality, load_state="IDLE", combined_score=score)

    def grid(self):
        return fleet_timeline(LEVEL, now=NOW)

    def row(self, name):
        return next(r for r in self.grid()["rows"] if r["node"] == name)

    def test_a_day_is_ninety_six_columns_and_the_last_one_holds_now(self):
        self.window("compute01", 0)
        grid = self.grid()
        self.assertEqual(grid["columns"], 96)
        self.assertEqual([r["node"] for r in grid["rows"]], ["compute01", "compute02", "compute03"])
        self.assertEqual(len(grid["rows"][0]["cells"]), 96)
        self.assertEqual(self.row("compute01")["cells"][-1], "n")
        last_column_start = datetime(2026, 10, 21, 17, 15, tzinfo=tz.utc)
        self.assertEqual(grid["start"] + 95 * 15 * 60 * 1000, int(last_column_start.timestamp() * 1000))

    def test_each_cell_says_normal_unusual_not_scored_or_no_data(self):
        self.window("compute01", 20, score=1.0)                     # 17:00 column: normal
        self.window("compute01", 35, score=1.0)                     # 16:45 column: one normal and one unusual window
        self.window("compute01", 33, score=4.5)
        self.window("compute01", 50, score=None, quality="UNRELIABLE")      # 16:30 column: only unreliable windows
        cells = self.row("compute01")["cells"]
        self.assertEqual(cells[94], "n")
        self.assertEqual(cells[93], "u")                            # one unusual window is enough
        self.assertEqual(cells[92], "q")
        self.assertEqual(cells[91], ".")                            # nothing at all
        self.assertEqual(set(self.row("compute02")["cells"]), {"."})

    def test_the_highest_score_in_a_cell_is_kept_for_the_tooltip(self):
        self.window("compute02", 20, score=2.4)
        self.window("compute02", 18, score=3.7)
        self.assertEqual(self.row("compute02")["v"][94], 37)

    def test_replay_windows_and_old_windows_are_not_drawn(self):
        self.window("compute01", 5, score=9.0, mode="REPLAY")
        self.window("compute01", 25 * 60, score=9.0)
        self.assertFalse(self.grid()["has_data"])

    def test_an_unusual_node_during_a_cluster_event_belongs_to_the_event(self):
        for name in ("compute01", "compute02"):
            self.window(name, 20, score=5.0)
        ClusterEvent.objects.create(started_at=NOW - timedelta(minutes=25), last_seen_at=NOW - timedelta(minutes=10),
                                    ended_at=NOW - timedelta(minutes=10), peak_nodes=9, peak_reliable=12, nodes=["compute01"])
        grid = self.grid()
        self.assertEqual(self.row("compute01")["cells"][94], "e")
        self.assertEqual(self.row("compute02")["cells"][94], "u")   # not part of the event, so still its own problem
        self.assertEqual([(e["nodes"], e["of"]) for e in grid["events"]], [(9, 12)])

    def test_an_event_that_ended_before_the_window_is_left_out(self):
        self.window("compute01", 5)
        ClusterEvent.objects.create(started_at=NOW - timedelta(days=3), last_seen_at=NOW - timedelta(days=3, minutes=-10),
                                    ended_at=NOW - timedelta(days=3, minutes=-10), peak_nodes=9, peak_reliable=12, nodes=["compute01"])
        self.assertEqual(self.grid()["events"], [])


class OverviewMapTests(TestCase):
    def setUp(self):
        self.client.force_login(make_user("viewer"))

    def test_the_map_is_only_drawn_when_there_is_something_to_draw(self):
        node = Node.objects.create(name="compute01", source_label="c1")
        self.assertNotContains(self.client.get(reverse("home")), 'id="watch-data"')
        cycle = CycleRun.objects.create(mode="LIVE", started_at=datetime.now(tz.utc), source_reachable=True, config=make_config())
        NodeWindow.objects.create(node=node, cycle=cycle, window_end=cycle.started_at, quality="RELIABLE", load_state="IDLE", combined_score=1.0)
        page = self.client.get(reverse("home"))
        self.assertContains(page, 'id="watch-data"')
        self.assertContains(page, reverse("node_detail", args=["compute01"]))     # the map links each row to its node
        self.assertContains(page, "Last 24 hours")
        self.assertContains(page, f"watch.js?v={asset_version()}")       # the map's script is fingerprinted like the stylesheet

    def test_the_headline_does_not_claim_normal_when_no_node_is_reporting(self):
        Node.objects.create(name="compute01", source_label="c1")
        self.assertContains(self.client.get(reverse("home")), "No node is reporting right now.")


class OverviewPanelTests(TestCase):
    """The side panel beside the map holds what needs a look, and is left out when there is nothing to show."""

    def setUp(self):
        self.client.force_login(make_user("viewer"))

    def test_the_panel_is_hidden_when_nothing_needs_a_look(self):
        make_node("compute01")
        self.assertContains(self.client.get(reverse("home")), 'class="deck solo bare"')

    def test_an_open_alert_fills_the_panel_with_a_link_to_it(self):
        alert = make_alert()
        page = self.client.get(reverse("home")).content.decode()
        self.assertNotIn("bare", page.split('class="deck')[1].split('"')[0])
        self.assertIn("Needs attention", page)
        self.assertIn(reverse("alert_detail", args=[alert.pk]), page)

    def test_an_alert_page_opens_with_its_score_chart_only_when_it_has_readings(self):
        alert = make_alert()
        url = reverse("alert_detail", args=[alert.pk])
        self.assertNotContains(self.client.get(url), "How unusual it was")
        cycle = CycleRun.objects.create(mode="LIVE", started_at=datetime.now(tz.utc), source_reachable=True, config=make_config())
        NodeWindow.objects.create(node=alert.node, cycle=cycle, window_end=cycle.started_at, quality="RELIABLE", load_state="IDLE",
                                  combined_score=4.2, alert=alert)
        page = self.client.get(url)
        self.assertContains(page, "How unusual it was")
        self.assertContains(page, 'id="score"')


class OverviewLayoutTests(TestCase):
    def setUp(self):
        self.client.force_login(make_user("viewer"))

    def test_nodes_are_listed_by_chassis_with_each_chassis_named_once(self):
        first, second = NodeGroup.objects.create(name="Chassis 1"), NodeGroup.objects.create(name="Chassis 2")
        Node.objects.create(name="compute13", source_label="c13", group=second)
        Node.objects.create(name="compute01", source_label="c01", group=first)
        Node.objects.create(name="compute99", source_label="c99")
        page = self.client.get(reverse("home")).content.decode()
        self.assertEqual(page.count('class="group-title"'), 3)
        self.assertLess(page.index("Chassis 1 <small>"), page.index("Chassis 2 <small>"))
        self.assertLess(page.index("Chassis 2 <small>"), page.index("Other nodes <small>"))

    def test_without_chassis_there_is_one_list_and_no_headings(self):
        Node.objects.create(name="compute01", source_label="c01")
        page = self.client.get(reverse("home")).content.decode()
        self.assertNotIn('class="group-title"', page)
        self.assertIn('data-name="compute01"', page)

    def test_every_page_asks_for_the_current_stylesheet_and_shows_which_version_is_running(self):
        """The stylesheet keeps its file name between deploys, so its address carries a fingerprint to defeat stale caches."""
        version = asset_version()
        page = self.client.get(reverse("home")).content.decode()
        self.assertIn(f"sentinel.css?v={version}", page)
        self.assertIn(f"interface {version}", page)
        self.client.logout()
        self.assertContains(self.client.get(reverse("login")), f"sentinel.css?v={version}")
