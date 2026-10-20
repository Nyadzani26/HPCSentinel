"""Values every page needs: the alert badge and the data-source status in the sidebar."""
from datetime import timedelta

from django.utils import timezone

from core.models import Alert, CycleRun, ModelVersion

STALE_AFTER = timedelta(minutes=10)


def sentinel(request):
    if not getattr(request, "user", None) or not request.user.is_authenticated:
        return {}
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
        "nav_alert_count": Alert.objects.filter(mode="LIVE", status__in=Alert.ACTIVE).count(),
        "nav_review_count": Alert.objects.filter(mode="LIVE", status=Alert.Status.RESOLVED).count(),
        "nav_status": status,
        "nav_last_cycle": last.started_at if last else None,
        "nav_has_model": ModelVersion.objects.exists(),
    }
