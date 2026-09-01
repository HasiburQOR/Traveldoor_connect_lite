"""
URL configuration for the TravelDoor Connect project.

Sections:
  /panel/...          staff/admin area (events, team, slots, bookings, audit, users)
  /b/<slug>/...       public visitor booking flow (FR-4)
  /m/<token>/...      visitor manage-booking links from confirmation emails (FR-4.6)
"""
from django.contrib import admin
from django.urls import include, path

from bookings import urls as booking_urls

urlpatterns = [
    path("admin/", admin.site.urls),
    path("", include("core.urls")),
    path("accounts/", include("accounts.urls")),
    path("notifications/", include("notifications.urls")),
    path("audit/", include("audit.urls")),
    path("b/", include((booking_urls.public_patterns, "bookings_public"))),
    path("m/", include((booking_urls.manage_patterns, "bookings_manage"))),
    path("panel/", include("events.urls")),
    path("panel/bookings/", include((booking_urls.admin_patterns, "bookings_admin"))),
]
