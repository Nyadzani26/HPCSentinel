"""Configuration loading and validation (FR-05, NFR-10, Section 4.4)."""
import copy
import tempfile
from pathlib import Path

import yaml
from django.test import SimpleTestCase, TestCase

from core.config import ConfigError, build_query, load_config, node_name, validate

EXAMPLE = Path(__file__).resolve().parents[2] / "config" / "hpcsentinel.example.yml"


def example():
    return yaml.safe_load(EXAMPLE.read_text())


class ConfigTests(SimpleTestCase):
    def test_example_file_is_valid(self):
        cfg = load_config(EXAMPLE)
        self.assertGreaterEqual(len(cfg["metrics"]), 10)

    def test_every_example_query_is_limited_by_the_selector(self):
        cfg = load_config(EXAMPLE)
        for metric in cfg["metrics"]:
            self.assertIn('job="node"', build_query(cfg, metric))
            self.assertNotIn("$SELECTOR", build_query(cfg, metric))

    def test_query_without_selector_is_rejected(self):
        cfg = example()
        cfg["metrics"][0]["query"] = "node_load1"
        with self.assertRaisesRegex(ConfigError, r"cpu_busy.*\$SELECTOR"):
            validate(cfg)

    def test_missing_section_is_named(self):
        cfg = example()
        del cfg["detection"]
        with self.assertRaisesRegex(ConfigError, "Missing 'detection'"):
            validate(cfg)

    def test_duplicate_metric_name_is_rejected(self):
        cfg = example()
        cfg["metrics"].append(copy.deepcopy(cfg["metrics"][0]))
        with self.assertRaisesRegex(ConfigError, "used more than once"):
            validate(cfg)

    def test_bad_regular_expression_is_rejected(self):
        cfg = example()
        cfg["cluster"]["node_name_regex"] = "(["
        with self.assertRaisesRegex(ConfigError, "regular expression"):
            validate(cfg)

    def test_the_start_up_hold_back_is_checked(self):
        base = yaml.safe_load(EXAMPLE.read_text())
        for bad in (-1, 5000, "an hour", True):
            cfg = copy.deepcopy(base)
            cfg["detection"]["startup_minutes"] = bad
            with self.assertRaisesRegex(ConfigError, "startup_minutes"):
                validate(cfg)
        cfg = copy.deepcopy(base)
        cfg["cluster"]["uptime_query"] = "node_boot_time_seconds"            # not limited to the configured nodes
        with self.assertRaisesRegex(ConfigError, r"uptime_query.*\$SELECTOR"):
            validate(cfg)
        cfg["cluster"]["uptime_query"] = "time() - node_boot_time_seconds{$SELECTOR}"
        validate(cfg)

    def test_out_of_range_settings_are_rejected(self):
        for key, value, text in [
            ("retrieval_interval_seconds", 5, "at least 30"),
            ("persistence_windows", 0, "between 1 and 20"),
            ("alert_threshold", 0, "greater than zero"),
        ]:
            cfg = example()
            cfg["detection"][key] = value
            with self.assertRaisesRegex(ConfigError, text):
                validate(cfg)

    def test_malformed_yaml_and_missing_file(self):
        with tempfile.NamedTemporaryFile("w", suffix=".yml") as f:
            f.write("a: [unclosed")
            f.flush()
            with self.assertRaisesRegex(ConfigError, "not valid YAML"):
                load_config(f.name)
        with self.assertRaisesRegex(ConfigError, "Cannot read"):
            load_config("/nonexistent/hpcsentinel.yml")

    def test_node_name_mapping(self):
        cfg = example()
        cfg["cluster"]["node_name_regex"] = r"^10\.10\.10\.1(?P<n>\d\d):9100$"
        cfg["cluster"]["node_name_template"] = "compute{n}"
        self.assertEqual(node_name(cfg, "10.10.10.116:9100"), "compute16")
        self.assertIsNone(node_name(cfg, "192.168.1.5:9100"))


class SpanConfigTests(TestCase):
    def cfg(self, **det):
        import yaml
        from pathlib import Path
        c = yaml.safe_load((Path(__file__).resolve().parents[2] / "config" / "hpcsentinel.example.yml").read_text())
        c["detection"].update(det)
        return c

    def test_stability_shorter_than_the_longest_span_is_rejected(self):
        from core.config import ConfigError, validate
        with self.assertRaises(ConfigError):
            validate(self.cfg(feature_spans_minutes=[5, 15, 60], state_stability_minutes=15))

    def test_valid_longer_span_is_accepted(self):
        from core.config import validate
        validate(self.cfg(feature_spans_minutes=[5, 15, 60], state_stability_minutes=60))

    def test_bad_spans_are_rejected(self):
        from core.config import ConfigError, validate
        for bad in ([], [0], ["a"], [500]):
            with self.assertRaises(ConfigError):
                validate(self.cfg(feature_spans_minutes=bad))


class MinEffectConfigTests(SpanConfigTests):
    def test_min_effect_is_validated(self):
        from core.config import ConfigError, validate
        validate(self.cfg(min_effect={"cpu_busy": 0.03}))
        for bad in ([1], {"cpu_busy": -1}, {"cpu_busy": "x"}, {1: 0.1}, {"cpu_busy": True}):
            with self.assertRaises(ConfigError):
                validate(self.cfg(min_effect=bad))

    def test_the_shipped_configs_are_valid(self):
        from pathlib import Path
        from core.config import load_config
        base = Path(__file__).resolve().parents[2] / "config"
        for name in ("hpcsentinel.example.yml", "hpcsentinel.ranger.yml"):
            self.assertIn("cpu_busy", load_config(base / name)["detection"]["min_effect"])
