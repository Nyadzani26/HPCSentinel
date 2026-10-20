"""Zero-touch learning: the platform decides for itself when and from what to learn.

An administrator should not have to pick dates, name models or know when the cluster was healthy. Each check this
service looks at the data Prometheus already holds and decides one of four things:

  collecting  not enough clean data yet to learn anything useful; it keeps watching and says how much it has;
  learned     it fitted a new model, either the first one or because enough new data arrived to improve coverage;
  current     the newest model already reflects the data; nothing to do;
  waiting     it learned or checked recently and will look again later.

Learning data is "everything reliable in the last `learn_days` days (default 14)", minus time around alerts nobody
dismissed as false alarms and any maintenance period the administrator marked. The fit then cleans itself (see
core.pipeline.cleaning). The outcome of every check is stored so the interface can explain it in plain language.
"""
from datetime import datetime, timedelta

from django.utils import timezone

from core.models import ModelVersion, PlatformSetting
from core.pipeline import jobs
from core.pipeline.cycle import DetectionCycle
from core.pipeline.detector import MIN_TRAIN_ROWS_FOREST
from core.services.config_store import get_system_user

STATE_KEY = "autolearn_state"
MIN_NODES = 3
RECHECK_WHILE_COLLECTING = timedelta(minutes=30)


def last_state() -> dict:
    row = PlatformSetting.objects.filter(key=STATE_KEY).first()
    return row.value if row else {}


def _record(now, status: str, message: str, coverage: dict | None = None) -> dict:
    state = {"checked_at": now.isoformat(), "status": status, "message": message, "coverage": coverage or {}}
    PlatformSetting.objects.update_or_create(key=STATE_KEY, defaults={"value": state, "updated_by": get_system_user()})
    return state


def step(cfg: dict, client, now=None, force: bool = False) -> dict:
    """One check. Returns the recorded state; a new model, if any, is the newest ModelVersion."""
    now = now or timezone.now()
    det = cfg["detection"]
    days = int(det.get("learn_days", 14))
    min_interval = timedelta(hours=float(det.get("learn_min_interval_hours", 6)))
    weekly = timedelta(hours=float(det.get("relearn_hours", 168)) or 24 * 365)
    latest = ModelVersion.objects.order_by("-trained_at").first()
    state = last_state()

    if not force and state.get("checked_at"):
        age = now - datetime.fromisoformat(state["checked_at"])
        if age < (min_interval if latest else RECHECK_WHILE_COLLECTING):
            return {**state, "status": "waiting"}

    DetectionCycle(cfg, client).sync_nodes()
    end = now - timedelta(minutes=30)  # the newest windows are still settling
    start = end - timedelta(days=days)
    try:
        tables, groups = jobs.build_training_tables(cfg, client, start, end, jobs.suspect_spans(now, days))
    except jobs.JobError as exc:
        return _record(now, "collecting", f"No usable data yet: {exc}")
    stride = int(det.get("train_stride_minutes", 5))
    cov = jobs.coverage_of(tables, stride)
    min_hours = float(det.get("learn_min_hours", 6))
    if cov["rows"] < MIN_TRAIN_ROWS_FOREST or cov["nodes"] < MIN_NODES or cov["hours_per_node"] < min_hours:
        return _record(now, "collecting", f"Watching the cluster: {cov['hours_per_node']} of {min_hours:g} hours of clean data per node "
                                          f"so far. Learning starts by itself once there is enough.", cov)

    if latest and not force:
        old = (latest.baselines or {}).get("coverage") or {}
        improved = (cov["nodes_own_idle"] > old.get("nodes_own_idle", 0) or cov["nodes_own_loaded"] > old.get("nodes_own_loaded", 0)
                    or cov["rows"] >= 1.5 * max(1, old.get("rows", 0)))
        stale = now - latest.trained_at >= weekly
        if not (improved or stale):
            return _record(now, "current", "The newest model already reflects the data.", cov)

    first, last = min(t.index.min() for t in tables.values()), max(t.index.max() for t in tables.values())
    model = jobs.fit_and_register(cfg, tables, groups, first.to_pydatetime(), last.to_pydatetime(),
                                  label=f"auto-{now:%Y%m%d-%H%M}", user=get_system_user(), origin="auto")
    return _record(now, "learned", f"Learned {model.label} from {cov['hours_per_node']} hours of data per node on "
                                   f"{cov['nodes']} nodes.", model.baselines["coverage"])
