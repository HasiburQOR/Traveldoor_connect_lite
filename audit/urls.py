"""Audit URLs — read-only audit log (FR-9)."""
from django.urls import path

from . import views

app_name = "audit"

urlpatterns = [
    path("", views.audit_list, name="list"),
]
