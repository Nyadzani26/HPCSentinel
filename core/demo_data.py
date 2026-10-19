"""A stand-in for Prometheus that produces believable node metrics with injected faults.

It lets the tests (and the demo without a cluster) run the real pipeline end to end.
Every node follows the same daily pattern of idle and busy hours plus its own noise. A
fault is a (node, start, end, kind) tuple that changes some metrics on that node only.
"""
import zlib
from datetime import datetime, timezone

import numpy as np

from core.config import build_query

T0 = datetime(2026, 10, 1, tzinfo=timezone.utc)


class SyntheticClient:
    def __init__(self, cfg, n_nodes=8, faults=(), reachable=True, seed=1, busy_hours=(9, 17)):
        self.cfg, self.reachable, self.faults, self.busy_hours = cfg, reachable, list(faults), busy_hours
        self.labels = [f"10.10.10.{100 + i}:9100" for i in range(1, n_nodes + 1)]
        self.queries = {build_query(cfg, m): m["name"] for m in cfg["metrics"]}
        self.seed = seed

    def is_reachable(self):
        return self.reachable

    def query_instant(self, query, label="instance"):
        return {l: 1.0 for l in self.labels}

    def _fault(self, label, ts):
        for node_label, start, end, kind in self.faults:
            if node_label == label and start.timestamp() <= ts < end.timestamp():
                return kind
        return None

    def _value(self, metric, label, ts, rng):
        hour = (ts % 86400) / 3600
        busy = self.busy_hours[0] <= hour < self.busy_hours[1]
        cpu = (0.80 if busy else 0.02) + rng.normal(0, 0.02 if not busy else 0.04)
        fault = self._fault(label, ts)
        if fault == "cpu":
            cpu = 0.99 if busy else 0.90
        base = {
            "cpu_busy": cpu,
            "load_per_core": cpu * 1.05 + rng.normal(0, 0.01),
            "context_switch_rate": 2000 + 20000 * cpu + rng.normal(0, 300),
            "mem_available_ratio": 0.92 - 0.35 * cpu + rng.normal(0, 0.005),
            "swap_used_bytes": 0.0,
            "major_fault_rate": abs(rng.normal(0.2, 0.05)),
            "ecc_correctable_15m": 0.0,
            "net_rx_bytes_rate": 5e4 + 3e5 * cpu + rng.normal(0, 5e3),
            "net_tx_bytes_rate": 4e4 + 2e5 * cpu + rng.normal(0, 5e3),
            "net_error_rate": 0.0,
            "net_drop_rate": 0.0,
            "disk_busy_ratio": 0.01 + 0.05 * cpu + abs(rng.normal(0, 0.003)),
            "nfs_request_rate": 5 + 40 * cpu + rng.normal(0, 1),
            "processes_blocked": 0.0,
            "open_file_descriptors": 1500 + 400 * cpu + rng.normal(0, 10),
            "clock_offset_seconds": abs(rng.normal(0.0005, 0.0001)),
        }[metric]
        if fault == "memory":
            if metric == "mem_available_ratio":
                base = 0.25 + rng.normal(0, 0.01)
            if metric == "major_fault_rate":
                base = 400 + rng.normal(0, 20)
            if metric == "swap_used_bytes":
                base = 4e9
        if fault == "network" and metric in ("net_error_rate", "net_drop_rate"):
            base = 50 + rng.normal(0, 3)
        if fault == "ecc" and metric == "ecc_correctable_15m":
            base = 6.0
        return base

    def query_range(self, query, start, end, step, label="instance"):
        metric = self.queries.get(query)
        if metric is None:
            return {}
        out = {}
        first = int(start // step) * step
        times = np.arange(first, int(end) + 1, step)
        for i, node_label in enumerate(self.labels):
            rng = np.random.default_rng(zlib.crc32(f'{self.seed}|{i}|{metric}|{int(start)}'.encode()))
            points = []
            for ts in times:
                if self._gap(node_label, ts):
                    continue
                points.append((float(ts), float(self._value(metric, node_label, float(ts), rng))))
            out[node_label] = points
        return out

    gaps = ()

    def _gap(self, label, ts):
        return any(l == label and s.timestamp() <= ts < e.timestamp() for l, s, e in self.gaps)
