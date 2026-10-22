"""Screens of the platform (design Section 5.5, screens S-01 to S-05 and the controls).

Views only read data, call a service and render. Changes to alerts go through
ReviewService (BR-11); long work is requested by writing a Job for the worker (D-3).
"""
from datetime import datetime, timedelta
from functools import wraps

from django.conf import settings as django_settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import connection
from django.db.models import Count, Q
from django.http import Http404, HttpResponse, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from core import display, forms
from core.config import ConfigError, load_config
from core.models import (Alert, AlertAction, ClusterEvent, CycleRun, EvaluationRun, ExcludedPeriod, ExperimentEntry, InvalidTransition,
                         Job, ModelVersion, Node, NodeWindow, Report, User)
from core.pipeline.jobs import JobError, import_experiments
from core.services import readiness as readiness_service
from core.services import settings_store
from core.services import autolearn
from core.services import chassis as chassis_service
from core.services import timeline as timeline_service
from core.services import reports as reports_service
from core.services.review import PermissionDenied, ReviewService

STALE_AFTER = timedelta(minutes=10)


def health(request):
    """Used by deployments to confirm the web container can reach its database."""
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
        return JsonResponse({"status": "ok"})
    except Exception:  # noqa: BLE001 - report any database failure as unhealthy
        return JsonResponse({"status": "database unavailable"}, status=503)


def administrator_required(view):
    @wraps(view)
    @login_required
    def wrapper(request, *args, **kwargs):
        if not request.user.is_administrator:
            return HttpResponseForbidden("Only an Administrator can use this page.")
        return view(request, *args, **kwargs)
    return wrapper


def node_state(node, window, live_alert, in_event=False):
    """Colour and label of a node tile (S-02): normal, unusual, under review or unreliable.

    `in_event` is true for a node that is unusual right now but whose own alert is held back because it is part of a
    cluster-wide event that is still going (BR-13). Without it such a node would be labelled Normal beside a score
    that says otherwise.
    """
    if live_alert is not None:
        if live_alert.status == Alert.Status.ACKNOWLEDGED:
            return "review", "Under review"
        return "unusual", "Unusual"
    if window is None or window.quality != NodeWindow.Quality.RELIABLE:
        return "unknown", "No baseline" if window is not None and window.quality == "NOT_LEARNED" else "Unreliable"
    if in_event:
        return "unusual", "In cluster event"
    return "normal", "Normal"


def _held_back_nodes():
    """Names of the nodes involved in a cluster-wide event that is still going; their own alerts are held back."""
    names = set()
    for event in ClusterEvent.objects.filter(ended_at__isnull=True):
        names.update(event.nodes)
    return names


def _in_event(node, window, held_back, threshold):
    """True when the node belongs to a running event and its latest score is still above the alert level."""
    return bool(window is not None and window.combined_score is not None and threshold is not None
                and node.name in held_back and window.combined_score >= threshold)


def _alert_level(cycle):
    return ((cycle.config.content or {}).get("detection") or {}).get("alert_threshold") if cycle else None


