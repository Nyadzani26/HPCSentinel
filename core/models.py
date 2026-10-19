"""Data model of HPCSentinel (design Section 5.6, Figure 10).

Integrity rules that can be expressed in the database are expressed here as constraints,
so that a fault in the application cannot break them:

* BR-06  an alert has an outcome if and only if it is Closed
* BR-08  at most one live alert per node may be Open or Acknowledged
* a node is scored at most once per cycle
* an excluded period or an injected episode must end after it starts

Rules about the order of events (BR-05, BR-07) are enforced by the Alert methods below
and by core.services.review.ReviewService.
"""
from django.contrib.auth.models import AbstractUser
from django.db import models
from django.db.models import F, Q


class User(AbstractUser):
    """A person who signs in. Role decides what they may do (FR-02, BR-04)."""

    class Role(models.TextChoices):
        ADMINISTRATOR = "ADMINISTRATOR", "Administrator"
        OPERATOR = "OPERATOR", "Operator"

    role = models.CharField(max_length=13, choices=Role.choices, default=Role.OPERATOR)

    @property
    def is_administrator(self) -> bool:
        return self.is_active and self.role == self.Role.ADMINISTRATOR

    def save(self, *args, **kwargs):
        # An account made with "createsuperuser" is an Administrator of the platform.
        if self.is_superuser:
            self.role = self.Role.ADMINISTRATOR
        super().save(*args, **kwargs)


class ConfigVersion(models.Model):
    """A versioned snapshot of the platform configuration (FR-05, BR-10)."""

    version_no = models.PositiveIntegerField(unique=True)
    content = models.JSONField()
    is_active = models.BooleanField(default=False)
    created_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name="config_versions")
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Configuration v{self.version_no}"


class PlatformSetting(models.Model):
    """A setting an Administrator changes in the web interface (FR-04, FR-18, FR-27).

    Settings override the configuration file, are validated before saving, and record
    who changed them. The effective configuration is versioned like any other (BR-10).
    """

    key = models.CharField(max_length=50, unique=True)
    value = models.JSONField()
    updated_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name="settings_changed")
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.key


class NodeGroup(models.Model):
    """A group of comparable nodes; peers are compared only within a group (FR-07)."""

    name = models.CharField(max_length=100, unique=True)

    def __str__(self):
        return self.name


class Node(models.Model):
    name = models.CharField(max_length=100, unique=True)
    source_label = models.CharField(max_length=200, help_text="Identifier under which Prometheus reports the node.")
    group = models.ForeignKey(NodeGroup, null=True, blank=True, on_delete=models.SET_NULL, related_name="nodes")
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return self.name


class Job(models.Model):
    """A request from the web application that the worker carries out (design decision D-3)."""

    class Type(models.TextChoices):
        TRAIN = "TRAIN", "Train"
        REPLAY = "REPLAY", "Replay"
        EVALUATE = "EVALUATE", "Evaluate"

    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        RUNNING = "RUNNING", "Running"
        DONE = "DONE", "Done"
        FAILED = "FAILED", "Failed"

    type = models.CharField(max_length=10, choices=Type.choices)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    parameters = models.JSONField(null=True, blank=True)
    requested_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name="jobs")
    requested_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    message = models.TextField(blank=True, help_text="Outcome or error text shown to the administrator.")


class CycleRun(models.Model):
    """One run of the detection cycle (workflow 1, Figure 3)."""

    class Mode(models.TextChoices):
        LIVE = "LIVE", "Live"
        REPLAY = "REPLAY", "Replay"

    mode = models.CharField(max_length=6, choices=Mode.choices, default=Mode.LIVE)
    started_at = models.DateTimeField()
    finished_at = models.DateTimeField(null=True, blank=True)
    source_reachable = models.BooleanField()
    config = models.ForeignKey(ConfigVersion, on_delete=models.PROTECT, related_name="cycles")

    class Meta:
        indexes = [models.Index(fields=["mode", "started_at"])]


