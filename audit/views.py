"""Audit views — filterable, strictly read-only log (FR-9.2, FR-9.4)."""
from core.decorators import staff_member_required  # app login page, not the Django admin
from django.shortcuts import render

from core.pagination import paginate, querystring_without_page

from .models import AuditLogEntry


@staff_member_required
def audit_list(request):
    """FR-9.2 — filter by date range, admin, entity and action."""
    qs = AuditLogEntry.objects.select_related("actor").all()
    actor = request.GET.get("actor") or ""
    entity_type = request.GET.get("entity") or ""
    action = request.GET.get("action") or ""
    date_from = request.GET.get("from") or ""
    date_to = request.GET.get("to") or ""
    if actor:
        qs = qs.filter(actor_id=actor)
    if entity_type:
        qs = qs.filter(entity_type=entity_type)
    if action:
        qs = qs.filter(action=action)
    if date_from:
        qs = qs.filter(created_at__date__gte=date_from)
    if date_to:
        qs = qs.filter(created_at__date__lte=date_to)
    page = paginate(request, qs)
    return render(request, "audit/audit_list.html", {
        "entries": page.object_list,
        "page": page,
        "qs": querystring_without_page(request),
        "actors": AuditLogEntry.objects.exclude(actor=None)
                       .values_list("actor_id", "actor__username").distinct().order_by("actor__username"),
        "entity_types": AuditLogEntry.ENTITY_EVENT_CHOICES,
        "actions": AuditLogEntry.ACTION_CHOICES,
        "filters": {"actor": actor, "entity": entity_type, "action": action,
                    "from": date_from, "to": date_to},
    })
