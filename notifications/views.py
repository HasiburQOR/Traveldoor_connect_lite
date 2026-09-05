"""In-app host notification views (FR-6.3)."""
from core.decorators import staff_member_required  # app login page, not the Django admin
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from events.models import TeamMember

from .models import HostNotification


def _visible_notifications(user):
    """Notifications for hosts linked to this login (or everything for superusers). FR-6.3."""
    linked = TeamMember.objects.filter(linked_user=user, is_active=True)
    qs = HostNotification.objects.select_related("host", "event")
    if user.is_superuser or user.is_staff and not linked.exists():
        return qs
    return qs.filter(host__in=linked) if linked.exists() else qs.none()


@staff_member_required
def notification_list(request):
    notifications = _visible_notifications(user=request.user)
    return render(request, "notifications/notification_list.html",
                  {"notifications": notifications, "unread": notifications.filter(is_read=False).count()})


@staff_member_required
@require_POST
def notification_mark_read(request, pk):
    notification = get_object_or_404(HostNotification, pk=pk)
    notification.is_read = True
    notification.save(update_fields=["is_read"])
    if request.headers.get("HX-Request"):
        return render(request, "notifications/partials/notification_rows.html",
                      {"notifications": _visible_notifications(user=request.user)})
    return redirect("notifications:list")


@staff_member_required
@require_POST
def notification_mark_all_read(request):
    _visible_notifications(user=request.user).filter(is_read=False).update(is_read=True)
    if request.headers.get("HX-Request"):
        return render(request, "notifications/partials/notification_rows.html",
                      {"notifications": _visible_notifications(user=request.user)})
    return redirect("notifications:list")