class ModelVersion(models.Model):
    """A trained model with its baselines (BR-10)."""

    label = models.CharField(max_length=50, unique=True)
    trained_at = models.DateTimeField()
    train_start = models.DateTimeField()
    train_end = models.DateTimeField()
    baselines = models.JSONField()
    file_path = models.CharField(max_length=255)
    config = models.ForeignKey(ConfigVersion, on_delete=models.PROTECT, related_name="models")
    job = models.ForeignKey(Job, null=True, blank=True, on_delete=models.PROTECT, related_name="models")

    def __str__(self):
        return self.label


class InvalidTransition(Exception):
    """Raised when a change to an alert is not allowed by BR-05, BR-06 or BR-07."""


class Alert(models.Model):
    class Mode(models.TextChoices):
        LIVE = "LIVE", "Live"
        REPLAY = "REPLAY", "Replay"

    class Status(models.TextChoices):
        OPEN = "OPEN", "Open"
        ACKNOWLEDGED = "ACKNOWLEDGED", "Acknowledged"
        CLOSED = "CLOSED", "Closed"

    class Outcome(models.TextChoices):
        CONFIRMED = "CONFIRMED", "Confirmed"
        FALSE_ALARM = "FALSE_ALARM", "False alarm"

    node = models.ForeignKey(Node, on_delete=models.PROTECT, related_name="alerts")
    mode = models.CharField(max_length=6, choices=Mode.choices, default=Mode.LIVE)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.OPEN)
    outcome = models.CharField(max_length=12, choices=Outcome.choices, null=True, blank=True)
    opened_at = models.DateTimeField()
    closed_at = models.DateTimeField(null=True, blank=True)
    peak_score = models.FloatField()
    explanation = models.JSONField()
    model = models.ForeignKey(ModelVersion, on_delete=models.PROTECT, related_name="alerts")
    config = models.ForeignKey(ConfigVersion, on_delete=models.PROTECT, related_name="alerts")

    class Meta:
        constraints = [
            # BR-06: an outcome exists if and only if the alert is closed.
            models.CheckConstraint(
                condition=(Q(status="CLOSED", outcome__isnull=False) | (~Q(status="CLOSED") & Q(outcome__isnull=True))),
                name="alert_outcome_iff_closed",
            ),
            # BR-08: one live alert per node while it is Open or Acknowledged.
            models.UniqueConstraint(
                fields=["node"],
                condition=Q(status__in=["OPEN", "ACKNOWLEDGED"], mode="LIVE"),
                name="one_live_open_alert_per_node",
            ),
        ]
        indexes = [
            models.Index(fields=["status", "opened_at"]),
        ]

    def __str__(self):
        return f"Alert {self.pk} on {self.node} ({self.status})"

    # --- lifecycle (Figure 7; BR-05, BR-06, BR-07) -------------------------------------
    def acknowledge(self):
        if self.status != self.Status.OPEN:
            raise InvalidTransition("Only an open alert can be acknowledged.")
        self.status = self.Status.ACKNOWLEDGED
        self.save(update_fields=["status"])

    def close(self, outcome, when):
        if self.status != self.Status.ACKNOWLEDGED:
            raise InvalidTransition("An alert must be acknowledged before it can be closed.")
        if outcome not in self.Outcome.values:
            raise InvalidTransition("An outcome of Confirmed or False alarm is required to close an alert.")
        self.status = self.Status.CLOSED
        self.outcome = outcome
        self.closed_at = when
        self.save(update_fields=["status", "outcome", "closed_at"])

    def reopen(self):
        if self.status != self.Status.CLOSED:
            raise InvalidTransition("Only a closed alert can be reopened.")
        self.status = self.Status.ACKNOWLEDGED
        self.outcome = None
        self.closed_at = None
        self.save(update_fields=["status", "outcome", "closed_at"])