@login_required
def home(request):
    """S-02 overview: what needs attention first, then every node (FR-20)."""
    last_cycle = CycleRun.objects.filter(mode=CycleRun.Mode.LIVE).order_by("-started_at").first()
    live_alerts = {a.node_id: a for a in Alert.objects.filter(mode="LIVE", status__in=Alert.ACTIVE)
                   .select_related("node").order_by("-opened_at")}
    latest = {}
    for w in NodeWindow.objects.filter(cycle__mode="LIVE", window_end__gte=timezone.now() - timedelta(hours=1)) \
            .order_by("window_end"):
        latest[w.node_id] = w
    order = {"unusual": 0, "review": 1, "unknown": 2, "normal": 3}
    held_back, level = _held_back_nodes(), _alert_level(last_cycle)
    tiles = []
    for node in Node.objects.filter(is_active=True).select_related("chassis").order_by("name"):
        window = latest.get(node.pk)
        css, label = node_state(node, window, live_alerts.get(node.pk), _in_event(node, window, held_back, level))
        tiles.append({"node": node, "css": css, "label": label, "window": window, "alert": live_alerts.get(node.pk)})
    tiles.sort(key=lambda t: (order[t["css"]], t["node"].name))
    counts = {k: sum(1 for t in tiles if t["css"] == k) for k in ("normal", "unusual", "review", "unknown")}
    by_chassis = {}
    for t in tiles:                       # tiles keep their order inside a chassis: problems first, then by name
        by_chassis.setdefault(t["node"].chassis.name if t["node"].chassis_id else "", []).append(t)
    named = sorted((k for k in by_chassis if k), key=chassis_service.natural_key)
    tile_groups = [{"name": k, "tiles": by_chassis[k]} for k in named]
    if "" in by_chassis:
        tile_groups.append({"name": "Other nodes" if named else "", "tiles": by_chassis[""]})
    in_event = sum(1 for t in tiles if t["label"] == "In cluster event")       # unusual, but part of one shared event
    source_ok = bool(last_cycle and last_cycle.source_reachable and timezone.now() - last_cycle.started_at < STALE_AFTER)
    model = ModelVersion.objects.order_by("-trained_at").first()
    setup = [
        {"done": bool(last_cycle and last_cycle.source_reachable), "title": "Connect to Prometheus",
         "text": "Set the address of your Prometheus server and test it.", "url": "controls", "action": "Open settings"},
        {"done": model is not None, "title": "Learning what normal looks like",
         "text": _learning_blurb(model), "url": "controls", "action": "See progress"},
        {"done": source_ok and model is not None, "title": "Monitoring runs by itself",
         "text": "The worker scores every node every two minutes and raises alerts here.", "url": None, "action": ""},
    ]
    return render(request, "fleet.html", {
        "watch": _watch(level) if tiles else None,
        "to_review": Alert.objects.filter(mode="LIVE", status=Alert.Status.RESOLVED).count(), "events": recent_events(),
        "tiles": tiles, "tile_groups": tile_groups, "counts": counts, "needs_attention": counts["unusual"] + counts["review"] - in_event, "in_event": in_event,
        "last_cycle": last_cycle, "source_ok": source_ok, "model": model,
        "attention": [t for t in tiles if t["alert"]], "setup": setup, "setup_done": all(x["done"] for x in setup),
        "total": len(tiles),
    })


def _watch(level):
    """The last 24 hours of every node for the history map, with the links the map needs. None when there is nothing to draw."""
    grid = timeline_service.fleet_timeline(level)
    if not grid["has_data"]:
        return None
    for row in grid["rows"]:
        row["url"] = reverse("node_detail", args=[row["node"]])
    for event in grid["events"]:
        event["url"] = reverse("event_detail", args=[event["id"]])
    return grid


def _learning_blurb(model):
    """One plain sentence about learning for the home page checklist."""
    if model is not None:
        return f"Learned automatically ({model.label}). It keeps learning as more data arrives."
    state = autolearn.last_state()
    return state.get("message") or "The platform learns by itself once it has watched the cluster for a few hours. Nothing to do."


def _baseline_lookup(node, state):
    """Normal values for this node and load state from the newest model: {feature: (median, scale)} and pooled scales."""
    model = ModelVersion.objects.order_by("-trained_at").first()
    if model is None or not state:
        return {}, {}
    table = model.baselines.get("baselines", {}).get(node.name, {}).get(state, {})
    return {f: (v[0], v[1]) for f, v in table.items()}, model.baselines.get("pooled", {})


