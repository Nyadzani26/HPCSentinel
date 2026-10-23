"""Turning raw metric series into windows the detector can score (FR-09, FR-10, FR-11).

All functions here are pure: they take pandas objects and return pandas objects, and
touch neither the database nor Prometheus. This is what lets the live cycle and the
replay run exactly the same code (design decision D-5).
"""
import math

import pandas as pd

from core.config import SELECTOR_TOKEN, build_query
from core.services.prometheus import PrometheusError

SPANS_MINUTES = (5, 15)
RESAMPLE = "1min"

# Minutes since each node booted, read from node_exporter's own boot time (no scheduler needed). It rides along in the
# frame as a column that starts with "__", which marks it as context and not as a metric to turn into features.
UPTIME_COL = "__uptime_minutes"
DEFAULT_UPTIME_QUERY = "time() - node_boot_time_seconds{$SELECTOR}"


def startup_minutes(cfg: dict) -> float:
    """How long a node is left unscored after it boots; 0 (the default) turns the hold-back off."""
    value = cfg["detection"].get("startup_minutes", 0)
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0 else 0.0


def metric_names(cfg: dict) -> list[str]:
    return [m["name"] for m in cfg["metrics"]]


def fetch_frames(client, cfg: dict, start: float, end: float, step: int | None = None) -> dict[str, pd.DataFrame]:
    """Read every configured metric for [start, end] and return {source_label: DataFrame}.

    Each DataFrame has one column per metric and a one-minute UTC index. A metric that
    Prometheus does not have simply produces no column (the readiness check reports it).
    """
    step = step or max(cfg["prometheus"]["step_seconds"], 60)
    label = cfg["cluster"]["node_label"]
    per_node: dict[str, dict[str, pd.Series]] = {}
    for metric in cfg["metrics"]:
        series = client.query_range(build_query(cfg, metric), start, end, step, label=label)
        for source_label, points in series.items():
            if not points:
                continue
            index = pd.to_datetime([p[0] for p in points], unit="s", utc=True)
            values = pd.Series([p[1] for p in points], index=index, dtype="float64")
            values = values.replace([math.inf, -math.inf], math.nan)
            per_node.setdefault(source_label, {})[metric["name"]] = values
    if startup_minutes(cfg):
        query = cfg["cluster"].get("uptime_query", DEFAULT_UPTIME_QUERY).replace(SELECTOR_TOKEN, cfg["cluster"]["selector"])
        try:
            uptime = client.query_range(query, start, end, step, label=label)
        except PrometheusError:
            uptime = {}                     # boot time unknown: nodes are scored as before, never silenced by a failed query
        for source_label, points in uptime.items():
            if points:
                index = pd.to_datetime([p[0] for p in points], unit="s", utc=True)
                minutes = pd.Series([p[1] / 60 for p in points], index=index, dtype="float64")
                per_node.setdefault(source_label, {})[UPTIME_COL] = minutes.replace([math.inf, -math.inf], math.nan)
    frames = {}
    for source_label, columns in per_node.items():
        frame = pd.DataFrame(columns).sort_index()
        frame = frame[~frame.index.duplicated(keep="last")]
        frames[source_label] = frame.resample(RESAMPLE).mean()
    return frames


QUALITY_KEY = ("cpu_busy", "mem_available_ratio")
STATE_COL = "__load_state"
QUALITY_COL = "__quality"


