"""Daily, weekly and monthly reports (stage 3).

A report answers the questions an administrator, or a head of department who never opens the platform, asks of a
period: how many alerts were there, were they real, which nodes were involved, was the platform able to see the
cluster, and did it learn anything new. Everything is computed from what the platform has already stored, so
generating a report never touches Prometheus.

Periods are whole days, Monday-to-Monday weeks and calendar months in the time zone the interface shows
(settings.TIME_ZONE, South African time on Ranger), so "Wednesday" runs from midnight to midnight as the administrator
lives it. `generate_due` creates every completed period that has data and no report yet, so the worker can call it
every hour without producing duplicates.
"""
import csv
import io
import logging
import statistics
from datetime import timedelta

from django.db.models import Count
from django.db.models.functions import TruncHour
from django.utils import timezone

from core import display
from core.localtime import local, stamp, zone_label
from core.models import Alert, ClusterEvent, CycleRun, ModelVersion, NodeWindow, Report

log = logging.getLogger(__name__)

CYCLE_MINUTES = 2          # one window per node per cycle; used only to turn window counts into node-hours
TOP = 10                   # rows in the per-node and per-metric tables


# ---- periods -------------------------------------------------------------------------------------
def floor_period(kind: str, moment):
    """Start (local midnight) of the day, Monday-based week or calendar month that contains `moment`."""
    moment = local(moment)
    day = moment.replace(hour=0, minute=0, second=0, microsecond=0)
    if kind == Report.Kind.DAILY:
        return day
    if kind == Report.Kind.WEEKLY:
        return day - timedelta(days=day.weekday())
    return day.replace(day=1)


def next_period(kind: str, start):
    if kind == Report.Kind.DAILY:
        return start + timedelta(days=1)
    if kind == Report.Kind.WEEKLY:
        return start + timedelta(days=7)
    return (start.replace(day=28) + timedelta(days=4)).replace(day=1)


def previous_period(kind: str, start):
    return floor_period(kind, start - timedelta(seconds=1))


def label_for(kind: str, start) -> str:
    if kind == Report.Kind.DAILY:
        return f"{start:%A} {start.day} {start:%B %Y}"
    if kind == Report.Kind.WEEKLY:
        last = start + timedelta(days=6)
        return f"Week of {start.day} {start:%B} to {last.day} {last:%B %Y}"
    return f"{start:%B %Y}"


# ---- building ------------------------------------------------------------------------------------
def _minutes(delta) -> float:
    return round(delta.total_seconds() / 60, 1)


def _lasted(alert, now):
    """How long the alert was unusual, measured to `now` while it is still active (so a report is not tied to the wall clock)."""
    end = now if alert.is_active else (alert.last_seen_at or alert.opened_at)
    return max(end - alert.opened_at, timedelta(0))


