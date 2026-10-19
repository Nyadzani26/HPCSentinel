"""Work the worker does on request: train, replay and evaluate (FR-28 to FR-30, workflow 3).

The web application only writes a Job row; the worker picks it up and runs one of the
functions below. Replay uses the same scoring and alert code as the live cycle (D-5).
"""
import csv
import io
import logging
from datetime import datetime, timedelta, timezone as dt_tz
from pathlib import Path
from statistics import median

import pandas as pd
from django.conf import settings
from django.utils import timezone

from core.models import (Alert, CycleRun, EvaluationRun, ExcludedPeriod, ExperimentEntry, Job, ModelVersion, Node,
                         NodeWindow, User)
from core.pipeline import features as feat
from core.pipeline.detector import Detector
from core.pipeline.engine import AlertManager, DEFAULT_GROUP, record_window, row_to_entry, score_rows
from core.services.config_store import ensure_config_version, get_system_user

log = logging.getLogger(__name__)
CHUNK_HOURS = 6
GRACE = timedelta(minutes=30)


class JobError(Exception):
    """A job could not be carried out; the message is shown to the administrator."""


def parse_time(value) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=dt_tz.utc)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise JobError(f"'{value}' is not a valid date and time.") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt_tz.utc)


def _history(client, cfg, start: datetime, end: datetime) -> dict[str, pd.DataFrame]:
    """Read a long period in chunks so that no single query exceeds Prometheus limits."""
    parts: dict[str, list[pd.DataFrame]] = {}
    cursor = start
    while cursor < end:
        stop = min(cursor + timedelta(hours=CHUNK_HOURS), end)
        for label, frame in feat.fetch_frames(client, cfg, cursor.timestamp(), stop.timestamp(), step=60).items():
            parts.setdefault(label, []).append(frame)
        cursor = stop
    out = {}
    for label, frames in parts.items():
        merged = pd.concat(frames).sort_index()
        merged = merged[~merged.index.duplicated(keep="last")]
        out[label] = merged.resample(feat.RESAMPLE).mean()
    return out


def _node_frames(frames: dict[str, pd.DataFrame]) -> dict[Node, pd.DataFrame]:
    known = {n.source_label: n for n in Node.objects.filter(is_active=True).select_related("group")}
    return {known[label]: frame for label, frame in frames.items() if label in known}


def _drop_excluded(frame: pd.DataFrame) -> pd.DataFrame:
    """Rows inside an excluded period, and the hour after it, are never learned from (BR-03)."""
    for period in ExcludedPeriod.objects.all():
        lo = pd.Timestamp(period.start_at).tz_convert("UTC")
        hi = pd.Timestamp(period.end_at).tz_convert("UTC") + pd.Timedelta(minutes=60)
        frame = frame[(frame.index < lo) | (frame.index > hi)]
    return frame


def train(cfg: dict, client, start, end, label: str | None = None, user: User | None = None,
          job: Job | None = None) -> ModelVersion:
    """Learn baselines and the Isolation Forest from a period believed to be normal (FR-28)."""
    start, end = parse_time(start), parse_time(end)
    if end <= start:
        raise JobError("The end of the training period must be after its start.")
    if (end - start) < timedelta(hours=6):
        raise JobError("The training period must cover at least six hours.")
    frames = _node_frames(_history(client, cfg, start - timedelta(hours=1), end))
    if not frames:
        raise JobError("Prometheus returned no data for the monitored nodes in that period.")
    stride = int(cfg["detection"].get("train_stride_minutes", 5))
    tables, groups = {}, {}
    for node, frame in frames.items():
        table = feat.training_table(frame, cfg, stride)
        table = _drop_excluded(table)
        table = table[(table.index >= pd.Timestamp(start)) & (table.index <= pd.Timestamp(end))]
        if len(table):
            tables[node.name] = table
            groups[node.name] = node.group.name if node.group else DEFAULT_GROUP
    if not tables:
        raise JobError("There was no reliable data to learn from in that period.")
    detector = Detector.train(tables, groups, weights=cfg["detection"].get("weights"))
    version_no = ModelVersion.objects.count() + 1
    label = label or f"model-{version_no:03d}"
    store = Path(settings.MODEL_STORE)
    store.mkdir(parents=True, exist_ok=True)
    path = store / f"{label}.joblib"
    detector.save(path)
    config_version = ensure_config_version(cfg)
    return ModelVersion.objects.create(
        label=label, trained_at=timezone.now(), train_start=start, train_end=end,
        baselines={"nodes": len(tables), "rows": int(sum(len(t) for t in tables.values())),
                   "forests": sorted(detector.forests), "baselines": detector.baselines, "pooled": detector.pooled},
        file_path=str(path), config=config_version, job=job,
    )


