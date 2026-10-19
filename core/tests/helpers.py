"""Small builders shared by the tests."""
from datetime import timedelta

from django.utils import timezone

from core.models import Alert, ConfigVersion, ModelVersion, Node, User


def make_user(username="operator", role=User.Role.OPERATOR, active=True):
    return User.objects.create_user(username=username, password="correct-horse-battery", role=role, is_active=active)


def make_config(user=None):
    existing = ConfigVersion.objects.filter(version_no=1).first()
    if existing:
        return existing
    user = user or User.objects.filter(username="cfg-owner").first() or make_user("cfg-owner", User.Role.ADMINISTRATOR)
    return ConfigVersion.objects.create(version_no=1, content={}, is_active=True, created_by=user)


def make_model(config):
    existing = ModelVersion.objects.filter(label="m1").first()
    if existing:
        return existing
    now = timezone.now()
    return ModelVersion.objects.create(
        label="m1", trained_at=now, train_start=now - timedelta(days=7), train_end=now,
        baselines={}, file_path="m1.joblib", config=config,
    )


def make_node(name="compute01"):
    return Node.objects.create(name=name, source_label=f"{name}:9100")


def make_alert(node=None, status=Alert.Status.OPEN, outcome=None, mode=Alert.Mode.LIVE, config=None, model=None):
    config = config or make_config()
    model = model or make_model(config)
    node = node or make_node()
    return Alert.objects.create(
        node=node, mode=mode, status=status, outcome=outcome, opened_at=timezone.now(),
        peak_score=4.2, explanation={"summary": "test"}, model=model, config=config,
        closed_at=timezone.now() if status == Alert.Status.CLOSED else None,
    )
