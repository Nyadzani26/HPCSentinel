"""The detector: baselines, peer comparison and Isolation Forest (FR-11 to FR-15).

Three independent views of a node's current window are combined into one score:

* baseline  how far each feature is from the node's own normal for its load state,
            using the median and the median absolute deviation (robust to past faults);
* peer      how far each feature is from the other nodes in the same group right now;
* model     an Isolation Forest over all features together, which catches unusual
            combinations that no single feature shows.

Each view is expressed on the same scale, "robust standard deviations", so that the
alert threshold has one meaning. Nothing here reads the database or Prometheus.
"""
import math
from dataclasses import dataclass, field

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import IsolationForest

Z_CAP = 10.0
TOP_K = 3
MIN_TRAIN_ROWS_BASELINE = 30
MIN_TRAIN_ROWS_FOREST = 200
MIN_PEERS = 3
DEFAULT_WEIGHTS = {"baseline": 0.4, "peer": 0.2, "model": 0.4}
STATE_KEY = "__load_state"


def _mad_scale(values: np.ndarray) -> float:
    values = values[~np.isnan(values)]
    if len(values) == 0:
        return 0.0
    return float(1.4826 * np.median(np.abs(values - np.median(values))))


def _aggregate(zs: dict[str, float]) -> float | None:
    """Root-mean-square of the largest few deviations: one strong signal or several moderate ones."""
    if not zs:
        return None
    top = sorted(zs.values(), reverse=True)[:TOP_K]
    return math.sqrt(sum(v * v for v in top) / TOP_K)


def base_metric(feature: str) -> str:
    return feature.split("__")[0]