def feature_table(frame: pd.DataFrame, cfg: dict, instants=None) -> pd.DataFrame:
    """Features, load state and data quality for one node at each instant (FR-09, FR-10, FR-11).

    `frame` is on a regular one-minute grid, so a window of N minutes is N rows. The same
    function serves training, replay and the live cycle, which is what keeps them
    consistent (D-5). For every instant the table holds:

    * mean over each feature span (default 5 and 15 minutes) and standard deviation over 15 minutes per metric,
      named like 'cpu_busy__mean_15';
    * '__load_state': IDLE, LOADED or TRANSITION from the CPU busy ratio (BR-09);
    * '__quality': RELIABLE, UNRELIABLE or STARTING. STARTING is a node that booted less than `startup_minutes` ago: it
      is not scored, because the baselines describe nodes that have settled and a node in its first hour behaves
      differently (more NFS and network activity, daemons starting). A node whose boot time is unknown is never
      held back. A window is unreliable if too much of the last
      15 minutes is missing, if there are no fresh samples in the last 5 minutes, or if
      less than the stability period of data exist, or if the load state changed within the window (BR-02). Unreliable windows never raise alerts.
    """
    det = cfg["detection"]
    window = int(det.get("window_minutes", 15))
    out = {}
    spans = [int(x) for x in det.get("feature_spans_minutes", SPANS_MINUTES)]
    columns = [c for c in frame.columns if not c.startswith("__")]       # metrics only; context columns are not features
    for span in spans:
        means = frame[columns].rolling(span, min_periods=1).mean()
        for column in columns:
            out[f"{column}__mean_{span}"] = means[column]
    stds = frame[columns].rolling(window, min_periods=3).std()
    for column in columns:
        out[f"{column}__std_15"] = stds[column]
    table = pd.DataFrame(out, index=frame.index)

    # Load state (BR-09). A node is LOADED only if its 5-minute median CPU busy ratio has stayed
    # above the threshold for the whole stability period, IDLE only if it has stayed below it.
    # The stability period defaults to the longest feature span (15 minutes): every feature of a
    # scored window then describes one regime. A window that straddles a change of state is a
    # transition and is not scored, because neither baseline fits a mixture. Longer feature
    # spans (for example 60 minutes) need an equally long stability period, so a node is then
    # not scored for that long after its load changes. Tests on synthetic data showed that
    # 15-minute stability with a 60-minute feature gives false alerts on every node at every
    # job start and end; 15 minutes with 5 and 15 minute features gives none.
    if "cpu_busy" in frame:
        raw = (frame["cpu_busy"].rolling(5, min_periods=1).median() >= det.get("load_idle_cpu_busy", 0.10)).astype(int)
    else:
        raw = pd.Series(0, index=frame.index)
    stability = int(det.get("state_stability_minutes", max(spans)))
    low, high = raw.rolling(stability, min_periods=1).min(), raw.rolling(stability, min_periods=1).max()
    table[STATE_COL] = ["LOADED" if lo == 1 else "IDLE" if hi == 0 else "TRANSITION" for lo, hi in zip(low, high)]

    key = [c for c in QUALITY_KEY if c in columns] or columns
    if key:
        missing = frame[key].isna().mean(axis=1).rolling(window, min_periods=1).mean()
        fresh = frame[key].notna().any(axis=1).astype(float).rolling(5, min_periods=1).max() > 0
        enough = pd.Series(range(len(frame)), index=frame.index) >= max(window, stability) - 1
        ok = (missing <= det.get("max_missing_fraction", 0.4)) & fresh & enough & (table[STATE_COL] != "TRANSITION")
    else:
        ok = pd.Series(False, index=frame.index)
    table[QUALITY_COL] = ["RELIABLE" if v else "UNRELIABLE" for v in ok]
    hold = startup_minutes(cfg)
    if hold and UPTIME_COL in frame:
        table.loc[(frame[UPTIME_COL] < hold).to_numpy(), QUALITY_COL] = "STARTING"       # unknown (NaN) compares False

    if instants is not None:
        wanted = pd.DatetimeIndex(instants)
        table = table.reindex(wanted)
        table[QUALITY_COL] = table[QUALITY_COL].fillna("UNRELIABLE")
        table[STATE_COL] = table[STATE_COL].fillna("IDLE")
    return table


def training_table(frame: pd.DataFrame, cfg: dict, stride_minutes: int) -> pd.DataFrame:
    """Reliable feature rows every `stride_minutes`, for learning baselines and the model."""
    table = feature_table(frame, cfg)
    table = table[table[QUALITY_COL] == "RELIABLE"].drop(columns=[QUALITY_COL])
    return table.iloc[::stride_minutes]
