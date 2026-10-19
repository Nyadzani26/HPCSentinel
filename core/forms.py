"""Forms for the review and control screens. Validation lives here, not in the templates (FR-22, FR-26)."""
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
