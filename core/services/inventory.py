"""A read-only audit of the data available for learning (supports FR-28 and the readiness checks).

For every monitored node it counts the minutes of data Prometheus holds and how many of them are
reliable windows in each load state, then says whether that is enough to learn a baseline for the
state, using the same minimum row counts as the detector. Nothing is written.
"""
from datetime import timedelta

import pandas as pd

from core.pipeline import features as feat
from core.pipeline.detector import MIN_TRAIN_ROWS_BASELINE, MIN_TRAIN_ROWS_FOREST
from core.pipeline.jobs import _history, parse_time


def collect(cfg: dict, client, start, end) -> dict:
    start, end = parse_time(start), parse_time(end)
    stride = int(cfg["detection"].get("train_stride_minutes", 5))
    frames = _history(client, cfg, start, end)
    nodes = {}
    for label, frame in sorted(frames.items()):
        table = feat.feature_table(frame, cfg)
        table = table[(table.index >= pd.Timestamp(start)) & (table.index < pd.Timestamp(end))]
        reliable = table[table[feat.QUALITY_COL] == "RELIABLE"]
        states = reliable[feat.STATE_COL].value_counts()
        nodes[label] = {
            "minutes_with_data": int(frame.reindex(table.index)["cpu_busy"].notna().sum()) if "cpu_busy" in frame else 0,
            "reliable": int(len(reliable)),
            "idle": int(states.get("IDLE", 0)),
            "loaded": int(states.get("LOADED", 0)),
            "rows_idle": int(states.get("IDLE", 0)) // stride,
            "rows_loaded": int(states.get("LOADED", 0)) // stride,
            "days": sorted({ts.strftime("%m-%d") for ts in reliable.index}),
        }
    total_idle = sum(n["rows_idle"] for n in nodes.values())
    total_loaded = sum(n["rows_loaded"] for n in nodes.values())
    return {
        "start": start, "end": end, "stride_minutes": stride, "nodes": nodes,
        "rows_idle": total_idle, "rows_loaded": total_loaded,
        "nodes_own_idle": sum(n["rows_idle"] >= MIN_TRAIN_ROWS_BASELINE for n in nodes.values()),
        "nodes_own_loaded": sum(n["rows_loaded"] >= MIN_TRAIN_ROWS_BASELINE for n in nodes.values()),
        "forest_idle": total_idle >= MIN_TRAIN_ROWS_FOREST,
        "forest_loaded": total_loaded >= MIN_TRAIN_ROWS_FOREST,
    }


def render(report: dict) -> str:
    lines = [f"Data available for learning, {report['start']:%Y-%m-%d %H:%M} to {report['end']:%Y-%m-%d %H:%M} UTC "
             f"(one training row every {report['stride_minutes']} minutes)", ""]
    lines.append(f"{'node':<22}{'data min':>9}{'reliable':>9}{'idle rows':>10}{'loaded rows':>12}  days with reliable data")
    for label, n in report["nodes"].items():
        days = ",".join(n["days"][:8]) + (" ..." if len(n["days"]) > 8 else "")
        lines.append(f"{label:<22}{n['minutes_with_data']:>9}{n['reliable']:>9}{n['rows_idle']:>10}{n['rows_loaded']:>12}  {days}")
    total = len(report["nodes"])
    lines += [
        "",
        f"Nodes with at least {MIN_TRAIN_ROWS_BASELINE} idle rows of their own:   {report['nodes_own_idle']} of {total}",
        f"Nodes with at least {MIN_TRAIN_ROWS_BASELINE} loaded rows of their own: {report['nodes_own_loaded']} of {total}",
        f"Pooled idle rows {report['rows_idle']}, pooled loaded rows {report['rows_loaded']} "
        f"(a group model needs {MIN_TRAIN_ROWS_FOREST}): idle model {'yes' if report['forest_idle'] else 'NO'}, "
        f"loaded model {'yes' if report['forest_loaded'] else 'NO'}",
    ]
    return "\n".join(lines)
