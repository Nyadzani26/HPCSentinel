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
from django.http import Http404, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from core import forms
from core.config import ConfigError, load_config
from core.models import (Alert, AlertAction, CycleRun, EvaluationRun, ExcludedPeriod, ExperimentEntry, InvalidTransition,
                         Job, ModelVersion, Node, NodeWindow, User)
from core.pipeline.jobs import JobError, import_experiments
from core.services import readiness as readiness_service
from core.services import settings_store
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


def node_state(node, window, live_alert):
    """Colour and label of a node tile (S-02): normal, unusual, under review or unreliable."""
    if live_alert is not None:
        if live_alert.status == Alert.Status.ACKNOWLEDGED:
            return "review", "Under review"
        return "unusual", "Unusual"
    if window is None or window.quality != NodeWindow.Quality.RELIABLE:
        return "unknown", "No baseline" if window is not None and window.quality == "NOT_LEARNED" else "Unreliable"
    return "normal", "Normal"


@login_required
def home(request):
    """S-02 fleet overview."""
    last_cycle = CycleRun.objects.filter(mode=CycleRun.Mode.LIVE).order_by("-started_at").first()
    live_alerts = {a.node_id: a for a in Alert.objects.filter(mode="LIVE", status__in=["OPEN", "ACKNOWLEDGED"])}
    latest = {}
    for w in NodeWindow.objects.filter(cycle__mode="LIVE", window_end__gte=timezone.now() - timedelta(hours=1)) \
            .order_by("window_end"):
        latest[w.node_id] = w
    tiles = []
    for node in Node.objects.filter(is_active=True).order_by("name"):
        window = latest.get(node.pk)
        css, label = node_state(node, window, live_alerts.get(node.pk))
        tiles.append({"node": node, "css": css, "label": label, "window": window, "alert": live_alerts.get(node.pk)})
    counts = {k: sum(1 for t in tiles if t["css"] == k) for k in ("normal", "unusual", "review", "unknown")}
    source_ok = bool(last_cycle and last_cycle.source_reachable and timezone.now() - last_cycle.started_at < STALE_AFTER)
    return render(request, "fleet.html", {
        "tiles": tiles, "counts": counts, "last_cycle": last_cycle, "source_ok": source_ok,
        "model": ModelVersion.objects.order_by("-trained_at").first(),
    })


@login_required
def node_detail(request, name):
    """S-03 node inspection: load state, recent scores and alerts of one node."""
    node = get_object_or_404(Node, name=name)
    windows = list(NodeWindow.objects.filter(node=node, cycle__mode="LIVE").order_by("-window_end")[:360])
    windows.reverse()
    latest = windows[-1] if windows else None
    chart = [{"t": w.window_end.isoformat(), "v": w.combined_score} for w in windows if w.combined_score is not None]
    threshold = None
    cycle = CycleRun.objects.filter(mode="LIVE").order_by("-started_at").select_related("config").first()
    if cycle is not None:
        threshold = cycle.config.content.get("detection", {}).get("alert_threshold")
    features = {}
    if latest is not None:
        features = {k: v for k, v in sorted(latest.features.items()) if k.endswith("__mean_15")}
    series = {}
    for w in windows:  # one chart per metric, from the stored 5-minute means (FR-21)
        for key, value in w.features.items():
            if key.endswith("__mean_5"):
                series.setdefault(key[:-len("__mean_5")], []).append({"t": w.window_end.isoformat(), "v": value})
    alerts = Alert.objects.filter(node=node, mode="LIVE").order_by("-opened_at")
    markers = [a.opened_at.isoformat() for a in alerts if windows and a.opened_at >= windows[0].window_end]
    return render(request, "node_detail.html", {
        "node": node, "latest": latest, "chart": chart, "threshold": threshold, "features": features,
        "alerts": alerts[:20], "series": series, "markers": markers,
    })


