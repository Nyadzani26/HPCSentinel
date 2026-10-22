"""The chassis layout: which node sits where on the Overview. Display only, never part of scoring."""
from datetime import datetime, timezone as tz

from django.test import TestCase, override_settings
from django.urls import reverse
from pathlib import Path

from core.models import Chassis, Node, NodeGroup, User
from core.services import chassis
from core.services.timeline import fleet_timeline
from core.tests.helpers import make_user

EXAMPLE = str(Path(__file__).resolve().parents[2] / "config" / "hpcsentinel.example.yml")
NAMES = [f"compute{i:02d}" for i in range(1, 25)]


def nodes(names=NAMES):
    return {n: Node.objects.create(name=n, source_label=f"{n}:9100") for n in names}


class ParseTests(TestCase):
    def test_a_range_keeps_the_zero_padding_and_lists_every_node(self):
        layout = chassis.parse("Chassis 1: compute01-compute12", NAMES)
        self.assertEqual(layout, {"Chassis 1": NAMES[:12]})

    def test_the_second_prefix_can_be_left_out_and_single_names_can_be_added(self):
        layout = chassis.parse("Chassis 2: compute13-18, compute20, compute22 - compute23", NAMES)
        self.assertEqual(layout["Chassis 2"], ["compute13", "compute14", "compute15", "compute16", "compute17", "compute18",
                                               "compute20", "compute22", "compute23"])

    def test_blank_lines_and_comments_are_ignored_and_empty_text_means_no_chassis(self):
        self.assertEqual(chassis.parse("\n# top shelf\nA: compute01\n\n", NAMES), {"A": ["compute01"]})
        self.assertEqual(chassis.parse("", NAMES), {})
        self.assertEqual(chassis.parse(None, NAMES), {})

    def test_a_real_node_name_that_looks_like_a_range_is_taken_as_the_name(self):
        self.assertEqual(chassis.parse("A: rack-1-2", ["rack-1-2", "rack-1", "rack-2"]), {"A": ["rack-1-2"]})

    def test_mistakes_are_explained(self):
        cases = {
            "Chassis 1 compute01": "write it like",
            "Chassis 1:": "write it like",
            "A: compute01\nA: compute02": "listed twice",
            "A: compute01-compute03\nB: compute03": "compute03 is in both 'A' and 'B'",
            "A: compute09-compute02": "runs backwards",
            "A: compute01-node05": "two different name beginnings",
            "A: compute01-compute99": "does not know these nodes: compute25",
            "A: compute77": "does not know these nodes: compute77",
        }
        for text, expected in cases.items():
            with self.assertRaises(chassis.LayoutError, msg=text) as caught:
                chassis.parse(text, NAMES)
            self.assertIn(expected, str(caught.exception), text)

    def test_a_huge_range_is_refused(self):
        with self.assertRaises(chassis.LayoutError):
            chassis.parse("A: node0-node999999", ["node0"])

    def test_chassis_sort_the_way_people_count(self):
        self.assertEqual(sorted(["Chassis 10", "Chassis 2", "Chassis 1"], key=chassis.natural_key), ["Chassis 1", "Chassis 2", "Chassis 10"])


