"""One scoring pass over the fleet and the alert rules that follow it (workflow 1).

`score_instant` and `AlertManager` are used unchanged by the live cycle and by replay
(design decision D-5), so what is evaluated is what runs in production.
"""
from datetime import timedelta

import pandas as pd

from core.models import Alert, AlertAction, ConfigVersion, ModelVersion, Node, NodeWindow
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
    """Raises, updates, ends and re-opens alerts from the stream of scored windows.

    One alert is one story about one node. It opens only after N consecutive abnormal windows (BR-01, FR-15), keeps
    updating while the node stays abnormal, ends by itself once the node has been normal for a while or has stopped
    reporting, and is re-opened rather than duplicated if the node becomes abnormal again soon afterwards. People
    review alerts afterwards if they wish; none of this waits for them.

    Replay uses the same persistence rule but not the recover and merge steps, so replayed alerts are separate
    abnormal runs. That makes the false-alert counts measured in replay a conservative (higher) estimate.
    """

    def __init__(self, cfg: dict, config_version: ConfigVersion, model_version: ModelVersion | None, mode: str):
        det = cfg["detection"]
        self.threshold = float(det["alert_threshold"])
        self.persistence = int(det["persistence_windows"])
        self.recovery = int(det.get("recovery_windows", 5))
        self.no_data = timedelta(minutes=float(det.get("no_data_minutes", 30)))
        self.merge = timedelta(minutes=float(det.get("merge_minutes", 60)))
        self.config_version, self.model_version, self.mode = config_version, model_version, mode

    def is_abnormal(self, window: NodeWindow) -> bool:
        return (window.quality == NodeWindow.Quality.RELIABLE and window.combined_score is not None
                and window.combined_score > self.threshold)

    def is_normal(self, window: NodeWindow) -> bool:
        return (window.quality == NodeWindow.Quality.RELIABLE and window.combined_score is not None
                and window.combined_score <= self.threshold)

    def _latest(self, node: Node, group: str, entry: dict, window_end) -> dict:
        """What the node looks like now, kept on the alert so an old alert shows its latest state."""
        e = explain(node, group, entry, self.threshold, self.persistence)
        return {"at": window_end.isoformat(), "summary": e["summary"], "contributors": e["contributors"],
                "scores": e["scores"], "load_state": e["load_state"]}

    @staticmethod
    def _system_note(alert: Alert, action: str, note: str):
        from core.services.config_store import get_system_user
        AlertAction.objects.create(alert=alert, user=get_system_user(), action=action, note=note)

    def evaluate(self, node: Node, window: NodeWindow, group: str, entry: dict, now) -> Alert | None:
        """Handle one abnormal window. Returns an alert when one is raised or brought back, otherwise None."""
        if not self.is_abnormal(window) or self.model_version is None:
            return None
        live = self.mode == Alert.Mode.LIVE
        live_alert = Alert.objects.filter(node=node, mode=self.mode, status__in=Alert.ACTIVE).first() if live else None
        if live_alert is None and self.mode == Alert.Mode.REPLAY:
            prev = NodeWindow.objects.filter(node=node, cycle__mode=self.mode, window_end__lt=window.window_end) \
                .order_by("-window_end").select_related("alert").first()
            live_alert = prev.alert if prev is not None and prev.alert is not None and self.is_abnormal(prev) else None
        if live_alert is not None:
            fields = []
            if window.combined_score > live_alert.peak_score:
                live_alert.peak_score = window.combined_score
                fields.append("peak_score")
            if live:  # update in place: the alert always shows the latest state of the problem
                live_alert.last_seen_at = window.window_end
                live_alert.explanation = {**live_alert.explanation, "latest": self._latest(node, group, entry, window.window_end)}
                fields += ["last_seen_at", "explanation"]
            if fields:
                live_alert.save(update_fields=fields)
            window.alert = live_alert
            window.save(update_fields=["alert"])
            return None
        # a node must be abnormal for N consecutive windows, counted after the last time an alert ended
        history = NodeWindow.objects.filter(node=node, cycle__mode=self.mode, window_end__lte=window.window_end)
        resolved = None
        if live:
            closed = Alert.objects.filter(node=node, mode=self.mode, status=Alert.Status.CLOSED).order_by("-closed_at").first()
            resolved = Alert.objects.filter(node=node, mode=self.mode, status=Alert.Status.RESOLVED).order_by("-recovered_at").first()
            barrier = max([t for t in (closed.closed_at if closed else None, resolved.recovered_at if resolved else None) if t],
                          default=None)
            if barrier is not None:
                history = history.filter(window_end__gt=barrier)
        recent = list(history.order_by("-window_end")[: self.persistence])
        if len(recent) < self.persistence or not all(self.is_abnormal(w) for w in recent):
            return None
        peak = max(w.combined_score for w in recent)
        if live and resolved is not None and resolved.recovered_at is not None \
                and window.window_end - resolved.recovered_at <= self.merge:
            # flapping: the same problem came back, so continue the same alert instead of opening another
            resolved.flare_up(window.window_end)
            resolved.peak_score = max(resolved.peak_score, peak)
            resolved.explanation = {**resolved.explanation, "latest": self._latest(node, group, entry, window.window_end)}
            resolved.save(update_fields=["peak_score", "explanation"])
            NodeWindow.objects.filter(pk__in=[w.pk for w in recent]).update(alert=resolved)
            self._system_note(resolved, AlertAction.Action.RECUR, "The node became unusual again, so this alert was continued.")
            return resolved
        explanation = explain(node, group, entry, self.threshold, self.persistence)
        explanation["history"] = history_context(node, now)
        alert = Alert.objects.create(
            node=node, mode=self.mode, opened_at=window.window_end, last_seen_at=window.window_end if live else None,
            peak_score=peak, explanation=explanation, model=self.model_version, config=self.config_version,
        )
        NodeWindow.objects.filter(pk__in=[w.pk for w in recent]).update(alert=alert)
        return alert

    def settle(self, node: Node, window: NodeWindow) -> Alert | None:
        """Handle one window that is not abnormal: end an active alert if the problem is over. Returns the ended alert."""
        if self.mode != Alert.Mode.LIVE:
            return None
        alert = Alert.objects.filter(node=node, mode=self.mode, status__in=Alert.ACTIVE).first()
        if alert is None:
            return None
        reference = alert.last_seen_at or alert.opened_at
        since = NodeWindow.objects.filter(node=node, cycle__mode=self.mode, window_end__gt=reference)
        recent = list(since.order_by("-window_end")[: self.recovery])
        normal_run = len(recent) >= self.recovery and all(self.is_normal(w) for w in recent)
        reliable_since = since.filter(quality=NodeWindow.Quality.RELIABLE).exists()
        elapsed = window.window_end - reference
        silent = elapsed >= self.no_data and not reliable_since        # switched off, or Prometheus lost it
        stale = elapsed >= timedelta(hours=6)                           # never leave an alert active indefinitely
        if not (normal_run or silent or stale):
            return None
        reason = "recovered" if (normal_run or reliable_since) else "no_data"
        alert.resolve(window.window_end, reason)
        self._system_note(alert, AlertAction.Action.RECOVER,
                          "The node has been normal again, so this alert ended by itself." if reason == "recovered"
                          else "The node stopped reporting, so this alert ended by itself.")
        return alert