def _clear_replay():
    NodeWindow.objects.filter(cycle__mode=CycleRun.Mode.REPLAY).delete()
    Alert.objects.filter(mode=Alert.Mode.REPLAY).delete()
    CycleRun.objects.filter(mode=CycleRun.Mode.REPLAY).delete()


def replay(cfg: dict, client, model: ModelVersion, start, end, stride_minutes: int | None = None) -> int:
    """Run the detection over a past period with the live code (FR-29). Returns alerts raised."""
    start, end = parse_time(start), parse_time(end)
    if end <= start:
        raise JobError("The end of the replay period must be after its start.")
    detector = Detector.load(model.file_path)
    stride = stride_minutes or max(1, cfg["detection"]["retrieval_interval_seconds"] // 60)
    frames = _node_frames(_history(client, cfg, start - timedelta(hours=1), end))
    if not frames:
        raise JobError("Prometheus returned no data for the monitored nodes in that period.")
    instants = pd.date_range(pd.Timestamp(start).ceil("min"), pd.Timestamp(end), freq=f"{stride}min", tz="UTC")
    tables = {node: feat.feature_table(frame, cfg, instants) for node, frame in frames.items()}
    groups = {n.name: (n.group.name if n.group else DEFAULT_GROUP) for n in tables}
    config_version = ensure_config_version(cfg)
    manager = AlertManager(cfg, config_version, model, Alert.Mode.REPLAY)
    model_scores = {}  # node name -> Series of model scores indexed by instant
    for node, table in tables.items():
        reliable = table[table[feat.QUALITY_COL] == "RELIABLE"]
        parts = [detector.model_scores_batch(groups[node.name], state, part.drop(columns=[feat.STATE_COL, feat.QUALITY_COL]))
                 for state, part in reliable.groupby(feat.STATE_COL)]
        model_scores[node.name] = pd.concat(parts) if parts else pd.Series(dtype=float)
    _clear_replay()
    raised = 0
    for instant in instants:
        rows = {node.name: row_to_entry(table.loc[instant]) for node, table in tables.items()}
        values = {name: float(series[instant]) for name, series in model_scores.items() if instant in series.index}
        entries = score_rows(detector, rows, groups, values)
        cycle = CycleRun.objects.create(mode=CycleRun.Mode.REPLAY, started_at=instant.to_pydatetime(),
                                        finished_at=instant.to_pydatetime(), source_reachable=True, config=config_version)
        nodes = {n.name: n for n in tables}
        windows = [record_window(cycle, nodes[name], entry, instant.to_pydatetime(), model) for name, entry in entries.items()]
        NodeWindow.objects.bulk_create(windows)
        saved = {w.node_id: w for w in NodeWindow.objects.filter(cycle=cycle)}
        for name, entry in entries.items():
            window = saved[nodes[name].pk]
            if manager.is_abnormal(window):
                if manager.evaluate(nodes[name], window, groups[name], entry, instant.to_pydatetime()):
                    raised += 1
    return raised


def import_experiments(text: str, user: User) -> int:
    """Import the log of injected anomalies used as ground truth (FR-29).

    CSV with the columns node, anomaly_type, start, end and an optional intensity.
    The whole file is rejected if any row is invalid, so the log is never half-imported.
    """
    reader = csv.DictReader(io.StringIO(text))
    required = {"node", "anomaly_type", "start", "end"}
    if not reader.fieldnames or not required <= set(reader.fieldnames):
        raise JobError("The file must have the columns node, anomaly_type, start and end.")
    nodes = {n.name: n for n in Node.objects.all()}
    types = set(ExperimentEntry.AnomalyType.values)
    entries = []
    for number, row in enumerate(reader, start=2):
        node = nodes.get((row["node"] or "").strip())
        if node is None:
            raise JobError(f"Row {number}: unknown node '{row['node']}'.")
        kind = (row["anomaly_type"] or "").strip().upper()
        if kind not in types:
            raise JobError(f"Row {number}: anomaly_type must be one of {', '.join(sorted(types))}.")
        s, e = parse_time(row["start"]), parse_time(row["end"])
        if e <= s:
            raise JobError(f"Row {number}: the end must be after the start.")
        entries.append(ExperimentEntry(node=node, anomaly_type=kind, start_at=s, end_at=e,
                                       intensity=(row.get("intensity") or "").strip(), imported_by=user))
    if not entries:
        raise JobError("The file contains no rows.")
    ExperimentEntry.objects.bulk_create(entries)
    return len(entries)


def evaluate(job: Job, model: ModelVersion, start, end) -> EvaluationRun:
    """Event-level metrics of the replayed alerts against the experiment log (FR-30).

    An episode is detected if an alert for that node opens during it or within 30 minutes
    after it ends. An alert that matches no episode of its node is a false alert.
    """
    start, end = parse_time(start), parse_time(end)
    episodes = list(ExperimentEntry.objects.filter(start_at__lt=end, end_at__gt=start).select_related("node"))
    alerts = list(Alert.objects.filter(mode=Alert.Mode.REPLAY, opened_at__gte=start, opened_at__lte=end))
    delays, detected = [], 0
    for ep in episodes:
        hits = sorted(a.opened_at for a in alerts if a.node_id == ep.node_id and ep.start_at <= a.opened_at <= ep.end_at + GRACE)
        if hits:
            detected += 1
            delays.append((hits[0] - ep.start_at).total_seconds())
    false_alerts = sum(
        1 for a in alerts
        if not any(ep.node_id == a.node_id and ep.start_at <= a.opened_at <= ep.end_at + GRACE for ep in episodes)
    )
    nodes = max(1, NodeWindow.objects.filter(cycle__mode=CycleRun.Mode.REPLAY).values("node").distinct().count())
    weeks = max((end - start).total_seconds() / (7 * 86400), 1e-9)
    return EvaluationRun.objects.create(
        job=job, model=model, period_start=start, period_end=end, episodes_total=len(episodes),
        episodes_detected=detected, median_delay_seconds=median(delays) if delays else None,
        false_alerts_per_node_week=false_alerts / nodes / weeks,
    )


def run_job(job: Job, cfg: dict, client) -> None:
    """Carry out one job and record how it ended (design decision D-3)."""
    job.status = Job.Status.RUNNING
    job.save(update_fields=["status"])
    p = job.parameters or {}
    try:
        if job.type == Job.Type.TRAIN:
            m = train(cfg, client, p["start"], p["end"], p.get("label") or None, job.requested_by, job)
            job.message = f"Trained {m.label} on {m.baselines['nodes']} nodes."
        elif job.type == Job.Type.REPLAY:
            model = ModelVersion.objects.get(pk=p["model_id"])
            n = replay(cfg, client, model, p["start"], p["end"], p.get("stride_minutes"))
            job.message = f"Replay finished; {n} alerts raised."
        elif job.type == Job.Type.EVALUATE:
            model = ModelVersion.objects.get(pk=p["model_id"])
            ev = evaluate(job, model, p["start"], p["end"])
            job.message = (f"Detected {ev.episodes_detected} of {ev.episodes_total} episodes; "
                           f"{ev.false_alerts_per_node_week:.2f} false alerts per node per week.")
        else:
            raise JobError(f"Unknown job type {job.type}.")
        job.status = Job.Status.DONE
    except (JobError, KeyError, ModelVersion.DoesNotExist) as exc:
        job.status, job.message = Job.Status.FAILED, f"{type(exc).__name__}: {exc}"
    except Exception as exc:  # noqa: BLE001 - a failed job must never stop the worker (NFR-08)
        log.exception("Job %s failed", job.pk)
        job.status, job.message = Job.Status.FAILED, f"Unexpected error: {exc}"
    job.finished_at = timezone.now()
    job.save(update_fields=["status", "message", "finished_at"])


def process_pending(cfg: dict, client) -> int:
    done = 0
    for job in Job.objects.filter(status=Job.Status.PENDING).order_by("requested_at"):
        run_job(job, cfg, client)
        done += 1
    return done