@login_required
def node_detail(request, name):
    """S-03 node inspection: the score, then the metrics that are most unusual right now (FR-21, FR-23)."""
    node = get_object_or_404(Node, name=name)
    windows = list(NodeWindow.objects.filter(node=node, cycle__mode="LIVE").order_by("-window_end")[:360])
    windows.reverse()
    latest = windows[-1] if windows else None
    chart = [{"t": w.window_end.isoformat(), "v": round(w.combined_score, 3)} for w in windows if w.combined_score is not None]
    cycle = CycleRun.objects.filter(mode="LIVE").order_by("-started_at").select_related("config").first()
    threshold = _alert_level(cycle)
    effects = ((cycle.config.content or {}).get("detection") or {}).get("min_effect") or {} if cycle else {}
    state = latest.load_state if latest else None
    baseline, pooled = _baseline_lookup(node, state)
    series = {}
    for w in windows:  # one chart per metric, from the stored 5-minute means
        for key, value in w.features.items():
            if key.endswith("__mean_5"):
                series.setdefault(key[:-len("__mean_5")], []).append({"t": w.window_end.isoformat(), "v": value})
    metrics = []
    for metric, points in series.items():
        feature = f"{metric}__mean_5"
        ref = baseline.get(feature)
        z, band = 0.0, None
        if ref is not None:
            median, scale = ref
            # The same floor the detector applies, including the smallest change that matters for this metric, so this
            # page never calls a metric unusual when the score ignored it.
            floor = max(0.1 * pooled.get(feature, 0.0), 0.05 * abs(median), effects.get(metric, 0.0), 1e-9)
            width = 3 * max(scale, floor)
            band = [median - width, median + width] if width > 1e-6 else None
            z = abs(points[-1]["v"] - median) / max(scale, floor)
        metrics.append({"name": metric, "label": display.label(metric), "kind": display.unit_kind(metric), "points": points,
                        "band": band, "z": round(z, 2), "now": display.fmt(points[-1]["v"], metric),
                        "normal": None if ref is None else display.fmt(ref[0], metric)})
    metrics.sort(key=lambda m: (-m["z"], m["label"]))
    unusual = [m for m in metrics if m["z"] >= 3][:6]
    if not unusual:
        unusual = []
    rest = [m for m in metrics if m not in unusual]
    alerts = Alert.objects.filter(node=node, mode="LIVE").order_by("-opened_at")
    live_alert = alerts.filter(status__in=["OPEN", "ACKNOWLEDGED"]).first()
    markers = [a.opened_at.isoformat() for a in alerts if windows and a.opened_at >= windows[0].window_end]
    css, label = node_state(node, latest, live_alert, _in_event(node, latest, _held_back_nodes(), threshold))
    return render(request, "node_detail.html", {
        "node": node, "latest": latest, "chart": chart, "threshold": threshold, "state_css": css, "state_label": label,
        "unusual": unusual, "rest": rest, "alerts": alerts[:20], "markers": markers, "live_alert": live_alert,
        "has_band": bool(baseline),
    })


def recent_events(now=None):
    """Cluster-wide events that are still going or ended within the last day, newest first."""
    now = now or timezone.now()
    return list(ClusterEvent.objects.filter(Q(ended_at__isnull=True) | Q(ended_at__gte=now - timedelta(days=1))).order_by("-started_at")[:3])


@login_required
def event_detail(request, pk):
    """One cluster-wide event: which nodes, what changed, and why it is one event rather than many alerts."""
    event = get_object_or_404(ClusterEvent, pk=pk)
    causes = sorted(event.causes.items(), key=lambda kv: (-kv[1], kv[0]))
    return render(request, "event.html", {
        "event": event, "nodes": sorted(event.nodes),
        "causes": [{"label": display.label(m), "nodes": n} for m, n in causes],
        "share": round(100 * event.peak_nodes / event.peak_reliable) if event.peak_reliable else 0,
    })


ALERT_TABS = [("live", "Active"), ("review", "Needs review"), ("closed", "Reviewed"), ("all", "All")]


