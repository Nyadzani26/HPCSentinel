"""Plain-language display of metrics (usability: values carry units, changes are stated in words)."""
from datetime import timedelta

from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone

from core import display
from core.templatetags.sentinel import ago, score_words
from core.tests.helpers import make_alert, make_user


class DisplayTests(SimpleTestCase):
    def test_values_carry_their_units(self):
        self.assertEqual(display.fmt(0.2456, "mem_available_ratio"), "24.6%")
        self.assertEqual(display.fmt(4_000_000_000, "swap_used_bytes"), "3.7 GB")
        self.assertEqual(display.fmt(55_000, "net_rx_bytes_rate"), "53.7 KB/s")
        self.assertEqual(display.fmt(0.00055, "clock_offset_seconds"), "0.55 ms")
        self.assertEqual(display.fmt(None, "cpu_busy"), "–")

    def test_unknown_metrics_get_a_readable_name(self):
        self.assertEqual(display.label("gpu_temperature_celsius"), "Gpu temperature celsius")
        self.assertEqual(display.label("cpu_busy"), "CPU busy")

    def test_changes_are_stated_in_words(self):
        self.assertEqual(display.change(0.24, 0.91)["text"], "74% lower")
        self.assertEqual(display.change(0.24, 0.91)["direction"], "down")
        self.assertEqual(display.change(5, 0)["text"], "above normal (normally zero)")
        self.assertEqual(display.change(2000, 1)["text"], "2,000× higher")
        self.assertEqual(display.change(1.0, 1.0)["text"], "no change")

    def test_headline_names_the_node_and_the_strongest_metric(self):
        ex = {"contributors": [{"metric": "mem_available_ratio", "observed": 0.24, "expected": 0.91}]}
        self.assertEqual(display.headline(ex, "compute03"), "compute03: available memory 74% lower than normal")
        self.assertEqual(display.headline({}, "compute03"), "compute03 is behaving unusually")

    def test_a_swing_is_called_a_variation_not_a_rise_in_the_metric(self):
        ex = {"contributors": [{"metric": "mem_available_ratio", "feature": "mem_available_ratio__std_15",
                                "observed": 0.2, "expected": 0.00002}]}
        self.assertEqual(display.headline(ex, "compute03"), "compute03: available memory variation 10,000× higher than normal")
        self.assertEqual(display.contributor_label(ex["contributors"][0]), "Available memory (variation)")
        level = {"metric": "mem_available_ratio", "feature": "mem_available_ratio__mean_15", "observed": 0.5, "expected": 0.98}
        self.assertEqual(display.contributor_label(level), "Available memory")
        self.assertEqual(display.contributor_label({"metric": "cpu_busy"}), "CPU busy")  # older alerts carry no feature name

    def test_score_words_and_relative_time(self):
        self.assertEqual(score_words(0.5), "normal")
        self.assertEqual(score_words(8), "very unusual")
        self.assertEqual(ago(timezone.now() - timedelta(seconds=5)), "just now")
        self.assertEqual(ago(timezone.now() - timedelta(minutes=12)), "12 minutes ago")


class UsabilityTests(TestCase):
    def test_first_run_guides_an_administrator(self):
        self.client.force_login(make_user("boss", role="ADMINISTRATOR"))
        html = self.client.get(reverse("home")).content.decode()
        self.assertIn("Get started", html)
        self.assertIn("Learning what normal looks like", html)
        self.assertNotIn("Train the first model", html)  # nobody is asked to train by hand

    def test_first_run_tells_an_operator_to_ask_an_administrator(self):
        self.client.force_login(make_user("op"))
        self.assertIn("Ask an Administrator", self.client.get(reverse("home")).content.decode())

    def test_sidebar_shows_how_many_alerts_need_attention(self):
        self.client.force_login(make_user("op"))
        make_alert()
        self.assertIn('aria-label="1 need attention"', self.client.get(reverse("alert_list")).content.decode())

    def test_alert_page_speaks_plainly(self):
        self.client.force_login(make_user("op"))
        alert = make_alert()
        alert.explanation = {"summary": "x", "load_state": "IDLE", "persistence_windows": 3, "threshold": 3.0, "scores": {},
                             "contributors": [{"metric": "mem_available_ratio", "observed": 0.24, "expected": 0.91, "z": 10.0,
                                               "compared_with": "own baseline"}]}
        alert.save()
        html = self.client.get(reverse("alert_detail", args=[alert.pk])).content.decode()
        for text in ("74% lower", "24.0%", "91.0%", "I'm looking into it", "3 checks in a row"):
            self.assertIn(text, html)

    def test_sidebar_shows_recovered_alerts_waiting_for_review_when_nothing_is_active(self):
        self.client.force_login(make_user("op"))
        make_alert(status="RESOLVED")
        html = self.client.get(reverse("alert_list")).content.decode()
        self.assertIn('aria-label="1 to review"', html)
        self.assertNotIn("need attention", html)

    def test_closing_form_explains_the_two_outcomes(self):
        self.client.force_login(make_user("op"))
        alert = make_alert(status="ACKNOWLEDGED")
        html = self.client.get(reverse("alert_detail", args=[alert.pk])).content.decode()
        self.assertIn("Real fault", html)
        self.assertIn("False alarm", html)
