"""Self-cleaning of learning data.

An unsupervised detector is only as good as the data it is told is normal. If a fault, a stress test or a
shutdown slips into the learning period, the detector learns it as normal and later misses it. Instead of asking
an administrator to mark every bad period by hand, the platform fits a first model, scores its own training
windows against it, and drops the ones that are far outside the node's normal. It then fits again on what is left.

The rule is deliberately conservative: only windows more than `z_cut` robust deviations out are removed, and never
more than `max_fraction` of a node's windows in a load state, so ordinary variation is kept and a node that is
genuinely noisy cannot have its whole baseline trimmed away.
"""
import math

import numpy as np
import pandas as pd

from core.pipeline.detector import LONG_SPAN, STATE_KEY, TOP_K, Z_CAP, Detector


def trim_contamination(detector: Detector, tables: dict[str, pd.DataFrame], groups: dict[str, str],
                       z_cut: float = 6.0, max_fraction: float = 0.05):
    """Return (clean_tables, trimmed_rows) where trimmed rows are those far outside their own baseline."""
    clean, trimmed = {}, 0
    for node, table in tables.items():
        keep = pd.Series(True, index=table.index)
        group = groups.get(node, "default")
        for state, part in table.groupby(STATE_KEY):
            ref = detector.baselines.get(node, {}).get(state) or detector.group_baselines.get(f"{group}|{state}")
            if not ref:
                continue
            features = [c for c in part.columns if c != STATE_KEY and not c.endswith(LONG_SPAN) and c in ref]
            if len(features) < TOP_K:
                continue
            median = np.array([ref[f][0] for f in features], dtype=float)
            scale = np.array([max(ref[f][1], detector._floor(f, ref[f][0])) for f in features], dtype=float)
            z = np.abs(part[features].to_numpy(dtype=float) - median) / scale
            z = np.nan_to_num(np.minimum(z, Z_CAP), nan=0.0)
            top = np.sort(z, axis=1)[:, -TOP_K:]
            score = np.sqrt((top ** 2).mean(axis=1))
            limit = int(math.floor(max_fraction * len(part)))
            flagged = np.where(score > z_cut)[0]
            if limit == 0 or len(flagged) == 0:
                continue
            worst = flagged[np.argsort(score[flagged])[::-1][:limit]]
            keep.loc[part.index[worst]] = False
        trimmed += int((~keep).sum())
        clean[node] = table[keep]
    return clean, trimmed
