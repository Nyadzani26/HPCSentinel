"""Keeping the configuration in the database as versions (BR-10)."""
from django.db import transaction
from django.db.models import Max

from core.models import ConfigVersion, User


def get_system_user() -> User:
    """The account that owns automatic records. It is inactive, so nobody can sign in as it."""
    user, created = User.objects.get_or_create(
        username="system",
        defaults={"role": User.Role.ADMINISTRATOR, "is_active": False, "first_name": "System"},
    )
    if created:
        user.set_unusable_password()
        user.save(update_fields=["password"])
    return user


def ensure_config_version(cfg: dict, user: User | None = None) -> ConfigVersion:
    """Return the active ConfigVersion, creating a new one if the content has changed."""
    user = user or get_system_user()
    with transaction.atomic():
        active = ConfigVersion.objects.filter(is_active=True).first()
        if active is not None and active.content == cfg:
            return active
        ConfigVersion.objects.filter(is_active=True).update(is_active=False)
        latest = ConfigVersion.objects.aggregate(m=Max("version_no"))["m"] or 0
        return ConfigVersion.objects.create(version_no=latest + 1, content=cfg, is_active=True, created_by=user)
