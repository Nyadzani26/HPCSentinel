"""One scoring pass over the fleet and the alert rules that follow it (workflow 1).

`score_instant` and `AlertManager` are used unchanged by the live cycle and by replay
(design decision D-5), so what is evaluated is what runs in production.
"""
from datetime import timedelta

import pandas as pd

from core.models import Alert, ConfigVersion, ModelVersion, Node, NodeWindow
from core.pipeline import features as feat
from core.pipeline.detector import Detector, base_metric

DEFAULT_GROUP = "default"


def rows_at(frames: dict[str, pd.DataFrame], cfg: dict, instant: pd.Timestamp) -> dict[str, dict]:
    """Quality, load state and features of each node at one instant, for the live cycle."""
    rows = {}
    for node, frame in frames.items():
        row = feat.feature_table(frame, cfg, [instant]).iloc[0]
        rows[node] = row_to_entry(row)
    return rows


def row_to_entry(row: pd.Series) -> dict:
    quality = row[feat.QUALITY_COL]
    feats = {k: float(v) for k, v in row.items() if not k.startswith("__") and not pd.isna(v)} \
        if quality == "RELIABLE" else {}
    return {"quality": quality, "state": row[feat.STATE_COL] if quality == "RELIABLE" else None,
            "features": feats, "result": None}


def score_rows(detector: Detector | None, rows: dict[str, dict], groups: dict[str, str],
               model_values: dict[str, float] | None = None) -> dict[str, dict]:
    """Score every reliable node of one instant (FR-12 to FR-14).

    `model_values` holds precomputed model scores (replay computes them in batches).
    `result` stays None when the window is unreliable or the node has no baseline yet,
    in which case the quality becomes NOT_LEARNED (FR-09, FR-12).
    """
    entries = {n: dict(e) for n, e in rows.items()}
    if detector is None:
        for e in entries.values():
            if e["quality"] == "RELIABLE":
                e["quality"] = "NOT_LEARNED"
        return entries
    reliable = {n: e for n, e in entries.items() if e["quality"] == "RELIABLE"}
    window = {n: e["features"] for n, e in reliable.items()}
    states = {n: e["state"] for n, e in reliable.items()}
    by_group: dict[str, list[str]] = {}
    for n in reliable:
        by_group.setdefault(groups.get(n, DEFAULT_GROUP), []).append(n)
    for group, members in by_group.items():
        for node in members:
            result = detector.score_window(node, group, members, window, states,
                                           None if model_values is None else model_values.get(node))
            if result["combined"] is None or not result["learned"]:
                entries[node]["quality"] = "NOT_LEARNED"
            else:
                entries[node]["result"] = result
    return entries


def record_window(cycle, node: Node, entry: dict, window_end, model_version) -> NodeWindow:
    """Store the outcome of scoring one node in one cycle (FR-13, FR-14, FR-23)."""
    r = entry["result"]
    return NodeWindow(
        node=node, cycle=cycle, window_end=window_end, quality=entry["quality"], load_state=entry["state"],
        features={k: round(v, 6) for k, v in entry["features"].items()},
        baseline_score=None if r is None else r["baseline"], peer_score=None if r is None else r["peer"],
        model_score=None if r is None else r["model"], combined_score=None if r is None else r["combined"],
        model=model_version if r is not None else None,
    )


