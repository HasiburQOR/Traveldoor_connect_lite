"""Admin (panel) URLs for events, teams and slots."""
from django.urls import path

from . import views

app_name = "events"

urlpatterns = [
    path("", views.event_list, name="list"),
    path("new/", views.event_create, name="create"),
    path("event/<int:pk>/", views.event_detail, name="detail"),
    path("event/<int:pk>/edit/", views.event_update, name="update"),
    path("event/<int:pk>/delete/", views.event_delete, name="delete"),
    path("event/<int:pk>/delete/confirm/", views.event_confirm_delete, name="delete_confirm"),
    path("event/<int:pk>/clone/", views.event_clone, name="clone"),
    path("event/<int:pk>/publish/", views.event_publish, name="publish"),
    path("event/<int:pk>/close/", views.event_close, name="close"),
    path("event/<int:pk>/reopen/", views.event_reopen, name="reopen"),
    # Team (FR-2)
    path("event/<int:event_pk>/team/add/", views.team_add, name="team_add"),
    path("event/<int:event_pk>/team/<int:pk>/edit/", views.team_edit, name="team_edit"),
    path("event/<int:event_pk>/team/<int:pk>/remove/", views.team_remove, name="team_remove"),
    path("event/<int:event_pk>/team/<int:pk>/reactivate/", views.team_reactivate, name="team_reactivate"),
    path("event/<int:event_pk>/team/<int:pk>/delete/", views.team_delete, name="team_delete"),
    # Slots (FR-3)
    path("event/<int:event_pk>/slots/add/", views.slot_add, name="slot_add"),
    path("event/<int:event_pk>/slots/bulk/", views.slot_bulk_add, name="slot_bulk"),
    path("event/<int:event_pk>/slots/<int:pk>/edit/", views.slot_edit, name="slot_edit"),
    path("event/<int:event_pk>/slots/<int:pk>/delete/", views.slot_delete, name="slot_delete"),
    path("event/<int:event_pk>/slots/<int:pk>/close/", views.slot_close, name="slot_close"),
    path("event/<int:event_pk>/slots/<int:pk>/reopen/", views.slot_reopen, name="slot_reopen"),
]
