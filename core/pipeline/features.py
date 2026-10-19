"""Turning raw metric series into windows the detector can score (FR-09, FR-10, FR-11).

All functions here are pure: they take pandas objects and return pandas objects, and
touch neither the database nor Prometheus. This is what lets the live cycle and the
replay run exactly the same code (design decision D-5).
"""
import math

import pandas as pd

from core.config import build_query

SPANS_MINUTES = (5, 15, 60)
RESAMPLE = "1min"


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

    * mean over 5, 15 and 60 minutes and standard deviation over 15 minutes per metric,
      named like 'cpu_busy__mean_15';
    * '__load_state': IDLE, LOADED or TRANSITION from the CPU busy ratio (BR-09);
    * '__quality': RELIABLE or UNRELIABLE. A window is unreliable if too much of the last
      15 minutes is missing, if there are no fresh samples in the last 5 minutes, or if
      less than the stability period of data exist, or if the load state changed within the window (BR-02). Unreliable windows never raise alerts.
    """
    det = cfg["detection"]
    window = int(det.get("window_minutes", 15))
    out = {}
    for span in SPANS_MINUTES:
        means = frame.rolling(span, min_periods=1).mean()
        for column in frame.columns:
            out[f"{column}__mean_{span}"] = means[column]
    stds = frame.rolling(window, min_periods=3).std()
    for column in frame.columns:
        out[f"{column}__std_15"] = stds[column]
    table = pd.DataFrame(out, index=frame.index)

    # Load state (BR-09). A node is LOADED only if its 5-minute median CPU busy ratio has stayed
    # above the threshold for the whole stability period (by default 60 minutes, the longest
    # feature span), IDLE only if it has stayed below it. A window that straddles a change of
    # state is a transition and is not scored, because every feature of a scored window then
    # describes a single regime and neither baseline fits a mixture. The cost is that a node
    # is not scored for up to an hour after its load changes.
    if "cpu_busy" in frame:
        raw = (frame["cpu_busy"].rolling(5, min_periods=1).median() >= det.get("load_idle_cpu_busy", 0.10)).astype(int)
    else:
        raw = pd.Series(0, index=frame.index)
    stability = int(det.get("state_stability_minutes", max(SPANS_MINUTES)))
    low, high = raw.rolling(stability, min_periods=1).min(), raw.rolling(stability, min_periods=1).max()
    table[STATE_COL] = ["LOADED" if lo == 1 else "IDLE" if hi == 0 else "TRANSITION" for lo, hi in zip(low, high)]

    key = [c for c in QUALITY_KEY if c in frame.columns] or list(frame.columns)
    if key:
        missing = frame[key].isna().mean(axis=1).rolling(window, min_periods=1).mean()
        fresh = frame[key].notna().any(axis=1).astype(float).rolling(5, min_periods=1).max() > 0
        enough = pd.Series(range(len(frame)), index=frame.index) >= max(window, stability) - 1
        ok = (missing <= det.get("max_missing_fraction", 0.4)) & fresh & enough & (table[STATE_COL] != "TRANSITION")
    else:
        ok = pd.Series(False, index=frame.index)
    table[QUALITY_COL] = ["RELIABLE" if v else "UNRELIABLE" for v in ok]

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
