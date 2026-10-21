from django import template
from django.utils import timezone
from django.utils.timesince import timesince

from core import display

register = template.Library()


@register.filter
def metric_label(name):
    return display.label(name)


@register.simple_tag
def metric_value(value, metric):
    return display.fmt(value, metric)


@register.simple_tag
def metric_change(observed, expected):
    return display.change(observed, expected)["text"]


@register.simple_tag
def metric_direction(observed, expected):
    return display.change(observed, expected)["direction"]


@register.simple_tag
def alert_headline(alert):
    """What is wrong, from the latest state of the alert when it has been updated since it opened."""
    ex = alert.explanation or {}
    return display.headline(ex.get("latest") or ex, alert.node.name)


@register.filter
def span(value):
    """'25 min', '2 h 10 min' or '3 days' for a length of time."""
    if value is None:
        return "–"
    minutes = int(value.total_seconds() // 60)
    if minutes < 1:
        return "under a minute"
    if minutes < 60:
        return f"{minutes} min"
    hours, rest = divmod(minutes, 60)
    if hours < 24:
        return f"{hours} h {rest} min" if rest else f"{hours} h"
    days = hours // 24
    return f"{days} day{'s' if days != 1 else ''}"


@register.filter
def ago(value):
    """'12 minutes ago' for a past time."""
    if not value:
        return "–"
    if value > timezone.now():
        return "just now"
    text = timesince(value, depth=1).replace("\xa0", " ")
    return "just now" if text.split()[0] == "0" else f"{text} ago"


@register.filter
def score_words(value):
    """Plain wording for an unusualness score."""
    if value is None:
        return "not scored"
    if value < 2:
        return "normal"
    if value < 3:
        return "slightly unusual"
    if value < 6:
        return "unusual"
    return "very unusual"


@register.filter
def still_going(events):
    """The cluster-wide events that have not ended yet."""
    return [e for e in events if e.is_active]


@register.filter
def ended(events):
    """The cluster-wide events that have ended."""
    return [e for e in events if not e.is_active]
