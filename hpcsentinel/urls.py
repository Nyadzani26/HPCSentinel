from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import path

from core import views

urlpatterns = [
    path("admin/", admin.site.urls),
    path("accounts/login/", auth_views.LoginView.as_view(), name="login"),
    path("accounts/logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("health/", views.health, name="health"),
    path("", views.home, name="home"),
    path("nodes/<str:name>/", views.node_detail, name="node_detail"),
    path("alerts/", views.alert_list, name="alert_list"),
    path("alerts/<int:pk>/", views.alert_detail, name="alert_detail"),
    path("alerts/<int:pk>/acknowledge/", views.alert_acknowledge, name="alert_acknowledge"),
    path("alerts/<int:pk>/close/", views.alert_close, name="alert_close"),
    path("alerts/<int:pk>/reopen/", views.alert_reopen, name="alert_reopen"),
    path("alerts/<int:pk>/note/", views.alert_note, name="alert_note"),
    path("controls/", views.controls, name="controls"),
    path("controls/excluded/<int:pk>/delete/", views.excluded_delete, name="excluded_delete"),
]