@login_required
def alert_list(request):
    """S-04 alerts: what is happening now, what recovered and wants a quick look, and what has been reviewed (FR-22)."""
    status = request.GET.get("status", "live")
    everything = Alert.objects.filter(mode="LIVE")
    counts = everything.aggregate(live=Count("pk", filter=Q(status__in=Alert.ACTIVE)), review=Count("pk", filter=Q(status="RESOLVED")),
                                  closed=Count("pk", filter=Q(status="CLOSED")), all=Count("pk"))
    qs = everything.select_related("node")
    if status == "live":
        qs = qs.filter(status__in=Alert.ACTIVE)
    elif status == "review":
        qs = qs.filter(status=Alert.Status.RESOLVED)
    elif status == "closed":
        qs = qs.filter(status=Alert.Status.CLOSED)
    elif status in Alert.Status.values:
        qs = qs.filter(status=status)
    elif status != "all":
        status = "live"
    node = request.GET.get("node", "").strip()
    if node:
        qs = qs.filter(node__name__icontains=node)
    errors = []
    for key, lookup in (("from", "opened_at__date__gte"), ("to", "opened_at__date__lte")):
        value = request.GET.get(key, "").strip()
        if value:
            try:
                qs = qs.filter(**{lookup: datetime.strptime(value, "%Y-%m-%d").date()})
            except ValueError:
                errors.append(f"'{value}' is not a date in the form YYYY-MM-DD.")
    return render(request, "alert_list.html", {
        "alerts": qs.order_by("-opened_at")[:200], "status": status, "node": node, "counts": counts,
        "filters": [(key, label, counts[key]) for key, label in ALERT_TABS],
        "date_from": request.GET.get("from", ""), "date_to": request.GET.get("to", ""), "errors": errors,
        "can_review": status in ("live", "review", "all"), "events": recent_events(),
    })


def _contributor_rows(contributors):
    top_z = max([c.get("z", 0) for c in contributors] or [1]) or 1
    rows = []
    for c in contributors:
        delta = display.change(c.get("observed"), c.get("expected"))
        rows.append({"label": display.contributor_label(c), "now": display.fmt(c.get("observed"), c["metric"]),
                     "normal": display.fmt(c.get("expected"), c["metric"]), "change": delta["text"],
                     "direction": delta["direction"], "share": round(100 * c.get("z", 0) / top_z),
                     "compared_with": c.get("compared_with", "")})
    return rows


@login_required
def alert_detail(request, pk):
    """S-05 one alert: what changed, why it was raised, and the next step (FR-19, FR-24 to FR-27)."""
    alert = get_object_or_404(Alert.objects.select_related("node", "model"), pk=pk)
    ex = alert.explanation or {}
    latest = ex.get("latest") or {}
    rows = _contributor_rows(latest.get("contributors") or ex.get("contributors", []))
    opening = _contributor_rows(ex.get("contributors", [])) if latest else []
    h = ex.get("history") or {}
    windows = list(alert.windows.order_by("window_end")[:400])
    chart = [{"t": w.window_end.isoformat(), "v": round(w.combined_score, 3)} for w in windows if w.combined_score is not None]
    steps = [("OPEN", "Active"), ("RESOLVED", "Recovered"), ("CLOSED", "Reviewed")]
    reached = {"OPEN": 0, "ACKNOWLEDGED": 0, "RESOLVED": 1, "CLOSED": 2}[alert.status]
    return render(request, "alert_detail.html", {
        "alert": alert, "rows": rows, "opening": opening, "latest_at": latest.get("at"), "history": h, "chart": chart,
        "threshold": ex.get("threshold"),
        "actions": alert.actions.select_related("user").order_by("created_at"), "windows": windows[-50:],
        "steps": [{"key": k, "label": lbl, "state": "done" if i < reached else "current" if i == reached else "todo"}
                  for i, (k, lbl) in enumerate(steps)],
        "close_form": forms.CloseForm(), "note_form": forms.NoteForm(),
        "load_state": (latest.get("load_state") or ex.get("load_state") or "").lower(),
    })


def _review(request, pk, operation, done="Done."):
    alert = get_object_or_404(Alert, pk=pk)
    service = ReviewService()
    try:
        operation(service, alert)
    except PermissionDenied as exc:
        messages.error(request, str(exc))
    except InvalidTransition as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, done)
    return redirect("alert_detail", pk=pk)


