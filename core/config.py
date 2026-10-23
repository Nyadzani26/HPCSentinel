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

    for key, low in (("learn_days", 1), ("learn_min_interval_hours", 0), ("learn_min_hours", 1)):
        value = det.get(key, {"learn_days": 14, "learn_min_interval_hours": 0, "learn_min_hours": 6}[key])
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < low:
            raise ConfigError(f"'{key}' in detection must be a number, at least {low}.")
    for key, low, default in (("recovery_windows", 1, 5), ("no_data_minutes", 1, 30), ("merge_minutes", 0, 60)):
        value = det.get(key, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < low:
            raise ConfigError(f"'{key}' in detection must be a number, at least {low}.")
    fraction = det.get("cluster_event_fraction", 0.3)
    if isinstance(fraction, bool) or not isinstance(fraction, (int, float)) or not 0 < fraction <= 1:
        raise ConfigError("'cluster_event_fraction' in detection must be a share of the nodes, above 0 and at most 1.")
    minimum = det.get("cluster_event_min_nodes", 5)
    if isinstance(minimum, bool) or not isinstance(minimum, int) or minimum < 2:
        raise ConfigError("'cluster_event_min_nodes' in detection must be a whole number, at least 2.")
    hold = det.get("startup_minutes", 0)
    if isinstance(hold, bool) or not isinstance(hold, (int, float)) or not 0 <= hold <= 720:
        raise ConfigError("'startup_minutes' in detection must be a number of minutes from 0 to 720 (0 turns the hold-back off).")
    uptime_query = cluster.get("uptime_query")
    if uptime_query is not None and (not isinstance(uptime_query, str) or SELECTOR_TOKEN not in uptime_query):
        raise ConfigError(f"'uptime_query' in cluster must contain {SELECTOR_TOKEN} so that it is limited to the configured nodes.")
    if not isinstance(det.get("self_clean", True), bool):
        raise ConfigError("'self_clean' in detection must be true or false.")

    effects = det.get("min_effect", {})
    if not isinstance(effects, dict) or not all(isinstance(k, str) and isinstance(v, (int, float)) and not isinstance(v, bool)
                                                 and v >= 0 for k, v in effects.items()):
        raise ConfigError("'min_effect' in detection must map metric names to the smallest change that matters "
                          "(a number, zero or more).")

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