class NodeWindow(models.Model):
    """One node scored in one cycle (FR-09 to FR-14)."""

    class Quality(models.TextChoices):
        RELIABLE = "RELIABLE", "Reliable"
        UNRELIABLE = "UNRELIABLE", "Unreliable"
        NOT_LEARNED = "NOT_LEARNED", "Not yet learned"

    class LoadState(models.TextChoices):
        IDLE = "IDLE", "Idle"
        LOADED = "LOADED", "Loaded"

    node = models.ForeignKey(Node, on_delete=models.PROTECT, related_name="windows")
    cycle = models.ForeignKey(CycleRun, on_delete=models.PROTECT, related_name="windows")
    window_end = models.DateTimeField()
    quality = models.CharField(max_length=12, choices=Quality.choices)
    load_state = models.CharField(max_length=6, choices=LoadState.choices, null=True, blank=True)
    features = models.JSONField(default=dict)
    baseline_score = models.FloatField(null=True, blank=True)
    peer_score = models.FloatField(null=True, blank=True)
    model_score = models.FloatField(null=True, blank=True)
    combined_score = models.FloatField(null=True, blank=True)
    model = models.ForeignKey(ModelVersion, null=True, blank=True, on_delete=models.PROTECT, related_name="windows")
    alert = models.ForeignKey(Alert, null=True, blank=True, on_delete=models.PROTECT, related_name="windows")

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["node", "cycle"], name="one_window_per_node_per_cycle"),
        ]
        indexes = [models.Index(fields=["node", "window_end"])]


class AlertAction(models.Model):
    """One action taken on an alert, with who and when (FR-25, FR-27, BR-11)."""

    class Action(models.TextChoices):
        ACKNOWLEDGE = "ACKNOWLEDGE", "Acknowledge"
        CLOSE = "CLOSE", "Close"
        REOPEN = "REOPEN", "Reopen"
        NOTE = "NOTE", "Note"

    alert = models.ForeignKey(Alert, on_delete=models.PROTECT, related_name="actions")
    user = models.ForeignKey(User, on_delete=models.PROTECT, related_name="alert_actions")
    action = models.CharField(max_length=12, choices=Action.choices)
    outcome = models.CharField(max_length=12, choices=Alert.Outcome.choices, null=True, blank=True)
    note = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=["alert", "created_at"])]


class ExcludedPeriod(models.Model):
    """A period never used for learning, such as an injected test (FR-16, BR-03)."""

    start_at = models.DateTimeField()
    end_at = models.DateTimeField()
    reason = models.CharField(max_length=255)
    created_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name="excluded_periods")

    class Meta:
        constraints = [models.CheckConstraint(condition=Q(end_at__gt=F("start_at")), name="excluded_period_ends_after_start")]


class ExperimentEntry(models.Model):
    """One logged injected anomaly, used as ground truth when evaluating (FR-29)."""

    class AnomalyType(models.TextChoices):
        CPU = "CPU", "CPU"
        MEMORY = "MEMORY", "Memory"
        IO = "IO", "Storage I/O"
        NETWORK = "NETWORK", "Network"
        OTHER = "OTHER", "Other"

    node = models.ForeignKey(Node, on_delete=models.PROTECT, related_name="experiments")
    anomaly_type = models.CharField(max_length=20, choices=AnomalyType.choices)
    start_at = models.DateTimeField()
    end_at = models.DateTimeField()
    intensity = models.CharField(max_length=20, blank=True)
    imported_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name="imported_experiments")

    class Meta:
        constraints = [models.CheckConstraint(condition=Q(end_at__gt=F("start_at")), name="experiment_ends_after_start")]


class EvaluationRun(models.Model):
    """Result of evaluating a model over a period (FR-30)."""

    job = models.ForeignKey(Job, on_delete=models.PROTECT, related_name="evaluations")
    model = models.ForeignKey(ModelVersion, on_delete=models.PROTECT, related_name="evaluations")
    period_start = models.DateTimeField()
    period_end = models.DateTimeField()
    episodes_total = models.PositiveIntegerField()
    episodes_detected = models.PositiveIntegerField()
    median_delay_seconds = models.FloatField(null=True, blank=True)
    false_alerts_per_node_week = models.FloatField()