@login_required
@require_POST
def alert_acknowledge(request, pk):
    return _review(request, pk, lambda s, a: s.acknowledge(a, request.user, request.POST.get("note", "")),
                   "Alert acknowledged. Close it when you know what happened.")


@login_required
@require_POST
def alert_close(request, pk):
    form = forms.CloseForm(request.POST)
    if not form.is_valid():
        messages.error(request, " ".join(e for errs in form.errors.values() for e in errs))
        return redirect("alert_detail", pk=pk)
    return _review(request, pk, lambda s, a: s.close(a, request.user, form.cleaned_data["outcome"], form.cleaned_data["note"]),
                   "Alert closed.")


def _back(request, default="alert_list"):
    """Where to return after a one-click action: the page the person was on, if it is one of ours."""
    target = request.POST.get("next", "")
    if target and url_has_allowed_host_and_scheme(target, allowed_hosts={request.get_host()}):
        return redirect(target)
    return redirect(default)


@login_required
@require_POST
def alert_quick(request, pk):
    """One-click review from a list: real fault or false alarm, no acknowledging first."""
    outcome = request.POST.get("outcome")
    if outcome not in Alert.Outcome.values:
        messages.error(request, "Choose Real fault or False alarm.")
        return _back(request)
    alert = get_object_or_404(Alert, pk=pk)
    try:
        ReviewService().close(alert, request.user, outcome)
    except (PermissionDenied, InvalidTransition) as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, f"{alert.node.name} marked as {Alert.Outcome(outcome).label.lower()}.")
    return _back(request)


@login_required
@require_POST
def alert_bulk(request):
    """Review many alerts at once, for example every recovered alert from a maintenance window."""
    outcome = request.POST.get("outcome")
    ids = [int(i) for i in request.POST.getlist("ids") if i.isdigit()]
    if not ids:
        messages.error(request, "Tick at least one alert first.")
    elif outcome not in Alert.Outcome.values:
        messages.error(request, "Choose Real fault or False alarm.")
    else:
        try:
            n = ReviewService().close_many(ids, request.user, outcome)
        except PermissionDenied as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, f"{n} alert{'s' if n != 1 else ''} marked as {Alert.Outcome(outcome).label.lower()}.")
    return _back(request)


@login_required
@require_POST
def alert_reopen(request, pk):
    return _review(request, pk, lambda s, a: s.reopen(a, request.user, request.POST.get("note", "")), "Alert reopened.")


@login_required
@require_POST
def alert_note(request, pk):
    form = forms.NoteForm(request.POST)
    if not form.is_valid():
        messages.error(request, " ".join(e for errs in form.errors.values() for e in errs))
        return redirect("alert_detail", pk=pk)
    return _review(request, pk, lambda s, a: s.add_note(a, request.user, form.cleaned_data["note"]), "Note added.")


# ---- reports ---------------------------------------------------------------------------------
REPORT_KINDS = [("daily", "Daily"), ("weekly", "Weekly"), ("monthly", "Monthly")]
SO_FAR = {"daily": "Today so far", "weekly": "This week so far", "monthly": "This month so far"}


@login_required
def reports_list(request):
    """Saved daily, weekly and monthly reports, newest first, with a "so far" shortcut for each."""
    groups = [{"kind": k, "label": label, "so_far": SO_FAR[k],
               "reports": Report.objects.filter(kind=k).order_by("-period_start")[:12]} for k, label in REPORT_KINDS]
    return render(request, "reports.html", {"groups": groups, "any": Report.objects.exists()})


def _report_context(request, data, *, report=None, kind=None):
    return {"report": report, "d": data, "kind": data["kind"],
            "kind_label": dict(REPORT_KINDS)[data["kind"]], "csv_base": request.path.rstrip("/")}


@login_required
def report_detail(request, pk):
    report = get_object_or_404(Report, pk=pk)
    return render(request, "report.html", _report_context(request, report.data, report=report))


