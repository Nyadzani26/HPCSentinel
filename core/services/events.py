"""Cluster-wide events (stage 4): one event instead of one alert per node.

Switching Ranger off, a power or cooling problem, a network switch failing or a site-wide job start changes many
nodes at the same moment. Forty-seven alerts for one cause bury the one thing the administrator needs to know and
would have to be reviewed one by one. This module notices the pattern, "far more nodes are unusual at once than
a coincidence of independent faults could explain", and records a single ClusterEvent while per-node alerts are
held back. When the event ends, any node that is still unusual alerts as normal, because its history of unusual
windows is unchanged.

A node that already has an active alert keeps updating it; only the opening of new alerts is held back.
"""
from core.models import ClusterEvent

DEFAULT_FRACTION = 0.3     # share of the nodes with usable data that must be unusual together
DEFAULT_MIN_NODES = 5      # and never fewer than this many, so a small cluster is not called an event by two nodes
QUIET_CYCLES = 3           # consecutive ordinary cycles before an event is considered over


def thresholds(cfg: dict) -> tuple[float, int]:
    det = cfg["detection"]
    return float(det.get("cluster_event_fraction", DEFAULT_FRACTION)), int(det.get("cluster_event_min_nodes", DEFAULT_MIN_NODES))


def is_event(abnormal: int, reliable: int, cfg: dict) -> bool:
    fraction, minimum = thresholds(cfg)
    return reliable > 0 and abnormal >= minimum and abnormal / reliable >= fraction


def update(cfg: dict, now, abnormal_nodes: list[str], reliable: int, causes: dict[str, int]) -> ClusterEvent | None:
    """Record this cycle. Returns the event while one is in progress (alerts for new nodes are held back), else None."""
    current = ClusterEvent.objects.filter(ended_at__isnull=True).order_by("-started_at").first()
    if is_event(len(abnormal_nodes), reliable, cfg):
        if current is None:
            current = ClusterEvent.objects.create(started_at=now, last_seen_at=now, nodes=[], causes={})
        current.last_seen_at = now
        current.quiet_cycles = 0
        current.nodes = sorted(set(current.nodes) | set(abnormal_nodes))
        if len(abnormal_nodes) > current.peak_nodes:
            current.peak_nodes, current.peak_reliable = len(abnormal_nodes), reliable
        merged = dict(current.causes)
        for metric, n in causes.items():
            merged[metric] = max(merged.get(metric, 0), n)
        current.causes = merged
        current.save()
        return current
    if current is not None:
        current.quiet_cycles += 1
        if current.quiet_cycles >= QUIET_CYCLES:
            current.ended_at = current.last_seen_at
            current.save(update_fields=["quiet_cycles", "ended_at"])
        else:
            current.save(update_fields=["quiet_cycles"])
            return current          # still settling: keep holding new alerts back for a few more cycles
    return None