def build(kind: str, start, end, now=None) -> dict:
    """Compute one report. `end` may be in the future for a "so far" report; it is cut off at `now`."""
    now = now or timezone.now()
    start = local(start)                       # a report read back from the database arrives in UTC; its days are local
    until = min(end, now)
    partial = end > now
    alerts = list(Alert.objects.filter(mode=Alert.Mode.LIVE, opened_at__gte=start, opened_at__lt=end).select_related("node"))
    previous = previous_period(kind, start)
    prev_alerts = Alert.objects.filter(mode=Alert.Mode.LIVE, opened_at__gte=previous, opened_at__lt=start).count()

    windows = NodeWindow.objects.filter(cycle__mode="LIVE", window_end__gte=start, window_end__lt=end)
    by_quality = {r["quality"]: r["n"] for r in windows.values("quality").annotate(n=Count("pk"))}
    reliable = by_quality.get("RELIABLE", 0)
    seen = reliable + by_quality.get("NOT_LEARNED", 0) + by_quality.get("STARTING", 0)     # data arrived, even if held back
    total = seen + by_quality.get("UNRELIABLE", 0)
    hours_reporting = (windows.filter(quality__in=["RELIABLE", "NOT_LEARNED", "STARTING"]).annotate(h=TruncHour("window_end"))
                       .values("h").distinct().count())
    nodes_seen = windows.filter(quality__in=["RELIABLE", "NOT_LEARNED", "STARTING"]).values("node").distinct().count()
    outages = CycleRun.objects.filter(mode="LIVE", started_at__gte=start, started_at__lt=end, source_reachable=False).count()

    reviewed = [a for a in alerts if a.status == Alert.Status.CLOSED]
    confirmed = sum(a.outcome == Alert.Outcome.CONFIRMED for a in reviewed)
    false_alarm = sum(a.outcome == Alert.Outcome.FALSE_ALARM for a in reviewed)
    durations = [_minutes(_lasted(a, until)) for a in alerts]
    stats = {
        "opened": len(alerts),
        "still_active": sum(a.is_active for a in alerts),
        "recovered_unreviewed": sum(a.status == Alert.Status.RESOLVED for a in alerts),
        "reviewed": len(reviewed), "confirmed": confirmed, "false_alarm": false_alarm,
        "precision_reviewed": round(confirmed / len(reviewed), 3) if reviewed else None,
        "median_minutes": round(statistics.median(durations), 1) if durations else None,
        "longest_minutes": max(durations) if durations else None,
        "came_back": sum(a.recurrences > 0 for a in alerts),
        "stopped_reporting": sum(a.resolved_reason == "no_data" for a in alerts),
    }

    per_node: dict[str, dict] = {}
    per_metric: dict[str, int] = {}
    per_state: dict[str, int] = {}
    for a in alerts:
        row = per_node.setdefault(a.node.name, {"node": a.node.name, "alerts": 0, "minutes": 0.0, "confirmed": 0,
                                                "false_alarm": 0, "unreviewed": 0})
        row["alerts"] += 1
        row["minutes"] = round(row["minutes"] + _minutes(_lasted(a, until)), 1)
        if a.outcome == Alert.Outcome.CONFIRMED:
            row["confirmed"] += 1
        elif a.outcome == Alert.Outcome.FALSE_ALARM:
            row["false_alarm"] += 1
        else:
            row["unreviewed"] += 1
        ex = a.explanation or {}
        first = (ex.get("contributors") or [{}])[0].get("metric")
        if first:
            per_metric[first] = per_metric.get(first, 0) + 1
        state = (ex.get("load_state") or "").upper()
        if state:
            per_state[state] = per_state.get(state, 0) + 1
    nodes = sorted(per_node.values(), key=lambda r: (-r["alerts"], -r["minutes"], r["node"]))[:TOP]
    metrics = [{"metric": m, "label": display.label(m), "alerts": n}
               for m, n in sorted(per_metric.items(), key=lambda kv: (-kv[1], kv[0]))[:TOP]]

    learned = [{"label": m.label, "trained_at": m.trained_at.isoformat(), "trained_label": stamp(m.trained_at), "origin": (m.baselines or {}).get("origin", "manual"),
                "hours_per_node": ((m.baselines or {}).get("coverage") or {}).get("hours_per_node"),
                "nodes": (m.baselines or {}).get("nodes")}
               for m in ModelVersion.objects.filter(trained_at__gte=start, trained_at__lt=end).order_by("trained_at")]
    in_use = ModelVersion.objects.filter(trained_at__lt=end).order_by("-trained_at").first()

    events = [{"pk": e.pk, "started": stamp(e.started_at), "minutes": _minutes(e.lasted), "active": e.is_active,
               "peak_nodes": e.peak_nodes, "peak_reliable": e.peak_reliable,
               "cause": display.label(max(e.causes, key=e.causes.get)) if e.causes else ""}
              for e in ClusterEvent.objects.filter(started_at__gte=start, started_at__lt=end).order_by("started_at")]

    data = {
        "kind": kind, "label": label_for(kind, start), "partial": partial,
        "start": start.isoformat(), "end": end.isoformat(), "until": until.isoformat(), "until_label": stamp(until),
        "coverage": {"nodes_seen": nodes_seen, "hours_reporting": hours_reporting,
                     "hours_in_period": round((until - start).total_seconds() / 3600, 1),
                     "node_hours": round(reliable * CYCLE_MINUTES / 60, 1), "windows_total": total,
                     "reliable_share": round(reliable / total, 3) if total else None, "outage_cycles": outages},
        "alerts": stats, "previous_opened": prev_alerts,
        "by_node": nodes, "by_metric": metrics, "by_state": per_state,
        "learning": {"models": learned, "in_use": in_use.label if in_use else None},
        "events": events,
    }
    data["summary"] = summarise(data)
    return data


