"""Data model of HPCSentinel (design Section 5.6, Figure 10).

Integrity rules that can be expressed in the database are expressed here as constraints,
so that a fault in the application cannot break them:

* BR-06  an alert has an outcome if and only if it is Closed (Reviewed)
* BR-08  at most one live alert per node may be Active (Open or Acknowledged); a Recovered alert does not count
* a node is scored at most once per cycle
* an excluded period or an injected episode must end after it starts

Rules about the order of events (BR-05, BR-07) are enforced by the Alert methods below
and by core.services.review.ReviewService. BR-05 was relaxed after the first deployment: acknowledging is
optional, because an alert that recovers by itself and is reviewed hours later should not have to be
"acknowledged" first.
"""
from datetime import timedelta

from django.contrib.auth.models import AbstractUser
from django.db import models
from django.db.models import F, Q
from django.utils import timezone


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


class Chassis(models.Model):
    """A physical enclosure. It only arranges nodes on screen; it never changes how a node is scored (a NodeGroup does that)."""

    name = models.CharField(max_length=60, unique=True)

    class Meta:
        verbose_name_plural = "chassis"

    def __str__(self):
        return self.name


class Node(models.Model):
    name = models.CharField(max_length=100, unique=True)
    source_label = models.CharField(max_length=200, help_text="Identifier under which Prometheus reports the node.")
    group = models.ForeignKey(NodeGroup, null=True, blank=True, on_delete=models.SET_NULL, related_name="nodes")
    chassis = models.ForeignKey(Chassis, null=True, blank=True, on_delete=models.SET_NULL, related_name="nodes",
                                help_text="Where the node sits, for the Overview. Display only.")
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
        OPEN = "OPEN", "Active"
        ACKNOWLEDGED = "ACKNOWLEDGED", "Being looked at"
        RESOLVED = "RESOLVED", "Recovered"      # the problem ended by itself; nobody has judged it yet
        CLOSED = "CLOSED", "Reviewed"

    # Alerts that are still happening, as opposed to ended (RESOLVED) or judged (CLOSED).
    ACTIVE = ("OPEN", "ACKNOWLEDGED")

    class Outcome(models.TextChoices):
        CONFIRMED = "CONFIRMED", "Confirmed"
        FALSE_ALARM = "FALSE_ALARM", "False alarm"

    node = models.ForeignKey(Node, on_delete=models.PROTECT, related_name="alerts")
    mode = models.CharField(max_length=6, choices=Mode.choices, default=Mode.LIVE)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.OPEN)
    outcome = models.CharField(max_length=12, choices=Outcome.choices, null=True, blank=True)
    opened_at = models.DateTimeField()
    closed_at = models.DateTimeField(null=True, blank=True)
    # An alert is one story, not one event: it keeps updating while the node stays unusual (last_seen_at, peak_score,
    # explanation["latest"]), ends by itself (recovered_at) and reopens if the node flares up again soon after.
    last_seen_at = models.DateTimeField(null=True, blank=True)
    recovered_at = models.DateTimeField(null=True, blank=True)
    resolved_reason = models.CharField(max_length=12, blank=True, default="")   # "recovered" or "no_data"
    recurrences = models.PositiveIntegerField(default=0)
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

    # --- lifecycle (Figure 7; BR-05, BR-06, BR-07) --------------------------------------
    # Active (OPEN, ACKNOWLEDGED) -> Recovered (RESOLVED, by the platform) -> Reviewed (CLOSED, by a person).
    # A person may review an alert in any of the first three states; reviewing is optional for the platform to work.
    def acknowledge(self):
        if self.status != self.Status.OPEN:
            raise InvalidTransition("Only an active alert can be marked as being looked at.")
        self.status = self.Status.ACKNOWLEDGED
        self.save(update_fields=["status"])

    def close(self, outcome, when):
        if self.status == self.Status.CLOSED:
            raise InvalidTransition("This alert has already been reviewed.")
        if outcome not in self.Outcome.values:
            raise InvalidTransition("An outcome of Confirmed or False alarm is required to close an alert.")
        self.status = self.Status.CLOSED
        self.outcome = outcome
        self.closed_at = when
        self.save(update_fields=["status", "outcome", "closed_at"])

    def reopen(self):
        if self.status != self.Status.CLOSED:
            raise InvalidTransition("Only a reviewed alert can be reopened.")
        self.status = self.Status.ACKNOWLEDGED
        self.outcome = None
        self.closed_at = None
        self.recovered_at = None
        self.resolved_reason = ""
        self.save(update_fields=["status", "outcome", "closed_at", "recovered_at", "resolved_reason"])

    def resolve(self, when, reason):
        """The platform ends an alert because the node is normal again, or has stopped reporting."""
        if self.status not in self.ACTIVE:
            raise InvalidTransition("Only an active alert can recover.")
        self.status = self.Status.RESOLVED
        self.recovered_at = when
        self.resolved_reason = reason
        self.save(update_fields=["status", "recovered_at", "resolved_reason"])

    def flare_up(self, when):
        """The node became unusual again soon after recovering: continue the same alert."""
        if self.status != self.Status.RESOLVED:
            raise InvalidTransition("Only a recovered alert can come back.")
        self.status = self.Status.OPEN
        self.recovered_at = None
        self.resolved_reason = ""
        self.recurrences += 1
        self.last_seen_at = when
        self.save(update_fields=["status", "recovered_at", "resolved_reason", "recurrences", "last_seen_at"])

    @property
    def is_active(self):
        return self.status in self.ACTIVE

    @property
    def lasted(self):
        """How long the node was unusual: up to now while active, up to the last unusual check once it ended."""
        end = timezone.now() if self.is_active else (self.last_seen_at or self.opened_at)
        return max(end - self.opened_at, timedelta(0))


