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
    return display.headline(alert.explanation, alert.node.name)


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
