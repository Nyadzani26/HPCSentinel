"""Data-readiness report (FR-06): which configured metrics the connected Prometheus has.

For each metric the report says how many of the monitored nodes return data and flags
the cases that make a metric unfit for comparison between nodes: no data, data for only
some nodes, or the same non-zero reading on every node (as when an exporter reads one
shared source rather than each node, FR-09).
"""
from core.config import build_query


def check(cfg: dict, client, expected_nodes: int) -> list[dict]:
    label = cfg["cluster"]["node_label"]
    rows = []
    for metric in cfg["metrics"]:
        row = {"name": metric["name"], "group": metric["group"], "nodes": 0, "expected": expected_nodes,
               "status": "Missing", "note": ""}
        try:
            values = client.query_instant(build_query(cfg, metric), label=label)
        except Exception as exc:  # noqa: BLE001 - a bad query is reported, not raised
            row["status"], row["note"] = "Error", str(exc)[:120]
            rows.append(row)
            continue
        row["nodes"] = len(values)
        if values:
            full = expected_nodes == 0 or len(values) >= expected_nodes
            row["status"] = "Present" if full else "Partial"
            if not full:
                row["note"] = f"{expected_nodes - len(values)} nodes return no data"
            distinct = {round(v, 9) for v in values.values()}
            if len(values) >= 3 and len(distinct) == 1 and next(iter(distinct)) != 0:
                row["status"], row["note"] = "Suspect", "Every node reports the same value; it may not be measured per node."
        rows.append(row)
    return rows
