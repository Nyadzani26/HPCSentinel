"""The last day of the whole cluster as one grid: a row per node, a column per quarter of an hour.

This feeds the history map on the Overview. It reads what the live cycles already stored; it scores nothing.
Each cell is one character so the whole map travels to the browser as a few kilobytes of text:

    n  normal                    u  unusual
    e  unusual during a cluster event (reported once, as the event, instead of once per node)
    q  not scored (no reliable reading, or the node was changing between idle and busy)
    .  no data at all (the node was off, or Prometheus did not answer)
"""
from datetime import datetime, timezone as dt_tz

from django.utils import timezone

from core.models import ClusterEvent, Node, NodeWindow

BUCKET_MINUTES = 15
HOURS = 24


def _ms(moment):
    return int(moment.timestamp() * 1000)


def fleet_timeline(level, now=None, hours=HOURS, bucket_minutes=BUCKET_MINUTES):
    """Return the grid as plain data. `level` is the alert level; a window at or above it counts as unusual."""
    now = now or timezone.now()
    step = bucket_minutes * 60
    columns = hours * 60 // bucket_minutes
    last = int(now.timestamp()) // step * step                       # start of the bucket that contains "now"
    start = datetime.fromtimestamp(last - (columns - 1) * step, tz=dt_tz.utc)
    end = datetime.fromtimestamp(last + step, tz=dt_tz.utc)

    seen = {}                                                        # (node id, column) -> [reliable, hot, highest score]
    windows = NodeWindow.objects.filter(cycle__mode="LIVE", window_end__gte=start, window_end__lt=end) \
        .values_list("node_id", "window_end", "quality", "combined_score")
    for node_id, at, quality, score in windows.iterator(chunk_size=5000):
        column = int((at - start).total_seconds() // step)
        cell = seen.setdefault((node_id, column), [False, False, 0.0])
        if quality == NodeWindow.Quality.RELIABLE:
            cell[0] = True
            if score is not None:
                cell[2] = max(cell[2], score)
                if level is not None and score >= level:
                    cell[1] = True

    rows = []
    for node in Node.objects.filter(is_active=True).select_related("group"):
        cells, scores = [], []
        for column in range(columns):
            cell = seen.get((node.pk, column))
            if cell is None:
                cells.append(".")
                scores.append(0)
                continue
            cells.append("u" if cell[1] else "n" if cell[0] else "q")
            scores.append(min(int(round(cell[2] * 10)), 999))
        rows.append({"node": node.name, "group": node.group.name if node.group else "", "cells": cells, "v": scores})
    rows.sort(key=lambda r: (r["group"], r["node"]))

    shown = []
    for event in ClusterEvent.objects.filter(started_at__lt=end).order_by("started_at"):
        finished = event.ended_at or event.last_seen_at
        if finished < start:
            continue
        first = max(int((event.started_at - start).total_seconds() // step), 0)
        final = min(int((finished - start).total_seconds() // step), columns - 1)
        involved = set(event.nodes)
        for row in rows:                                             # during an event an unusual node belongs to the event
            if row["node"] in involved:
                for column in range(first, final + 1):
                    if row["cells"][column] == "u":
                        row["cells"][column] = "e"
        shown.append({"id": event.pk, "s": _ms(event.started_at), "e": _ms(event.ended_at) if event.ended_at else None,
                      "nodes": event.peak_nodes, "of": event.peak_reliable})
    for row in rows:
        row["cells"] = "".join(row["cells"])
    return {"start": _ms(start), "step": bucket_minutes, "columns": columns, "level": level, "rows": rows, "events": shown,
            "has_data": any(set(r["cells"]) - {"."} for r in rows)}
