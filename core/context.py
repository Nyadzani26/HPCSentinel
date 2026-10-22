"""Values every page needs: the alert badge, the data-source status, the colour of the page edge, the asset version
and the time zone the times are shown in."""
import hashlib
from datetime import timedelta
from functools import lru_cache
from pathlib import Path

from django.utils import timezone

from core.localtime import zone_label, zone_name
from core.models import Alert, ClusterEvent, CycleRun, ModelVersion

STALE_AFTER = timedelta(minutes=10)
ASSETS = Path(__file__).resolve().parent / "static" / "core"


@lru_cache(maxsize=1)
def asset_version():
    """Short fingerprint of the stylesheet and script.

    It goes on their URLs, so a new deploy always fetches the new files instead of a copy the browser (or a proxy in
    front of it) kept under the same name, and it is shown in the footer so a person can tell which version is running.
    """
    digest = hashlib.sha1()
    for name in ("sentinel.css", "watch.js"):
        try:
            digest.update((ASSETS / name).read_bytes())
        except OSError:
            pass
    return digest.hexdigest()[:7]


def sentinel(request):
    if not getattr(request, "user", None) or not request.user.is_authenticated:
        return {"asset_v": asset_version(), "tz_name": zone_name(), "tz_label": zone_label()}
    last = CycleRun.objects.filter(mode="LIVE").order_by("-started_at").first()
    if last is None:
        status = ("idle", "Waiting for the first cycle")
    elif not last.source_reachable:
        status = ("down", "Prometheus unreachable")
    elif timezone.now() - last.started_at > STALE_AFTER:
        status = ("down", "Worker not running")
    else:
        status = ("ok", "Monitoring active")
    return {
        "asset_v": asset_version(), "tz_name": zone_name(), "tz_label": zone_label(),
        "nav_alert_count": Alert.objects.filter(mode="LIVE", status__in=Alert.ACTIVE).count(),
        "nav_review_count": Alert.objects.filter(mode="LIVE", status=Alert.Status.RESOLVED).count(),
        "nav_status": status,
        "nav_event_active": ClusterEvent.objects.filter(ended_at__isnull=True).exists(),
        "nav_last_cycle": last.started_at if last else None,
        "nav_has_model": ModelVersion.objects.exists(),
    }
