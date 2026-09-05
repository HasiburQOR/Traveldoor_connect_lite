"""
URL configuration for the TravelDoor Connect project.

Sections:
  /panel/...          staff/admin area (events, team, slots, bookings, audit, users)
  /b/<slug>/...       public visitor booking flow (FR-4)
  /m/<token>/...      visitor manage-booking links from confirmation emails (FR-4.6)

The Django admin site is deliberately NOT mounted: the branded login page at
/accounts/login/ (with its self-service password-reset flow) is the only
staff entrance.
"""
from django.conf import settings
from django.urls import include, path, re_path
from django.views.static import serve as serve_static

from bookings import urls as booking_urls
from events import urls as events_urls

urlpatterns = [
    path("", include("core.urls")),
    path("accounts/", include("accounts.urls")),
    path("notifications/", include("notifications.urls")),
    path("audit/", include("audit.urls")),
    path("b/", include((booking_urls.public_patterns, "bookings_public"))),
    path("m/", include((booking_urls.manage_patterns, "bookings_manage"))),
    path("panel/", include("events.urls")),
    path("panel/people/", include((events_urls.people_patterns, "events_people"))),
    path("panel/bookings/", include((booking_urls.admin_patterns, "bookings_admin"))),
]

# Uploaded host photos. WhiteNoise handles only build-time static files and
# this deployment runs a single gunicorn container (no nginx), so media is
# served by Django directly — fine at this app's traffic level.
urlpatterns += [
    re_path(r"^media/(?P<path>.*)$", serve_static, {"document_root": settings.MEDIA_ROOT}),
]