@dataclass
class Detector:
    baselines: dict = field(default_factory=dict)   # node -> state -> feature -> [median, scale, n]
    pooled: dict = field(default_factory=dict)      # feature -> pooled scale across all nodes
    forests: dict = field(default_factory=dict)     # "group|state" -> dict with model and calibration
    weights: dict = field(default_factory=lambda: dict(DEFAULT_WEIGHTS))

    # ---- learning -------------------------------------------------------------------------
    @classmethod
    def train(cls, tables: dict[str, pd.DataFrame], groups: dict[str, str], seed: int = 42,
              weights: dict | None = None) -> "Detector":
        """tables: {node: feature_table}; groups: {node: group name}."""
        det = cls(weights=dict(weights or DEFAULT_WEIGHTS))
        features = sorted({c for t in tables.values() if not t.empty for c in t.columns if c != STATE_KEY})
        all_rows = pd.concat([t for t in tables.values() if not t.empty], sort=False) if tables else pd.DataFrame()
        for feature in features:
            det.pooled[feature] = _mad_scale(all_rows[feature].to_numpy(dtype=float))
        for node, table in tables.items():
            if table.empty:
                continue
            det.baselines[node] = {}
            for state, part in table.groupby(STATE_KEY):
                if len(part) < MIN_TRAIN_ROWS_BASELINE:
                    continue
                det.baselines[node][state] = {
                    f: [float(part[f].median()), _mad_scale(part[f].to_numpy(dtype=float)), int(part[f].notna().sum())]
                    for f in features if f in part and part[f].notna().sum() >= MIN_TRAIN_ROWS_BASELINE
                }
        # one forest per (group, load state)
        by_key: dict[str, list[pd.DataFrame]] = {}
        for node, table in tables.items():
            if table.empty:
                continue
            for state, part in table.groupby(STATE_KEY):
                by_key.setdefault(f"{groups.get(node, 'default')}|{state}", []).append(part.drop(columns=[STATE_KEY]))
        for key, parts in by_key.items():
            data = pd.concat(parts, sort=False)
            if len(data) < MIN_TRAIN_ROWS_FOREST:
                continue
            keep = [c for c in data.columns if data[c].notna().mean() >= 0.8]
            data = data[keep]
            fill = data.median()
            x = data.fillna(fill).to_numpy(dtype=float)
            forest = IsolationForest(n_estimators=200, contamination="auto", random_state=seed, n_jobs=1).fit(x)
            raw = -forest.score_samples(x)
            scale = max(_mad_scale(raw), 1e-6)
            det.forests[key] = {"model": forest, "features": keep, "fill": fill.to_dict(),
                                "center": float(np.median(raw)), "scale": scale}
        return det

    # ---- scoring --------------------------------------------------------------------------
    def _floor(self, feature: str, median: float) -> float:
        return max(0.1 * self.pooled.get(feature, 0.0), 0.05 * abs(median), 1e-9)

    def baseline_deviations(self, node: str, state: str, feats: dict) -> dict:
        """{feature: (z, median, observed)} against the node's own baseline for this state."""
        table = self.baselines.get(node, {}).get(state)
        if not table:
            return {}
        out = {}
        for feature, value in feats.items():
            ref = table.get(feature)
            if ref is None or value is None or math.isnan(value):
                continue
            median, scale, _ = ref
            z = abs(value - median) / max(scale, self._floor(feature, median))
            out[feature] = (min(z, Z_CAP), median, value)
        return out

    def peer_deviations(self, node: str, group_nodes: list[str], window: dict[str, dict], states: dict[str, str]) -> dict:
        """{feature: (z, peer median, observed)} against nodes of the group in the same state."""
        peers = [n for n in group_nodes if n != node and states.get(n) == states.get(node) and n in window]
        if len(peers) < MIN_PEERS:
            return {}
        out = {}
        for feature, value in window[node].items():
            if not feature.endswith("__mean_15") or math.isnan(value):
                continue
            vals = np.array([window[p].get(feature, np.nan) for p in peers], dtype=float)
            vals = vals[~np.isnan(vals)]
            if len(vals) < MIN_PEERS:
                continue
            median = float(np.median(vals))
            scale = max(_mad_scale(vals), self._floor(feature, median))
            out[feature] = (min(abs(value - median) / scale, Z_CAP), median, value)
        return out

    def model_score(self, group: str, state: str, feats: dict) -> float | None:
        entry = self.forests.get(f"{group}|{state}")
        if not entry:
            return None
        row = np.array([[feats.get(c, entry["fill"][c]) if not _isnan(feats.get(c)) else entry["fill"][c]
                         for c in entry["features"]]], dtype=float)
        raw = float(-entry["model"].score_samples(row)[0])
        return float(min(max((raw - entry["center"]) / entry["scale"], 0.0), Z_CAP))

    def model_scores_batch(self, group: str, state: str, table: pd.DataFrame) -> pd.Series:
        """Model score for many rows at once (replay); same arithmetic as `model_score`."""
        entry = self.forests.get(f"{group}|{state}")
        if not entry or table.empty:
            return pd.Series(dtype=float)
        data = table.reindex(columns=entry["features"])
        x = data.fillna(pd.Series(entry["fill"])).to_numpy(dtype=float)
        raw = -entry["model"].score_samples(x)
        return pd.Series(np.clip((raw - entry["center"]) / entry["scale"], 0.0, Z_CAP), index=table.index)

    def combine(self, baseline: float | None, peer: float | None, model: float | None) -> float | None:
        parts = {"baseline": baseline, "peer": peer, "model": model}
        used = {k: v for k, v in parts.items() if v is not None}
        if not used:
            return None
        total_weight = sum(self.weights[k] for k in used)
        return sum(self.weights[k] * v for k, v in used.items()) / total_weight

    def score_window(self, node: str, group: str, group_nodes: list[str], window: dict[str, dict],
                     states: dict[str, str], model_value: float | None = None) -> dict:
        """Score one node. Returns the three views, the combined score and the evidence."""
        state = states[node]
        feats = window[node]
        base = self.baseline_deviations(node, state, feats)
        peer = self.peer_deviations(node, group_nodes, window, states)
        model = model_value if model_value is not None else self.model_score(group, state, feats)
        b = _aggregate({f: v[0] for f, v in base.items()})
        p = _aggregate({f: v[0] for f, v in peer.items()})
        return {"baseline": b, "peer": p, "model": model, "combined": self.combine(b, p, model),
                "baseline_detail": base, "peer_detail": peer, "learned": bool(base)}

    # ---- storage --------------------------------------------------------------------------
    def save(self, path) -> None:
        joblib.dump({"forests": self.forests, "pooled": self.pooled, "baselines": self.baselines,
                     "weights": self.weights}, path)

    @classmethod
    def load(cls, path) -> "Detector":
        data = joblib.load(path)
        return cls(baselines=data["baselines"], pooled=data["pooled"], forests=data["forests"], weights=data["weights"])


def _isnan(value) -> bool:
    return value is None or (isinstance(value, float) and math.isnan(value))
