"""Email alerts (FR-31).

The platform emails the people an administrator names when a live alert opens, and (if asked) when many nodes become
unusual at once. This is a separate step that runs after detection. It only reads alerts that already exist, so it
cannot change a score, an alert or an evaluation result, and a mail server that is down or slow cannot stop detection.

What is sent, and what is deliberately not:

* One email per alert, once. Alerts that open in the same minute go in one email. An alert that ends, comes back or is
  acknowledged does not send another.
* One email per cluster-wide event, not one per node. Alerts held back during an event send nothing.
* Nothing for an alert that opened before the person turned this on, so switching it on never sends a backlog.
* Nothing for an alert that is no longer new (older than an hour), already being looked at, already ended, or from a replay.
* At most MAX_PER_HOUR emails an hour. What waits is sent when the hour allows, as long as it is still news.
* After a failed send the platform waits RETRY_AFTER before trying again, and records every attempt.

Recipients, the on/off switch and the event option are set on the Settings page. The mail server and the password are in
the server's .env file (see .env.example), never in the database or the repository.
"""
import logging
import re
from datetime import datetime, timedelta

from django.conf import settings as django_settings
from django.core.exceptions import ValidationError
from django.core.mail import EmailMessage
from django.core.validators import validate_email
from django.urls import reverse
from django.utils import timezone

from core import display
from core.models import Alert, ClusterEvent, Notification, PlatformSetting

log = logging.getLogger(__name__)

MAX_RECIPIENTS = 10
MAX_AGE = timedelta(hours=1)            # an alert older than this is no longer news
MAX_PER_HOUR = 6
RETRY_AFTER = timedelta(minutes=5)

ENABLED, RECIPIENTS, EVENTS, SINCE = "notify_enabled", "notify_recipients", "notify_events", "notify_since"


class NotifyError(ValueError):
    """A notification setting is not acceptable; the message says why."""


# --- settings -------------------------------------------------------------------------------------------------

def parse_recipients(text) -> list[str]:
    """Addresses separated by commas, spaces, semicolons or new lines. Each is checked; duplicates are dropped."""
    found: list[str] = []
    for item in re.split(r"[,;\s]+", text or ""):
        item = item.strip()
        if not item:
            continue
        try:
            validate_email(item)
        except ValidationError:
            raise NotifyError(f"'{item}' is not an email address.") from None
        if item.lower() not in (f.lower() for f in found):
            found.append(item)
    if len(found) > MAX_RECIPIENTS:
        raise NotifyError(f"At most {MAX_RECIPIENTS} addresses can be used.")
    return found


def get_settings() -> dict:
    stored = {s.key: s.value for s in PlatformSetting.objects.filter(key__in=[ENABLED, RECIPIENTS, EVENTS, SINCE])}
    since = None
    if stored.get(SINCE):
        try:
            since = datetime.fromisoformat(stored[SINCE])
        except (TypeError, ValueError):
            since = None
    return {"enabled": bool(stored.get(ENABLED, False)), "recipients": list(stored.get(RECIPIENTS, [])),
            "events": bool(stored.get(EVENTS, True)), "since": since}


def save_settings(user, enabled: bool, recipients_text: str, events: bool, now=None) -> dict:
    """Validate and store. Turning it on starts the clock, so only alerts that open from then on are sent."""
    recipients = parse_recipients(recipients_text)
    if enabled and not recipients:
        raise NotifyError("Add at least one email address, or turn email alerts off.")
    now = now or timezone.now()
    before = get_settings()
    values = {ENABLED: bool(enabled), RECIPIENTS: recipients, EVENTS: bool(events)}
    if enabled and not before["enabled"]:
        values[SINCE] = now.isoformat()
    for key, value in values.items():
        PlatformSetting.objects.update_or_create(key=key, defaults={"value": value, "updated_by": user})
    return get_settings()


def server_status() -> dict:
    """Whether the server is set up to send (from .env). Never includes the password."""
    configured = bool(django_settings.EMAIL_HOST)
    return {"configured": configured, "host": django_settings.EMAIL_HOST, "port": django_settings.EMAIL_PORT,
            "sender": django_settings.DEFAULT_FROM_EMAIL if configured else ""}


# --- the message ----------------------------------------------------------------------------------------------

def _clean(text: str, limit: int = 150) -> str:
    """One line, no control characters: a node name can never add a header to the email."""
    return re.sub(r"[\x00-\x1f\x7f]+", " ", str(text)).strip()[:limit]


def _when(moment) -> str:
    local = timezone.localtime(moment)
    return f"{local.day} {local:%b %H:%M %Z}"


def _link(name: str, *args) -> str:
    return f"{django_settings.SITE_URL}{reverse(name, args=args)}" if django_settings.SITE_URL else ""


def _alert_headline(alert: Alert) -> str:
    ex = alert.explanation or {}
    return display.headline(ex.get("latest") or ex, alert.node.name)


