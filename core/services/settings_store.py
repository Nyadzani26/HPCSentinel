"""Settings changed in the web interface, layered over the configuration file (FR-04, FR-18)."""
import copy

from django.conf import settings as django_settings
from django.db import transaction

from core.models import PlatformSetting
from core.services.prometheus import PrometheusClient, PrometheusError


class SettingError(Exception):
    """A setting value is not acceptable; the message says why."""


def _stored() -> dict:
    return {s.key: s.value for s in PlatformSetting.objects.all()}


def effective_config(file_cfg: dict) -> dict:
    """The configuration in force: the file, overlaid by what an Administrator has set."""
    cfg = copy.deepcopy(file_cfg)
    stored = _stored()
    if "prometheus_url" in stored:
        cfg["prometheus"]["url"] = stored["prometheus_url"]
    elif django_settings.PROMETHEUS_URL:
        cfg["prometheus"]["url"] = django_settings.PROMETHEUS_URL
    if "alert_threshold" in stored:
        cfg["detection"]["alert_threshold"] = stored["alert_threshold"]
    if "persistence_windows" in stored:
        cfg["detection"]["persistence_windows"] = stored["persistence_windows"]
    return cfg


def make_client(cfg: dict) -> PrometheusClient:
    return PrometheusClient(cfg["prometheus"].get("url", ""), timeout=cfg["prometheus"]["timeout_seconds"])


def validate(url=None, threshold=None, persistence=None) -> dict:
    out = {}
    if url is not None:
        try:
            PrometheusClient(url)
        except PrometheusError as exc:
            raise SettingError(str(exc)) from exc
        out["prometheus_url"] = url.strip()
    if threshold is not None:
        if not isinstance(threshold, (int, float)) or threshold <= 0 or threshold > 100:
            raise SettingError("The alert threshold must be a number greater than 0 and at most 100.")
        out["alert_threshold"] = float(threshold)
    if persistence is not None:
        if not isinstance(persistence, int) or not 1 <= persistence <= 20:
            raise SettingError("The number of consecutive windows must be a whole number from 1 to 20.")
        out["persistence_windows"] = persistence
    return out


def save(user, **values) -> dict:
    """Validate and store settings; all or nothing."""
    clean = validate(**values)
    with transaction.atomic():
        for key, value in clean.items():
            PlatformSetting.objects.update_or_create(key=key, defaults={"value": value, "updated_by": user})
    return clean