def apply_alert_rules(manager: "AlertManager", windows, entries: dict, groups: dict, now, cfg: dict) -> int:
    """Everything that follows scoring in one cycle: cluster events, then each node's alert rules. Returns alerts raised.

    Used unchanged by the live cycle. Replay does not use it: replay keeps one alert per abnormal run so that
    evaluation stays conservative and reproducible (see AlertManager).
    """
    from core.services import events

    abnormal = [w for w in windows if manager.is_abnormal(w)]
    reliable = sum(w.quality == NodeWindow.Quality.RELIABLE for w in windows)
    causes: dict[str, int] = {}
    for w in abnormal:
        result = entries[w.node.name].get("result")
        if result:
            top = explain(w.node, groups[w.node.name], entries[w.node.name], manager.threshold, manager.persistence)["contributors"][:1]
            if top:
                causes[top[0]["metric"]] = causes.get(top[0]["metric"], 0) + 1
    holding = events.update(cfg, now, [w.node.name for w in abnormal], reliable, causes) if manager.mode == Alert.Mode.LIVE else None
    active = set(Alert.objects.filter(mode=manager.mode, status__in=Alert.ACTIVE).values_list("node_id", flat=True))
    raised = 0
    for window in windows:
        name = window.node.name
        if manager.is_abnormal(window):
            if holding is not None and window.node_id not in active:
                continue                         # part of a cluster-wide event: reported once, not once per node
            raised += bool(manager.evaluate(window.node, window, groups[name], entries[name], now))
        elif window.node_id in active:
            manager.settle(window.node, window)
    return raised
