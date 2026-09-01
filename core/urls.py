"""Core URLs — dashboard (FR-8.3 landing) and health check."""
from django.urls import path

from . import views

app_name = "core"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("settings/", views.site_settings_view, name="settings"),
    path("test-email/", views.send_test_email_view, name="test_email"),
    path("health/", views.health, name="health"),
]
