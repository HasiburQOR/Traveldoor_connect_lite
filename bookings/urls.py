"""URL patterns for the bookings app.

Three separate pattern lists are included by the root URLconf under
different prefixes:

    /b/                 public directory — every bookable event in one place
    /b/<slug>/…         public visitor flow (FR-4)
    /m/<token>/…       tokenised visitor manage links (FR-4.6)
    /panel/bookings/…  admin booking management (FR-7)

All lists share the ``bookings`` app_name so reverse() names are stable
no matter which prefix a view was reached under.
"""
from django.urls import path

from . import views

app_name = "bookings"

public_patterns = [
    path("", views.public_events, name="public_events"),
    path("<slug:slug>/", views.public_event, name="public_event"),
    path("<slug:slug>/host/<int:pk>/", views.public_host, name="public_host"),
    path("<slug:slug>/book/<int:slot_pk>/", views.public_book, name="public_book"),
]

manage_patterns = [
    path("<str:token>/", views.manage_view, name="manage"),
    path("<str:token>/reschedule/", views.manage_reschedule, name="manage_reschedule"),
    path("<str:token>/cancel/", views.manage_cancel, name="manage_cancel"),
]

admin_patterns = [
    path("", views.booking_list, name="admin_list"),
    path("export/", views.booking_export, name="admin_export"),
    path("new/", views.booking_create, name="admin_create"),
    path("<int:pk>/", views.booking_detail, name="admin_detail"),
    path("<int:pk>/edit/", views.booking_update, name="admin_update"),
    path("<int:pk>/resend/", views.booking_resend_confirmation, name="admin_resend"),
    path("<int:pk>/cancel/", views.booking_cancel, name="admin_cancel"),
    path("<int:pk>/reschedule/", views.booking_reschedule, name="admin_reschedule"),
    path("<int:pk>/clear-flag/", views.booking_clear_flag, name="admin_clear_flag"),
]