def summarise(d: dict) -> list[str]:
    """The report in a few plain sentences, for the person who reads only the top."""
    c, a = d["coverage"], d["alerts"]
    period = "so far" if d["partial"] else "in this period"
    lines = []
    if not c["nodes_seen"]:
        lines.append(f"The platform did not receive usable data from any node {period}, so there is nothing to report on the nodes. "
                     "If the cluster was switched off this is expected.")
    else:
        lines.append(f"HPCSentinel watched {c['nodes_seen']} node{'s' if c['nodes_seen'] != 1 else ''} {period}, with data arriving in "
                     f"{c['hours_reporting']} of {c['hours_in_period']:g} hours, and scored {c['node_hours']:g} node-hours of behaviour.")
    if a["opened"] == 0:
        lines.append("No alerts were raised.")
    else:
        before = d["previous_opened"]
        trend = ("the same as in the previous period" if a["opened"] == before
                 else f"{'up' if a['opened'] > before else 'down'} from {before} in the previous period")
        lines.append(f"{a['opened']} alert{'s were' if a['opened'] != 1 else ' was'} raised, {trend}."
                     + (f" {a['still_active']} {'is' if a['still_active'] == 1 else 'are'} still active." if a["still_active"] else ""))
        if a["reviewed"]:
            lines.append(f"{a['reviewed']} {'has' if a['reviewed'] == 1 else 'have'} been reviewed by a person: {a['confirmed']} real fault"
                         f"{'s' if a['confirmed'] != 1 else ''} and {a['false_alarm']} false alarm{'s' if a['false_alarm'] != 1 else ''}"
                         f" ({round(100 * a['precision_reviewed'])}% real among those reviewed). ")
        waiting = a["opened"] - a["reviewed"] - a["still_active"]
        if waiting > 0:
            lines.append(f"{waiting} recovered alert{'s are' if waiting != 1 else ' is'} still waiting for a quick review, "
                         "so the real-fault rate above may change.")
        if d["by_node"]:
            top = d["by_node"][0]
            lines.append(f"The node with the most alerts was {top['node']} ({top['alerts']}, unusual for {top['minutes']:g} minutes in total).")
        if d["by_metric"]:
            lines.append(f"The most common main cause was {d['by_metric'][0]['label'].lower()}.")
    if d["events"]:
        biggest = max(d["events"], key=lambda e: e["peak_nodes"])
        lines.append(f"{len(d['events'])} cluster-wide event{'s' if len(d['events']) != 1 else ''} happened, where many nodes changed at once "
                     f"(the largest involved {biggest['peak_nodes']} of {biggest['peak_reliable']} nodes). They are counted as events, not as individual alerts.")
    new = d["learning"]["models"]
    if new:
        lines.append(f"The platform learned {len(new)} new model{'s' if len(new) != 1 else ''} {period} "
                     f"({', '.join(m['label'] for m in new[:3])}{'…' if len(new) > 3 else ''}).")
    if c["outage_cycles"]:
        lines.append(f"Prometheus could not be reached for {c['outage_cycles']} check{'s' if c['outage_cycles'] != 1 else ''}; no alerts can be raised during an outage.")
    return [line.strip() for line in lines]


