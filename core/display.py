"""Turning metric names and numbers into words an administrator reads at a glance.

Every metric the platform shows goes through here, so values always carry their unit and a
plain name, and a change is stated as "73% lower than normal" rather than as a score.
"""
import math

# name -> (label, unit kind). Metrics not listed fall back to a readable form of their name.
METRICS = {
    "cpu_busy": ("CPU busy", "ratio"),
    "load_per_core": ("Load per core", "number"),
    "context_switch_rate": ("Context switches", "per_second"),
    "mem_available_ratio": ("Available memory", "ratio"),
    "swap_used_bytes": ("Swap in use", "bytes"),
    "major_fault_rate": ("Major page faults", "per_second"),
    "ecc_correctable_15m": ("Corrected memory errors (15 min)", "count"),
    "net_rx_bytes_rate": ("Network received", "bytes_per_second"),
    "net_tx_bytes_rate": ("Network sent", "bytes_per_second"),
    "net_error_rate": ("Network errors", "per_second"),
    "net_drop_rate": ("Network packets dropped", "per_second"),
    "disk_busy_ratio": ("Disk busy", "ratio"),
    "nfs_request_rate": ("NFS requests", "per_second"),
    "processes_blocked": ("Blocked processes", "count"),
    "open_file_descriptors": ("Open files", "count"),
    "clock_offset_seconds": ("Clock offset", "seconds"),
}


def label(metric: str) -> str:
    if metric in METRICS:
        return METRICS[metric][0]
    return metric.replace("_", " ").strip().capitalize()


def unit_kind(metric: str) -> str:
    return METRICS.get(metric, (None, "number"))[1]


def _si(value: float, step: float, units: list[str]) -> str:
    index = 0
    while abs(value) >= step and index < len(units) - 1:
        value /= step
        index += 1
    return f"{value:.0f} {units[index]}" if abs(value) >= 100 or index == 0 and value == int(value) \
        else f"{value:.1f} {units[index]}"


def fmt(value, metric: str) -> str:
    """A value with its unit, e.g. 0.2445 of mem_available_ratio -> '24.5%'."""
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "–"
    kind = unit_kind(metric)
    if kind == "ratio":
        return f"{value * 100:.1f}%" if abs(value) < 0.995 else f"{value * 100:.0f}%"
    if kind == "bytes":
        return _si(value, 1024.0, ["B", "KB", "MB", "GB", "TB"])
    if kind == "bytes_per_second":
        return _si(value, 1024.0, ["B/s", "KB/s", "MB/s", "GB/s"])
    if kind == "per_second":
        return f"{value:.3g}/s" if abs(value) < 1000 else f"{value / 1000:.1f}k/s"
    if kind == "seconds":
        return f"{value * 1000:.2f} ms" if abs(value) < 1 else f"{value:.2f} s"
    if kind == "count":
        return f"{value:,.0f}" if abs(value) >= 10 or value == int(value) else f"{value:.1f}"
    return f"{value:.3g}"


def change(observed, expected) -> dict:
    """Describe how far `observed` is from `expected` in words.

    Returns {"text": "73% lower", "direction": "down"|"up"|"same", "pct": float|None}.
    """
    if observed is None or expected is None:
        return {"text": "changed", "direction": "same", "pct": None}
    if expected == 0:
        if observed == 0:
            return {"text": "no change", "direction": "same", "pct": 0.0}
        return {"text": "above normal (normally zero)", "direction": "up", "pct": None}
    pct = (observed - expected) / abs(expected) * 100
    if abs(pct) < 1:
        return {"text": "no change", "direction": "same", "pct": pct}
    direction = "up" if pct > 0 else "down"
    word = "higher" if pct > 0 else "lower"
    size = abs(pct)
    if size >= 1000:
        times = (observed / expected) if expected else 0
        text = f"{times:,.0f}× higher" if pct > 0 and times else f"{size:,.0f}% {word}"
    else:
        text = f"{size:.0f}% {word}"
    return {"text": text, "direction": direction, "pct": pct}


def headline(explanation: dict, node_name: str) -> str:
    """One sentence saying what is wrong, from the strongest contributing metric."""
    contributors = (explanation or {}).get("contributors") or []
    if not contributors:
        return f"{node_name} is behaving unusually"
    top = contributors[0]
    delta = change(top.get("observed"), top.get("expected"))
    name = label(top["metric"]).lower()
    if delta["direction"] == "same":
        return f"{node_name}: {name} looks unusual"
    if "normally zero" in delta["text"]:
        return f"{node_name}: {name} above normal (normally zero)"
    return f"{node_name}: {name} {delta['text']} than normal"
