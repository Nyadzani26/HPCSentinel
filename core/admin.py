from django.contrib import admin
from django.contrib.auth.admin import UserAdmin

from core import models


@admin.register(models.User)
class PlatformUserAdmin(UserAdmin):
    fieldsets = UserAdmin.fieldsets + (("Platform", {"fields": ("role",)}),)
    list_display = ("username", "role", "is_active")


for model in (
    models.ConfigVersion, models.NodeGroup, models.Chassis, models.Node, models.Job, models.CycleRun, models.ModelVersion,
    models.Alert, models.AlertAction, models.NodeWindow, models.ExcludedPeriod, models.ExperimentEntry,
    models.EvaluationRun,
):
    admin.site.register(model)