def explain(node: Node, group: str, entry: dict, threshold: float, persistence: int) -> dict:
    """Build the explanation shown with an alert (FR-19).

    It states the baseline, the observed value, the deviation, the load state and the
    metrics that contributed most, so an administrator can judge the alert without
    opening another tool (interviews: "what is it comparing against, and why").
    """
    result = entry["result"]
    best: dict[str, dict] = {}
    for source, detail in (("own baseline", result["baseline_detail"]), ("peer nodes", result["peer_detail"])):
        for feature, (z, reference, observed) in detail.items():
            metric = base_metric(feature)
            if metric not in best or z > best[metric]["z"]:
                pct = None if reference == 0 else (observed - reference) / abs(reference) * 100
                best[metric] = {"metric": metric, "feature": feature, "compared_with": source,
                                "observed": observed, "expected": reference,
                                "deviation_pct": None if pct is None else round(pct, 1), "z": round(z, 2)}
    contributors = sorted(best.values(), key=lambda c: c["z"], reverse=True)[:5]
    top = contributors[0] if contributors else None
    state = entry["state"].lower()
    summary = (f"{node.name} has been unusual for {persistence} consecutive windows while {state}. ")
    if top:
        summary += (f"The largest departure is {top['metric']}: {top['observed']:.4g} against an expected "
                    f"{top['expected']:.4g} ({top['compared_with']}).")
    return {
        "summary": summary, "load_state": entry["state"], "group": group, "threshold": threshold,
        "persistence_windows": persistence,
        "scores": {k: (None if result[k] is None else round(result[k], 2)) for k in ("baseline", "peer", "model", "combined")},
        "contributors": contributors,
    }


def history_context(node: Node, now) -> dict:
    """How often this node has alerted before and how those alerts ended (interview request)."""
    since = now - timedelta(days=30)
    past = Alert.objects.filter(node=node, mode=Alert.Mode.LIVE, opened_at__gte=since)
    return {
        "alerts_last_30_days": past.count(),
        "confirmed_last_30_days": past.filter(outcome=Alert.Outcome.CONFIRMED).count(),
        "false_alarms_last_30_days": past.filter(outcome=Alert.Outcome.FALSE_ALARM).count(),
    }


class AlertManager:
    """Raises an alert only after N consecutive abnormal windows (BR-01, FR-15)."""

    def __init__(self, cfg: dict, config_version: ConfigVersion, model_version: ModelVersion | None, mode: str):
        det = cfg["detection"]
        self.threshold = float(det["alert_threshold"])
        self.persistence = int(det["persistence_windows"])
        self.config_version, self.model_version, self.mode = config_version, model_version, mode

    def is_abnormal(self, window: NodeWindow) -> bool:
        return (window.quality == NodeWindow.Quality.RELIABLE and window.combined_score is not None
                and window.combined_score > self.threshold)

    def evaluate(self, node: Node, window: NodeWindow, group: str, entry: dict, now) -> Alert | None:
        if not self.is_abnormal(window) or self.model_version is None:
            return None
        live_alert = Alert.objects.filter(node=node, mode=self.mode,
                                          status__in=[Alert.Status.OPEN, Alert.Status.ACKNOWLEDGED]).first() \
            if self.mode == Alert.Mode.LIVE else None
        if live_alert is None and self.mode == Alert.Mode.REPLAY:
            prev = NodeWindow.objects.filter(node=node, cycle__mode=self.mode, window_end__lt=window.window_end) \
                .order_by("-window_end").select_related("alert").first()
            live_alert = prev.alert if prev is not None and prev.alert is not None and self.is_abnormal(prev) else None
        if live_alert is not None:
            if window.combined_score > live_alert.peak_score:
                live_alert.peak_score = window.combined_score
                live_alert.save(update_fields=["peak_score"])
            window.alert = live_alert
            window.save(update_fields=["alert"])
            return None
        # a node must be abnormal for N consecutive windows, counted after any earlier closure
        closed = Alert.objects.filter(node=node, mode=self.mode, status=Alert.Status.CLOSED).order_by("-closed_at").first()
        history = NodeWindow.objects.filter(node=node, cycle__mode=self.mode, window_end__lte=window.window_end)
        if closed is not None and closed.closed_at is not None and self.mode == Alert.Mode.LIVE:
            history = history.filter(window_end__gt=closed.closed_at)
        recent = list(history.order_by("-window_end")[: self.persistence])
        if len(recent) < self.persistence or not all(self.is_abnormal(w) for w in recent):
            return None
        explanation = explain(node, group, entry, self.threshold, self.persistence)
        explanation["history"] = history_context(node, now)
        alert = Alert.objects.create(
            node=node, mode=self.mode, opened_at=window.window_end,
            peak_score=max(w.combined_score for w in recent), explanation=explanation,
            model=self.model_version, config=self.config_version,
        )
        NodeWindow.objects.filter(pk__in=[w.pk for w in recent]).update(alert=alert)
        return alert
