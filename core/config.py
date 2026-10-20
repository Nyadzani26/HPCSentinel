"""Loading and validating the platform configuration (FR-05, NFR-10).

The configuration says which metrics to use, how Prometheus names the nodes, and how
the detector behaves. A malformed file is rejected with a message that names the
problem, and the previous valid configuration stays in force (Section 4.4, FR-05).
"""
import re
from pathlib import Path

import yaml

SELECTOR_TOKEN = "$SELECTOR"


class ConfigError(Exception):
    """The configuration file is missing, unreadable or invalid."""


def _require(mapping, key, where, expected_type=None):
    if not isinstance(mapping, dict) or key not in mapping:
        raise ConfigError(f"Missing '{key}' in {where}.")
    value = mapping[key]
    if expected_type is not None and not isinstance(value, expected_type):
        raise ConfigError(f"'{key}' in {where} must be of type {expected_type.__name__}.")
    return value


def validate(cfg: dict) -> dict:
    prom = _require(cfg, "prometheus", "the file", dict)
    _require(prom, "step_seconds", "prometheus", int)
    _require(prom, "timeout_seconds", "prometheus", (int, float))

    cluster = _require(cfg, "cluster", "the file", dict)
    _require(cluster, "selector", "cluster", str)
    _require(cluster, "node_label", "cluster", str)
    regex = _require(cluster, "node_name_regex", "cluster", str)
    _require(cluster, "node_name_template", "cluster", str)
    try:
        re.compile(regex)
    except re.error as exc:
        raise ConfigError(f"'node_name_regex' in cluster is not a valid regular expression: {exc}.")

    det = _require(cfg, "detection", "the file", dict)
    interval = _require(det, "retrieval_interval_seconds", "detection", int)
    persistence = _require(det, "persistence_windows", "detection", int)
    threshold = _require(det, "alert_threshold", "detection", (int, float))
    if interval < 30:
        raise ConfigError("'retrieval_interval_seconds' in detection must be at least 30.")
    if not 1 <= persistence <= 20:
        raise ConfigError("'persistence_windows' in detection must be between 1 and 20.")
    if threshold <= 0:
        raise ConfigError("'alert_threshold' in detection must be greater than zero.")

    spans = det.get("feature_spans_minutes", [5, 15])
    if not isinstance(spans, list) or not spans or not all(isinstance(x, int) and 1 <= x <= 180 for x in spans):
        raise ConfigError("'feature_spans_minutes' in detection must be a list of whole numbers of minutes from 1 to 180.")
    stability = det.get("state_stability_minutes", max(spans))
    if not isinstance(stability, int) or stability < max(spans):
        raise ConfigError("'state_stability_minutes' in detection must be a whole number at least as long as the longest "
                          "feature span, otherwise scored windows would mix idle and loaded behaviour.")

    metrics = _require(cfg, "metrics", "the file", list)
    if not metrics:
        raise ConfigError("'metrics' must list at least one metric.")
    seen = set()
    for i, metric in enumerate(metrics, start=1):
        where = f"metric number {i}"
        name = _require(metric, "name", where, str)
        where = f"metric '{name}'"
        if name in seen:
            raise ConfigError(f"Metric name '{name}' is used more than once.")
        seen.add(name)
        query = _require(metric, "query", where, str)
        if SELECTOR_TOKEN not in query:
            raise ConfigError(f"The query of {where} must contain {SELECTOR_TOKEN} so that it is limited to the configured nodes.")
        _require(metric, "group", where, str)
    return cfg


def load_config(path) -> dict:
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"Cannot read configuration file {path}: {exc.strerror or exc}.")
    try:
        cfg = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"Configuration file {path} is not valid YAML: {exc}.")
    return validate(cfg)


def node_name(cfg: dict, source_label: str):
    """Translate the label Prometheus uses for a node into the platform's node name."""
    cluster = cfg["cluster"]
    match = re.match(cluster["node_name_regex"], source_label)
    if not match:
        return None
    return cluster["node_name_template"].format(**match.groupdict())


def build_query(cfg: dict, metric: dict) -> str:
    return metric["query"].replace(SELECTOR_TOKEN, cfg["cluster"]["selector"])
