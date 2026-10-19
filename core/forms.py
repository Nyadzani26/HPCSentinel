"""Forms for the review and control screens. Validation lives here, not in the templates (FR-22, FR-24, FR-26)."""
from django import forms

from core.models import Alert, ExcludedPeriod, ModelVersion

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


class TrainForm(forms.Form):
    start = _DateTimeField()
    end = _DateTimeField()
    label = forms.CharField(required=False, max_length=50)

    def clean(self):
        data = super().clean()
        if data.get("start") and data.get("end") and data["end"] <= data["start"]:
            raise forms.ValidationError("The end of the period must be after its start.")
        return data


class ModelPeriodForm(forms.Form):
    model = forms.ModelChoiceField(queryset=ModelVersion.objects.none())
    start = _DateTimeField()
    end = _DateTimeField()

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.fields["model"].queryset = ModelVersion.objects.order_by("-trained_at")

    def clean(self):
        data = super().clean()
        if data.get("start") and data.get("end") and data["end"] <= data["start"]:
            raise forms.ValidationError("The end of the period must be after its start.")
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


class ExperimentUploadForm(forms.Form):
    file = forms.FileField(help_text="CSV with columns node, anomaly_type, start, end, intensity (optional).")


class SettingsForm(forms.Form):
    prometheus_url = forms.CharField(label="Prometheus address", max_length=200)
    alert_threshold = forms.FloatField(label="Alert threshold", min_value=0.1, max_value=100)
    persistence_windows = forms.IntegerField(label="Consecutive abnormal windows required", min_value=1, max_value=20)


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