def build(alerts: list[Alert], events: list[ClusterEvent]) -> tuple[str, str]:
    """Subject and plain-text body for the alerts and events that are new."""
    if len(alerts) == 1 and not events:
        subject = f"HPCSentinel alert: {_alert_headline(alerts[0])}"
    elif events and not alerts:
        e = events[0]
        subject = f"HPCSentinel: cluster-wide event, {e.peak_nodes} nodes unusual together" if len(events) == 1 \
            else f"HPCSentinel: {len(events)} cluster-wide events"
    else:
        parts = []
        if alerts:
            parts.append(f"{len(alerts)} new alert{'s' if len(alerts) != 1 else ''}")
        if events:
            parts.append(f"{len(events)} cluster-wide event{'s' if len(events) != 1 else ''}")
        subject = "HPCSentinel: " + " and ".join(parts)

    lines: list[str] = []
    if alerts:
        lines.append(f"New alert{'s' if len(alerts) != 1 else ''}:")
        for a in alerts:
            lines.append(f"- {_alert_headline(a)}")
            lines.append(f"  Unusual since {_when(a.opened_at)}. Score peaked at {a.peak_score:.1f}.")
            link = _link("alert_detail", a.pk)
            if link:
                lines.append(f"  {link}")
        lines.append("")
    if events:
        lines.append("Cluster-wide event" + ("s" if len(events) != 1 else "") + ":")
        for e in events:
            lines.append(f"- {e.peak_nodes} of {e.peak_reliable} nodes became unusual in the same check, starting {_when(e.started_at)}."
                         " No alert is raised for each node while it lasts.")
            top = sorted(e.causes.items(), key=lambda kv: (-kv[1], kv[0]))[:1]
            if top:
                lines.append(f"  Main cause: {display.label(top[0][0]).lower()}.")
            link = _link("event_detail", e.pk)
            if link:
                lines.append(f"  {link}")
        lines.append("")
    lines += ["You get this email because email alerts are turned on in HPCSentinel (Settings).",
              "Each alert and event is sent once. Nothing more is sent when it ends or comes back."]
    return _clean(subject, 150), "\n".join(lines)


# --- sending --------------------------------------------------------------------------------------------------

def _error_text(exc: Exception) -> str:
    text = _clean(f"{type(exc).__name__}: {exc}", 300)
    password = django_settings.EMAIL_HOST_PASSWORD
    return text.replace(password, "…") if password else text


def _send(subject: str, body: str, recipients: list[str]) -> None:
    EmailMessage(subject, body, from_email=django_settings.DEFAULT_FROM_EMAIL, to=recipients).send(fail_silently=False)


def _record(now, kind, subject, recipients, alerts, events, error="") -> Notification:
    return Notification.objects.create(
        created_at=now, kind=kind, subject=_clean(subject, 200), recipients=", ".join(recipients)[:600],
        alerts=alerts, events=events, ok=not error, error=error)


def send_test(recipients_text: str, now=None) -> tuple[bool, str]:
    """Send a test email to the addresses given. Returns (sent, what to tell the administrator)."""
    now = now or timezone.now()
    try:
        recipients = parse_recipients(recipients_text)
    except NotifyError as exc:
        return False, str(exc)
    if not recipients:
        return False, "Type at least one email address to send the test to."
    if not server_status()["configured"]:
        return False, "This server is not set up to send email yet. Add the EMAIL_ lines to the .env file (see .env.example) and restart."
    subject = "HPCSentinel test email"
    body = ("This is a test from HPCSentinel.\n\nIf you can read it, email alerts will reach you. "
            "Nothing is wrong with the cluster.\n")
    try:
        _send(subject, body, recipients)
    except Exception as exc:  # noqa: BLE001 - shown to the administrator, never raised
        error = _error_text(exc)
        _record(now, Notification.Kind.TEST, subject, recipients, 0, 0, error)
        return False, f"The email could not be sent: {error}"
    _record(now, Notification.Kind.TEST, subject, recipients, 0, 0)
    return True, f"Test email sent to {', '.join(recipients)}. Look in the junk folder if it is not in the inbox."


def sweep(now=None) -> Notification | None:
    """Send one email for everything new, if anything is. Returns the record of the attempt, or None if nothing was due."""
    now = now or timezone.now()
    cfg = get_settings()
    if not (cfg["enabled"] and cfg["recipients"] and server_status()["configured"]):
        return None
    since = cfg["since"] or now
    cutoff = max(since, now - MAX_AGE)

    alerts = list(Alert.objects.filter(mode=Alert.Mode.LIVE, status=Alert.Status.OPEN, notified_at__isnull=True,
                                       opened_at__gte=cutoff).select_related("node").order_by("opened_at"))
    events = list(ClusterEvent.objects.filter(ended_at__isnull=True, notified_at__isnull=True, started_at__gte=cutoff)
                  .order_by("started_at")) if cfg["events"] else []
    if not alerts and not events:
        return None

    recent = Notification.objects.filter(kind=Notification.Kind.ALERTS, created_at__gte=now - timedelta(hours=1))
    if recent.filter(ok=True).count() >= MAX_PER_HOUR:
        log.warning("Email limit of %s an hour reached; %s alert(s) and %s event(s) wait.", MAX_PER_HOUR, len(alerts), len(events))
        return None
    last_failure = recent.filter(ok=False).order_by("-created_at").first()
    if last_failure and now - last_failure.created_at < RETRY_AFTER:
        return None

    subject, body = build(alerts, events)
    try:
        _send(subject, body, cfg["recipients"])
    except Exception as exc:  # noqa: BLE001 - detection must never be affected by a mail problem
        error = _error_text(exc)
        log.warning("Email alert could not be sent: %s", error)
        return _record(now, Notification.Kind.ALERTS, subject, cfg["recipients"], len(alerts), len(events), error)
    Alert.objects.filter(pk__in=[a.pk for a in alerts]).update(notified_at=now)
    ClusterEvent.objects.filter(pk__in=[e.pk for e in events]).update(notified_at=now)
    log.info("Email sent to %s: %s", ", ".join(cfg["recipients"]), subject)
    return _record(now, Notification.Kind.ALERTS, subject, cfg["recipients"], len(alerts), len(events))