class ApplyTests(TestCase):
    def setUp(self):
        self.n = nodes()

    def test_nodes_are_placed_and_the_text_round_trips(self):
        text = "Chassis 1: compute01-compute12\nChassis 2: compute13-compute24"
        self.assertEqual(chassis.apply(chassis.parse(text, NAMES)), 24)
        self.assertEqual(Node.objects.get(name="compute05").chassis.name, "Chassis 1")
        self.assertEqual(Node.objects.get(name="compute24").chassis.name, "Chassis 2")
        self.assertEqual(chassis.layout_text(), text)

    def test_a_gap_or_a_short_run_is_written_out_in_full(self):
        chassis.apply(chassis.parse("A: compute01, compute02, compute04-compute06, compute09", NAMES))
        self.assertEqual(chassis.layout_text(), "A: compute01, compute02, compute04-compute06, compute09")

    def test_saving_replaces_the_old_layout_completely(self):
        chassis.apply(chassis.parse("Old: compute01-compute24", NAMES))
        chassis.apply(chassis.parse("New: compute01-compute03", NAMES))
        self.assertEqual(list(Chassis.objects.values_list("name", flat=True)), ["New"])
        self.assertEqual(Node.objects.filter(chassis__isnull=True).count(), 21)

    def test_saving_nothing_clears_every_chassis(self):
        chassis.apply(chassis.parse("A: compute01-compute24", NAMES))
        chassis.apply({})
        self.assertFalse(Chassis.objects.exists())
        self.assertEqual(Node.objects.filter(chassis__isnull=True).count(), 24)
        self.assertEqual(chassis.layout_text(), "")

    def test_it_never_touches_how_a_node_is_scored(self):
        group = NodeGroup.objects.create(name="all compute")
        Node.objects.update(group=group)
        chassis.apply(chassis.parse("A: compute01-compute12\nB: compute13-compute24", NAMES))
        self.assertEqual(set(Node.objects.values_list("group__name", flat=True)), {"all compute"})


class MapTests(TestCase):
    def test_the_history_map_rows_follow_the_chassis_in_number_order(self):
        n = nodes(["compute01", "compute02", "compute03"])
        chassis.apply(chassis.parse("Chassis 10: compute01\nChassis 2: compute02", [*n]))
        rows = fleet_timeline(3.0, now=datetime(2026, 10, 21, 17, 20, tzinfo=tz.utc))["rows"]
        # numbered chassis first (2 before 10), then the nodes that are in none, as on the node list below the map
        self.assertEqual([(r["node"], r["group"]) for r in rows], [("compute02", "Chassis 2"), ("compute01", "Chassis 10"), ("compute03", "")])


@override_settings(HPCSENTINEL_CONFIG=EXAMPLE, PROMETHEUS_URL="")
class SettingsBoxTests(TestCase):
    def setUp(self):
        nodes(NAMES)
        self.client.force_login(make_user("boss", role=User.Role.ADMINISTRATOR))

    def post(self, text):
        return self.client.post(reverse("controls"), {"action": "chassis", "layout": text}, follow=True)

    def test_the_box_is_on_the_settings_page_and_starts_empty(self):
        page = self.client.get(reverse("controls")).content.decode()
        self.assertIn("<h2>Chassis</h2>", page)
        self.assertIn("Save chassis", page)

    def test_saving_a_layout_places_the_nodes_and_the_box_shows_it_back(self):
        response = self.post("Chassis 1: compute01-compute12\nChassis 2: compute13-compute24")
        self.assertContains(response, "Chassis saved: 24 nodes placed in 2 chassis.")
        self.assertContains(response, "Chassis 2: compute13-compute24")
        self.assertEqual(Node.objects.get(name="compute13").chassis.name, "Chassis 2")

    def test_the_overview_then_shows_the_chassis(self):
        self.post("Chassis 1: compute01-compute12\nChassis 2: compute13-compute24")
        page = self.client.get(reverse("home")).content.decode()
        self.assertEqual(page.count("<h4><span>"), 2)
        self.assertLess(page.index("<span>Chassis 1</span><span>compute01–12"), page.index("<span>Chassis 2</span><span>compute13–24"))

    def test_a_mistake_is_explained_and_nothing_changes(self):
        self.post("Chassis 1: compute01-compute12")
        response = self.post("Chassis 9: compute01-compute99")
        self.assertContains(response, "does not know these nodes: compute25")
        self.assertEqual(Chassis.objects.get().name, "Chassis 1")

    def test_leaving_the_box_empty_clears_the_layout(self):
        self.post("Chassis 1: compute01-compute12")
        self.assertContains(self.post(""), "Chassis layout cleared")
        self.assertFalse(Chassis.objects.exists())

    def test_an_operator_cannot_change_it(self):
        self.client.force_login(make_user("op"))
        self.assertEqual(self.client.post(reverse("controls"), {"action": "chassis", "layout": "A: compute01"}).status_code, 403)
        self.assertFalse(Chassis.objects.exists())