@login_required
def report_now(request, kind):
    """A report for the current day, week or month up to this moment. Not saved; the real one is saved when the period ends."""
    if kind not in dict(REPORT_KINDS):
        raise Http404
    now = timezone.now()
    start = reports_service.floor_period(kind, now)
    data = reports_service.build(kind, start, reports_service.next_period(kind, start), now)
    return render(request, "report.html", _report_context(request, data))


def _csv_response(text, name):
    response = HttpResponse(text, content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{name}"'
    return response


@login_required
def report_csv(request, pk, what):
    report = get_object_or_404(Report, pk=pk)
    return _csv_download(report.data, what)


@login_required
def report_now_csv(request, kind, what):
    if kind not in dict(REPORT_KINDS):
        raise Http404
    now = timezone.now()
    start = reports_service.floor_period(kind, now)
    return _csv_download(reports_service.build(kind, start, reports_service.next_period(kind, start), now), what)


def _csv_download(data, what):
    start, end = datetime.fromisoformat(data["start"]), datetime.fromisoformat(data["end"])
    stem = f"hpcsentinel-{data['kind']}-{start:%Y%m%d}"
    if what == "alerts":
        return _csv_response(reports_service.alerts_csv(start, end), f"{stem}-alerts.csv")
    if what == "nodes":
        return _csv_response(reports_service.nodes_csv(data), f"{stem}-nodes.csv")
    raise Http404


@administrator_required
@require_POST
def report_regenerate(request, pk):
    """Rebuild a saved report, for example to include reviews made after it was first generated."""
    report = get_object_or_404(Report, pk=pk)
    reports_service.generate(report.kind, report.period_start, replace=True)
    messages.success(request, "The report was rebuilt with the latest reviews.")
    return redirect("report_detail", pk=pk)


# ---- controls (Administrator only) ---------------------------------------------------------
def _queue(request, job_type, form, params, message=None):
    job = Job.objects.create(type=job_type, parameters=params, requested_by=request.user)
    messages.success(request, message or f"{job.get_type_display()} requested (job {job.pk}). The worker will start it shortly.")


@administrator_required
def controls(request):
    """Model and experiment controls: train, replay, evaluate, excluded periods, experiment log (FR-16, FR-28 to FR-30)."""
    today = timezone.localtime().replace(minute=0, second=0, microsecond=0)      # the fields are read in the same zone
    fmt_ = lambda d: d.strftime("%Y-%m-%dT%H:%M")  # noqa: E731
    train_initial = {"start": fmt_(today - timedelta(days=16)), "end": fmt_(today - timedelta(days=2))}
    test_initial = {"start": fmt_(today - timedelta(days=2)), "end": fmt_(today)}
    forms_ = {"train": forms.TrainForm(initial=train_initial),
              "replay": forms.ModelPeriodForm(prefix="replay", initial=test_initial),
              "evaluate": forms.ModelPeriodForm(prefix="eval", initial=test_initial), "excluded": forms.ExcludedPeriodForm(),
              "upload": forms.ExperimentUploadForm(), "chassis": forms.ChassisForm(initial={"layout": chassis_service.layout_text()})}
    try:
        base_cfg = _file_config()
    except ConfigError as exc:
        messages.error(request, f"The configuration file cannot be used: {exc}")
        return redirect("home")
    cfg = settings_store.effective_config(base_cfg)
    forms_["settings"] = forms.SettingsForm(initial={
        "prometheus_url": cfg["prometheus"]["url"], "alert_threshold": cfg["detection"]["alert_threshold"],
        "persistence_windows": cfg["detection"]["persistence_windows"],
        "sensitivity": forms.sensitivity_of(cfg["detection"]["alert_threshold"], cfg["detection"]["persistence_windows"])})
    readiness = None
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "settings":
            form = forms_["settings"] = forms.SettingsForm(request.POST)
            if form.is_valid():
                d = form.cleaned_data
                try:
                    settings_store.save(request.user, url=d["prometheus_url"], threshold=d["alert_threshold"],
                                        persistence=d["persistence_windows"])
                except settings_store.SettingError as exc:
                    form.add_error(None, str(exc))
                else:
                    messages.success(request, "Settings saved. The worker applies them at its next cycle.")
                    return redirect("controls")
        elif action in ("test_connection", "readiness"):
            url = request.POST.get("prometheus_url") or cfg["prometheus"]["url"]
            try:
                client = settings_store.make_client({**cfg, "prometheus": {**cfg["prometheus"], "url": url}})
            except Exception as exc:  # noqa: BLE001 - shown to the administrator
                messages.error(request, str(exc))
            else:
                if not client.is_reachable():
                    messages.error(request, f"Prometheus did not answer at {url}.")
                elif action == "test_connection":
                    messages.success(request, f"Prometheus answered at {url}.")
                else:
                    readiness = readiness_service.check(cfg, client, Node.objects.filter(is_active=True).count())
        if action == "train":
            form = forms_["train"] = forms.TrainForm(request.POST)
            if form.is_valid():
                d = form.cleaned_data
                _queue(request, Job.Type.TRAIN, form, {"start": d["start"].isoformat(), "end": d["end"].isoformat(), "label": d["label"]})
                return redirect("controls")
        elif action == "learn_now":
            _queue(request, Job.Type.TRAIN, None, {"auto": True},
                   "Learning started. The worker will look at the latest data and update the model within a minute.")
            return redirect("controls")
        elif action in ("replay", "evaluate"):
            prefix = "replay" if action == "replay" else "eval"
            form = forms_[action] = forms.ModelPeriodForm(request.POST, prefix=prefix)
            if form.is_valid():
                d = form.cleaned_data
                _queue(request, Job.Type.REPLAY if action == "replay" else Job.Type.EVALUATE, form,
                       {"model_id": d["model"].pk, "start": d["start"].isoformat(), "end": d["end"].isoformat()})
                return redirect("controls")
        elif action == "excluded":
            form = forms_["excluded"] = forms.ExcludedPeriodForm(request.POST)
            if form.is_valid():
                period = form.save(commit=False)
                period.created_by = request.user
                period.save()
                messages.success(request, "Excluded period saved. It is left out the next time a model is trained.")
                return redirect("controls")
        elif action == "chassis":
            form = forms_["chassis"] = forms.ChassisForm(request.POST)
            if form.is_valid():
                layout = form.cleaned_data["layout"]
                placed = chassis_service.apply(layout)
                messages.success(request, f"Chassis saved: {placed} node{'s' if placed != 1 else ''} placed in {len(layout)} chassis."
                                 if layout else "Chassis layout cleared. The Overview shows one list of nodes.")
                return redirect("controls")
        elif action == "upload":
            form = forms_["upload"] = forms.ExperimentUploadForm(request.POST, request.FILES)
            if form.is_valid():
                try:
                    text = form.cleaned_data["file"].read().decode("utf-8-sig")
                    n = import_experiments(text, request.user)
                    messages.success(request, f"Imported {n} logged anomalies.")
                    return redirect("controls")
                except UnicodeDecodeError:
                    messages.error(request, "The file must be a UTF-8 text (CSV) file.")
                except JobError as exc:
                    messages.error(request, str(exc))
        elif action not in ("settings", "test_connection", "readiness", "learn_now"):
            raise Http404
    return render(request, "controls.html", {
        "readiness": readiness,
        **{f"{k}_form": v for k, v in forms_.items()},
        "jobs": Job.objects.order_by("-requested_at")[:10],
        "autorefresh": request.method == "GET" and Job.objects.filter(status__in=["PENDING", "RUNNING"]).exists(),
        "learning": _learning(cfg),
        "active_tab": {"settings": "source", "test_connection": "source", "readiness": "source", "chassis": "source", "learn_now": "models",
                       "train": "advanced", "replay": "advanced", "evaluate": "advanced", "upload": "advanced",
                       "excluded": "advanced"}.get(request.POST.get("action"), "source"),
        "models": ModelVersion.objects.order_by("-trained_at")[:10],
        "evaluations": EvaluationRun.objects.select_related("model").order_by("-pk")[:10],
        "periods": ExcludedPeriod.objects.order_by("-start_at")[:10],
        "experiments": ExperimentEntry.objects.select_related("node").order_by("-start_at")[:10],
    })


def _learning(cfg):
    """What the platform has learned, and how far it has got, in terms an administrator can read."""
    state = autolearn.last_state()
    model = ModelVersion.objects.order_by("-trained_at").first()
    min_hours = float(cfg["detection"].get("learn_min_hours", 6))
    cov = ((model.baselines or {}).get("coverage") if model else None) or state.get("coverage") or {}
    nodes = Node.objects.filter(is_active=True).count()
    facts = []
    if model and cov:
        facts.append(f"Learned from {cov.get('hours_per_node', 0):g} hours of clean data per node on {cov.get('nodes', 0)} nodes.")
        idle, busy = cov.get("nodes_own_idle", 0), cov.get("nodes_own_loaded", 0)
        total = cov.get("nodes", nodes)
        facts.append(f"{idle} of {total} nodes have their own idle baseline; {busy} of {total} have their own busy baseline. "
                     f"The rest are compared with their group until they have enough data of their own.")
        if cov.get("trimmed_rows"):
            pct = 100 * cov["trimmed_rows"] / max(1, cov["rows"] + cov["trimmed_rows"])
            facts.append(f"{cov['trimmed_rows']} unusual windows ({pct:.1f}%) were left out of the learning automatically, "
                         f"so faults and stress tests do not become 'normal'.")
        if not cov.get("rows_loaded"):
            facts.append("No busy periods have been seen yet, so busy nodes are not scored until they have been.")
    progress = None
    if model is None and cov.get("hours_per_node") is not None:
        progress = min(100, round(100 * cov["hours_per_node"] / min_hours))
    return {"model": model, "state": state, "facts": facts, "progress": progress, "min_hours": min_hours,
            "coverage": cov, "origin": ((model.baselines or {}).get("origin") if model else None)}


@administrator_required
@require_POST
def excluded_delete(request, pk):
    get_object_or_404(ExcludedPeriod, pk=pk).delete()
    messages.success(request, "Excluded period removed.")
    return redirect("controls")


def _file_config():
    return load_config(django_settings.HPCSENTINEL_CONFIG)


# ---- users (Administrator only, FR-03) -------------------------------------------------------
@administrator_required
def users(request):
    form = forms.UserCreateForm()
    if request.method == "POST":
        form = forms.UserCreateForm(request.POST)
        if form.is_valid():
            d = form.cleaned_data
            User.objects.create_user(username=d["username"], password=d["password"], role=d["role"])
            messages.success(request, f"User {d['username']} created.")
            return redirect("users")
    return render(request, "users.html", {"users": User.objects.exclude(username="system").order_by("username"), "form": form})


@administrator_required
@require_POST
def user_update(request, pk):
    target = get_object_or_404(User.objects.exclude(username="system"), pk=pk)
    action = request.POST.get("action")
    admins_left = User.objects.filter(role=User.Role.ADMINISTRATOR, is_active=True).exclude(pk=target.pk).count()
    if action == "deactivate":
        if target == request.user or (target.is_administrator and admins_left == 0):
            messages.error(request, "You cannot deactivate yourself or the last active Administrator.")
        else:
            target.is_active = False
            target.save(update_fields=["is_active"])
            messages.success(request, f"{target.username} deactivated.")
    elif action == "activate":
        target.is_active = True
        target.save(update_fields=["is_active"])
        messages.success(request, f"{target.username} activated.")
    elif action in ("OPERATOR", "ADMINISTRATOR"):
        if action == "OPERATOR" and target.is_administrator and admins_left == 0:
            messages.error(request, "There must be at least one active Administrator.")
        elif target.is_superuser and action == "OPERATOR":
            messages.error(request, "A superuser is always an Administrator.")
        else:
            target.role = action
            target.save(update_fields=["role"])
            messages.success(request, f"{target.username} is now {target.get_role_display()}.")
    else:
        raise Http404
    return redirect("users")