@login_required
def alert_list(request):
    """S-04 alerts, newest first, filtered by status, node and time range (FR-22)."""
    status = request.GET.get("status", "live")
    qs = Alert.objects.filter(mode="LIVE").select_related("node")
    if status == "live":
        qs = qs.filter(status__in=["OPEN", "ACKNOWLEDGED"])
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
    filters = [("live", "Needs attention"), ("OPEN", "Open"), ("ACKNOWLEDGED", "Acknowledged"), ("CLOSED", "Closed"), ("all", "All")]
    return render(request, "alert_list.html", {
        "alerts": qs.order_by("-opened_at")[:200], "status": status, "filters": filters, "node": node,
        "date_from": request.GET.get("from", ""), "date_to": request.GET.get("to", ""), "errors": errors,
    })


@login_required
def alert_detail(request, pk):
    """S-05 one alert: explanation, evidence and the triage actions."""
    alert = get_object_or_404(Alert.objects.select_related("node", "model"), pk=pk)
    return render(request, "alert_detail.html", {
        "alert": alert, "actions": alert.actions.select_related("user").order_by("created_at"),
        "windows": alert.windows.order_by("window_end")[:50],
        "close_form": forms.CloseForm(), "note_form": forms.NoteForm(),
    })


def _review(request, pk, operation):
    alert = get_object_or_404(Alert, pk=pk)
    service = ReviewService()
    try:
        operation(service, alert)
    except PermissionDenied as exc:
        messages.error(request, str(exc))
    except InvalidTransition as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "Done.")
    return redirect("alert_detail", pk=pk)


@login_required
@require_POST
def alert_acknowledge(request, pk):
    return _review(request, pk, lambda s, a: s.acknowledge(a, request.user, request.POST.get("note", "")))


@login_required
@require_POST
def alert_close(request, pk):
    form = forms.CloseForm(request.POST)
    if not form.is_valid():
        messages.error(request, " ".join(e for errs in form.errors.values() for e in errs))
        return redirect("alert_detail", pk=pk)
    return _review(request, pk, lambda s, a: s.close(a, request.user, form.cleaned_data["outcome"], form.cleaned_data["note"]))


@login_required
@require_POST
def alert_reopen(request, pk):
    return _review(request, pk, lambda s, a: s.reopen(a, request.user, request.POST.get("note", "")))


@login_required
@require_POST
def alert_note(request, pk):
    form = forms.NoteForm(request.POST)
    if not form.is_valid():
        messages.error(request, " ".join(e for errs in form.errors.values() for e in errs))
        return redirect("alert_detail", pk=pk)
    return _review(request, pk, lambda s, a: s.add_note(a, request.user, form.cleaned_data["note"]))


# ---- controls (Administrator only) ---------------------------------------------------------
def _queue(request, job_type, form, params):
    job = Job.objects.create(type=job_type, parameters=params, requested_by=request.user)
    messages.success(request, f"{job.get_type_display()} requested (job {job.pk}). The worker will start it shortly.")


@administrator_required
def controls(request):
    """Model and experiment controls: train, replay, evaluate, excluded periods, experiment log (FR-16, FR-28 to FR-30)."""
    forms_ = {"train": forms.TrainForm(), "replay": forms.ModelPeriodForm(prefix="replay"),
              "evaluate": forms.ModelPeriodForm(prefix="eval"), "excluded": forms.ExcludedPeriodForm(),
              "upload": forms.ExperimentUploadForm()}
    try:
        base_cfg = _file_config()
    except ConfigError as exc:
        messages.error(request, f"The configuration file cannot be used: {exc}")
        return redirect("home")
    cfg = settings_store.effective_config(base_cfg)
    forms_["settings"] = forms.SettingsForm(initial={
        "prometheus_url": cfg["prometheus"]["url"], "alert_threshold": cfg["detection"]["alert_threshold"],
        "persistence_windows": cfg["detection"]["persistence_windows"]})
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
        elif action not in ("settings", "test_connection", "readiness"):
            raise Http404
    return render(request, "controls.html", {
        "readiness": readiness,
        **{f"{k}_form": v for k, v in forms_.items()},
        "jobs": Job.objects.order_by("-requested_at")[:10],
        "models": ModelVersion.objects.order_by("-trained_at")[:10],
        "evaluations": EvaluationRun.objects.select_related("model").order_by("-pk")[:10],
        "periods": ExcludedPeriod.objects.order_by("-start_at")[:10],
        "experiments": ExperimentEntry.objects.select_related("node").order_by("-start_at")[:10],
    })


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
