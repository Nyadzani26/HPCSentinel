"""Forms for the review and control screens. Validation lives here, not in the templates (FR-22, FR-24, FR-26)."""
from django import forms
from django.utils import timezone

from core.models import Alert, ExcludedPeriod, ModelVersion, Node
from core.services import chassis as chassis_service

DT = forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M")


class _DateTimeField(forms.DateTimeField):
    def __init__(self, **kw):
        kw.setdefault("widget", DT)
        kw.setdefault("input_formats", ["%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S"])
        super().__init__(**kw)


class CloseForm(forms.Form):
    outcome = forms.ChoiceField(choices=Alert.Outcome.choices, error_messages={"required": "Choose Confirmed or False alarm."})
    note = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 3}))


class NoteForm(forms.Form):
    note = forms.CharField(widget=forms.Textarea(attrs={"rows": 3}), error_messages={"required": "Write a note first."})


def _not_in_future(data, what):
    if data.get("end") and data["end"] > timezone.now():
        raise forms.ValidationError(f"The end of the {what} cannot be in the future. Check the year and the time.")


class TrainForm(forms.Form):
    start = _DateTimeField()
    end = _DateTimeField()
    label = forms.CharField(required=False, max_length=50)

    def clean(self):
        data = super().clean()
        if data.get("start") and data.get("end") and data["end"] <= data["start"]:
            raise forms.ValidationError("The end of the period must be after its start.")
        _not_in_future(data, "training period")
        return data


class ModelPeriodForm(forms.Form):
    model = forms.ModelChoiceField(queryset=ModelVersion.objects.none())
    start = _DateTimeField()
    end = _DateTimeField()

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.fields["model"].queryset = ModelVersion.objects.order_by("-trained_at")
        self.fields["model"].empty_label = None  # the newest model is pre-selected

    def clean(self):
        data = super().clean()
        if data.get("start") and data.get("end") and data["end"] <= data["start"]:
            raise forms.ValidationError("The end of the period must be after its start.")
        _not_in_future(data, "period")
        return data


class ExcludedPeriodForm(forms.ModelForm):
    start_at = _DateTimeField()
    end_at = _DateTimeField()

    class Meta:
        model = ExcludedPeriod
        fields = ["start_at", "end_at", "reason"]

    def clean(self):
        data = super().clean()
        if data.get("start_at") and data.get("end_at") and data["end_at"] <= data["start_at"]:
            raise forms.ValidationError("The end of the period must be after its start.")
        return data


class ChassisForm(forms.Form):
    """The chassis layout as text (see core/services/chassis.py). It is only checked against the nodes the platform knows."""

    layout = forms.CharField(required=False, widget=forms.Textarea(attrs={
        "rows": 6, "spellcheck": "false", "placeholder": "Chassis 1: compute01-compute12\nChassis 2: compute13-compute24"}))

    def clean_layout(self):
        try:
            return chassis_service.parse(self.cleaned_data["layout"], Node.objects.values_list("name", flat=True))
        except chassis_service.LayoutError as exc:
            raise forms.ValidationError(str(exc))


class ExperimentUploadForm(forms.Form):
    file = forms.FileField(help_text="CSV with columns node, anomaly_type, start, end, intensity (optional). A time with no zone is read as UTC, which is what tools/fault_run.sh writes; for local time add the offset, for example 2026-10-21T14:30:00+02:00.")


# One choice instead of two numbers: how unusual a node must be, and for how long, before it is worth an alert.
SENSITIVITY = {"relaxed": (4.0, 4), "balanced": (3.0, 3), "sensitive": (2.5, 2)}
SENSITIVITY_CHOICES = [("relaxed", "Relaxed"), ("balanced", "Balanced"), ("sensitive", "Sensitive"), ("custom", "Custom")]


def sensitivity_of(threshold, persistence) -> str:
    for name, (t, n) in SENSITIVITY.items():
        if float(threshold) == t and int(persistence) == n:
            return name
    return "custom"


class SettingsForm(forms.Form):
    prometheus_url = forms.CharField(label="Prometheus address", max_length=200)
    sensitivity = forms.ChoiceField(choices=SENSITIVITY_CHOICES, required=False, widget=forms.RadioSelect)
    alert_threshold = forms.FloatField(label="Alert threshold", min_value=0.1, max_value=100, required=False,
                                       localize=False, widget=forms.TextInput(attrs={"inputmode": "decimal"}))
    persistence_windows = forms.IntegerField(label="Consecutive abnormal windows required", min_value=1, max_value=20, required=False)

    def clean(self):
        data = super().clean()
        choice = data.get("sensitivity") or "custom"
        if choice in SENSITIVITY:
            data["alert_threshold"], data["persistence_windows"] = SENSITIVITY[choice]
            self.errors.pop("alert_threshold", None)
            self.errors.pop("persistence_windows", None)
        else:
            for field in ("alert_threshold", "persistence_windows"):
                if data.get(field) is None and field not in self.errors:
                    self.add_error(field, "Enter a number.")
        return data


class UserCreateForm(forms.Form):
    username = forms.CharField(max_length=150)
    password = forms.CharField(widget=forms.PasswordInput)
    role = forms.ChoiceField(choices=[("OPERATOR", "Operator"), ("ADMINISTRATOR", "Administrator")])

    def clean_username(self):
        from core.models import User
        name = self.cleaned_data["username"].strip()
        if User.objects.filter(username__iexact=name).exists():
            raise forms.ValidationError("That username is already in use.")
        return name

    def clean(self):
        data = super().clean()
        if data.get("password"):
            from django.contrib.auth.password_validation import validate_password
            try:
                validate_password(data["password"])
            except forms.ValidationError as exc:
                self.add_error("password", exc)
        return data
