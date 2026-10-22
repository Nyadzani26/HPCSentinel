"""The time zone people see (settings.TIME_ZONE; South African time on Ranger).

Everything is stored and calculated in UTC. These helpers are for the places where Python itself writes a time for a
person, such as a report line or a CSV file. In templates the `date` filter already converts, so they only need
the label from `zone_label()`.
"""
from django.utils import timezone


def zone_name() -> str:
    """The IANA name, for example "Africa/Johannesburg". The browser needs it to draw the charts in the same zone."""
    return timezone.get_current_timezone_name()


def zone_label(at=None) -> str:
    """The short name shown next to a time, for example "SAST"."""
    return timezone.localtime(at or timezone.now()).tzname()


def local(at):
    return timezone.localtime(at)


def stamp(at, fmt="%Y-%m-%d %H:%M") -> str:
    return timezone.localtime(at).strftime(fmt)