# ---- storing -------------------------------------------------------------------------------------
def generate(kind: str, start, now=None, replace=False) -> Report:
    """Build and save the report for the period beginning at `start` (a completed period)."""
    now = now or timezone.now()
    start = local(start)
    end = next_period(kind, start)
    data = build(kind, start, end, now)
    report, created = Report.objects.get_or_create(kind=kind, period_start=start,
                                                   defaults={"period_end": end, "generated_at": now, "data": data})
    if not created and replace:
        report.period_end, report.generated_at, report.data = end, now, data
        report.save(update_fields=["period_end", "generated_at", "data"])
    return report


def drop_misaligned() -> int:
    """Delete saved reports whose periods do not start at local midnight, and say how many went.

    Reports written while the platform counted days in UTC would overlap the local days that replace them (two
    "Wednesday" reports with different numbers). They are only summaries of stored readings, so they are removed and
    `generate_due` writes them again with the right boundaries. A report that already fits is never touched.
    """
    stale = [r.pk for r in Report.objects.all() if local(r.period_start) != floor_period(r.kind, r.period_start)]
    if stale:
        Report.objects.filter(pk__in=stale).delete()
        log.info("Removed %d saved report(s) written for a different time zone; they will be written again.", len(stale))
    return len(stale)


def generate_due(now=None, back=None) -> list[Report]:
    """Create every completed period that has data and no report yet. Safe to call as often as you like."""
    now = now or timezone.now()
    drop_misaligned()
    back = back or {Report.Kind.DAILY: 14, Report.Kind.WEEKLY: 8, Report.Kind.MONTHLY: 6}
    first = CycleRun.objects.filter(mode="LIVE").order_by("started_at").values_list("started_at", flat=True).first()
    made = []
    if first is None:
        return made
    for kind, count in back.items():
        start = previous_period(kind, floor_period(kind, now))
        for _ in range(count):
            end = next_period(kind, start)
            if end <= first:
                break
            if end <= now and not Report.objects.filter(kind=kind, period_start=start).exists() \
                    and CycleRun.objects.filter(mode="LIVE", started_at__gte=start, started_at__lt=end).exists():
                made.append(generate(kind, start, now))
            start = previous_period(kind, start)
    return made


# ---- download ------------------------------------------------------------------------------------
def _safe(value):
    """Stop a spreadsheet treating text that starts with = + - @ as a formula."""
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@") else text


def alerts_csv(start, end) -> str:
    out = io.StringIO()
    w = csv.writer(out)
    zone = zone_label().lower()                # the times are in the zone shown on screen, and the headings say which
    w.writerow(["alert", "node", f"opened_{zone}", f"last_unusual_{zone}", f"ended_{zone}", "lasted_minutes", "state", "ended_because",
                "came_back", "peak_score", "main_cause", "reviewed_as"])
    qs = Alert.objects.filter(mode=Alert.Mode.LIVE, opened_at__gte=start, opened_at__lt=end).select_related("node").order_by("opened_at")
    for a in qs:
        first = ((a.explanation or {}).get("contributors") or [{}])[0]
        top = first.get("metric", "")
        w.writerow([a.pk, _safe(a.node.name), stamp(a.opened_at),
                    stamp(a.last_seen_at) if a.last_seen_at else "",
                    stamp(a.recovered_at) if a.recovered_at else "",
                    _minutes(a.lasted), a.get_status_display(), a.resolved_reason, a.recurrences, round(a.peak_score, 2),
                    _safe(display.contributor_label(first)) if top else "", a.get_outcome_display() if a.outcome else ""])
    return out.getvalue()


def nodes_csv(data: dict) -> str:
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["node", "alerts", "unusual_minutes", "real_faults", "false_alarms", "not_yet_reviewed"])
    for r in data["by_node"]:
        w.writerow([_safe(r["node"]), r["alerts"], r["minutes"], r["confirmed"], r["false_alarm"], r["unreviewed"]])
    return out.getvalue()
