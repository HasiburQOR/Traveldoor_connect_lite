"""Core views — the admin dashboard (FR-8.3) and a health endpoint."""
from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth.decorators import user_passes_test
from django.db.models import Count
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from audit.models import AuditLogEntry, log_action
from bookings.models import Booking
from events.models import Event, Slot
from notifications.emails import email_health, send_test_email

from .forms import SiteSettingsForm
from .models import SiteSettings
from notifications.models import HostNotification, NotificationLog, SchedulerHeartbeat


@staff_member_required
def dashboard(request):
    """Operational overview: upcoming meetings, flags, recent activity."""
    today = timezone.localdate()
    upcoming = (
        Booking.objects.filter(status=Booking.STATUS_CONFIRMED, slot__date__gte=today)
        .select_related("slot", "slot__host", "event")
        .order_by("slot__date", "slot__start_time")[:10]
    )
    flags = Booking.objects.filter(needs_attention=True, status=Booking.STATUS_CONFIRMED).count()
    recent = AuditLogEntry.objects.select_related("actor")[:10]
    failed = NotificationLog.objects.filter(status=NotificationLog.STATUS_FAILED).count()
    return render(request, "core/dashboard.html", {
        "stats": {
            "events": Event.objects.count(),
            "live_events": Event.objects.filter(status=Event.STATUS_LIVE).count(),
            "hosts": Event.objects.aggregate(n=Count("team_members", distinct=True))["n"],
            "slots": Slot.objects.count(),
            "bookings": Booking.objects.filter(status=Booking.STATUS_CONFIRMED).count(),
            "flags": flags,
            "failed_emails": failed,
        },
        "upcoming": upcoming,
        "recent": recent,
        "notifications": HostNotification.objects.select_related("host", "event")[:10],
        "email_health": email_health(),
        "heartbeat": SchedulerHeartbeat.current(),
    })


@staff_member_required
@require_POST
def send_test_email_view(request):
    """Prove (or disprove) that outbound email works, in one click."""
    to = request.user.email
    if not to:
        messages.error(request, "Add an email address to your admin account first.")
        return redirect("core:dashboard")
    ok, detail = send_test_email(to)
    if ok:
        messages.success(request, f"Test email: {detail}")
    else:
        messages.error(request, f"Test email failed: {detail}")
    return redirect("core:dashboard")


def health(request):
    """Liveness probe (NFR-1)."""
    return JsonResponse({"status": "ok"})


def _superuser_required(user):
    """Settings hold live mail credentials — full-access admins only."""
    return user.is_active and user.is_superuser


@staff_member_required
@user_passes_test(_superuser_required)
def site_settings_view(request):
    """FR-8 — change the sending mailbox and branding without a redeploy."""
    site = SiteSettings.load()
    form = SiteSettingsForm(request.POST or None, instance=site)
    if request.method == "POST" and form.is_valid():
        site = form.save(commit=False)
        site.updated_by = request.user
        site.save()
        log_action(request.user, AuditLogEntry.ACTION_UPDATE, site,
                   details="Site settings updated.",
                   entity_type=AuditLogEntry.ENTITY_ADMIN_ACCOUNT)
        messages.success(
            request,
            "Settings saved. Send a test email to confirm mail is getting through.",
        )
        return redirect("core:settings")
    return render(request, "core/settings.html", {
        "form": form,
        "site": site,
        "email_health": email_health(),
    })