class ClusterEvent(models.Model):
    """Many nodes becoming unusual at the same moment, recorded once instead of as one alert per node (stage 4)."""

    started_at = models.DateTimeField()
    last_seen_at = models.DateTimeField()
    ended_at = models.DateTimeField(null=True, blank=True)
    peak_nodes = models.PositiveIntegerField(default=0)       # most nodes unusual in a single cycle
    peak_reliable = models.PositiveIntegerField(default=0)    # nodes with usable data in that cycle
    nodes = models.JSONField(default=list)                    # every node involved at any point
    causes = models.JSONField(default=dict)                   # main contributing metric -> most nodes it was the main cause on
    quiet_cycles = models.PositiveIntegerField(default=0)     # consecutive ordinary cycles since the last unusual one

    class Meta:
        ordering = ["-started_at"]
        indexes = [models.Index(fields=["started_at"])]

    @property
    def is_active(self):
        return self.ended_at is None

    @property
    def lasted(self):
        return max((self.ended_at or self.last_seen_at) - self.started_at, timedelta(0))

    def __str__(self):
        return f"Cluster event {self.started_at:%Y-%m-%d %H:%M} ({self.peak_nodes} nodes)"


class Report(models.Model):
    """A saved daily, weekly or monthly summary (stage 3). The numbers are frozen when it is generated, so a report
    read next month says what was known at the time; an Administrator can regenerate it to include later reviews."""

    class Kind(models.TextChoices):
        DAILY = "daily", "Daily"
        WEEKLY = "weekly", "Weekly"
        MONTHLY = "monthly", "Monthly"

    kind = models.CharField(max_length=8, choices=Kind.choices)
    period_start = models.DateTimeField()
    period_end = models.DateTimeField()
    generated_at = models.DateTimeField()
    data = models.JSONField()

    class Meta:
        constraints = [models.UniqueConstraint(fields=["kind", "period_start"], name="one_report_per_kind_and_period")]
        ordering = ["-period_start"]

    def __str__(self):
        return f"{self.get_kind_display()} report {self.period_start:%Y-%m-%d}"


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
        RECOVER = "RECOVER", "Recovered"       # recorded by the platform itself
        RECUR = "RECUR", "Came back"           # recorded by the platform itself

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
    intensity = models.CharField(max_length=200, blank=True)     # free text such as "stress-ng 8 workers, 90% of memory"
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
