"""
Audit log models (SRS FR-9).

Every create / update / delete (plus key state changes such as publish,
close, cancel and reschedule) performed through the application is recorded
with: which admin performed the action, the action, the entity type/id,
a human-readable summary, and the timestamp (FR-9.1, FR-9.2).

Entries are append-only: no view or admin action can edit or delete them
(FR-9.4). Writes are lightweight single-INSERT operations kept inside the
same transaction as the primary action (NFR-6).
"""
from django.conf import settings
from django.db import models


class AuditLogEntry(models.Model):
    ACTION_CREATE = "create"
    ACTION_UPDATE = "update"
    ACTION_DELETE = "delete"
    ACTION_PUBLISH = "publish"
    ACTION_CLOSE = "close"
    ACTION_CANCEL = "cancel"
    ACTION_RESCHEDULE = "reschedule"
    ACTION_REACTIVATE = "reactivate"
    ACTION_CHOICES = [
        (ACTION_CREATE, "Create"),
        (ACTION_UPDATE, "Update"),
        (ACTION_DELETE, "Delete"),
        (ACTION_PUBLISH, "Publish"),
        (ACTION_CLOSE, "Close"),
        (ACTION_CANCEL, "Cancel"),
        (ACTION_RESCHEDULE, "Reschedule"),
        (ACTION_REACTIVATE, "Re-activate"),
    ]

    ENTITY_EVENT = "Event"
    ENTITY_TEAM_MEMBER = "TeamMember"
    ENTITY_SLOT = "Slot"
    ENTITY_BOOKING = "Booking"
    ENTITY_ADMIN_ACCOUNT = "AdminAccount"
    ENTITY_EVENT_CHOICES = [
        (ENTITY_EVENT, "Event"),
        (ENTITY_TEAM_MEMBER, "Team Member"),
        (ENTITY_SLOT, "Slot"),
        (ENTITY_BOOKING, "Booking"),
        (ENTITY_ADMIN_ACCOUNT, "Admin Account"),
    ]

    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="audit_entries",
        help_text="Admin who performed the action (blank for system jobs).",
    )
    action = models.CharField(max_length=20, choices=ACTION_CHOICES)
    entity_type = models.CharField(max_length=40, choices=ENTITY_EVENT_CHOICES)
    entity_id = models.PositiveIntegerField()
    entity_repr = models.CharField(
        max_length=200, blank=True, default="", help_text="Human label at time of action."
    )
    details = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Audit log entry"
        verbose_name_plural = "Audit log entries"
        indexes = [
            models.Index(fields=["entity_type", "entity_id"]),
            models.Index(fields=["actor", "created_at"]),
        ]

    def __str__(self):
        return f"{self.get_action_display()} {self.entity_type}#{self.entity_id} by {self.actor or 'system'}"


def log_action(actor, action, entity, details="", entity_repr=None, entity_type=None):
    """Append an audit entry. Minimal overhead — one INSERT (NFR-6).

    Never raises: an audit problem must not break the user's action.
    Pass ``entity_type`` explicitly when the model class name differs from
    the canonical audit label (e.g. User -> "AdminAccount").
    """
    try:
        return AuditLogEntry.objects.create(
            actor=actor if (actor and getattr(actor, "is_authenticated", False)) else None,
            action=action,
            entity_type=entity_type or entity.__class__.__name__,
            entity_id=entity.pk,
            entity_repr=entity_repr if entity_repr is not None else str(entity)[:200],
            details=details,
        )
    except Exception:  # pragma: no cover - defensive only
        return None
