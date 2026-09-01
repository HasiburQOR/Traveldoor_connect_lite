"""
Notifications app models (SRS section 5 data model + FR-5, FR-6).

NotificationLog   — every outbound email (confirmation / reminder /
                    cancellation / admin-account) with delivery status and
                    retry counter (NFR-3 retry/queueing).
HostNotification  — in-app notification for hosts/admins ahead of a
                    meeting (FR-6.1 — FR-6.3), with one-click join link.
"""
from django.conf import settings
from django.db import models

from bookings.models import Booking
from events.models import Event, TeamMember


class NotificationLog(models.Model):
    TYPE_CONFIRMATION = "confirmation"
    TYPE_REMINDER = "reminder"
    TYPE_CANCELLATION = "cancellation"
    TYPE_RESCHEDULE = "reschedule"
    TYPE_ADMIN_ACCOUNT = "admin_account"
    TYPE_ATTENTION = "attention"
    TYPE_CHOICES = [
        (TYPE_CONFIRMATION, "Confirmation"),
        (TYPE_REMINDER, "Reminder"),
        (TYPE_CANCELLATION, "Cancellation"),
        (TYPE_RESCHEDULE, "Reschedule"),
        (TYPE_ADMIN_ACCOUNT, "Admin account"),
        (TYPE_ATTENTION, "Attention"),
    ]

    CHANNEL_EMAIL = "email"
    CHANNEL_INAPP = "in-app"
    CHANNEL_CHOICES = [(CHANNEL_EMAIL, "Email"), (CHANNEL_INAPP, "In-app")]

    STATUS_SENT = "sent"
    STATUS_FAILED = "failed"
    STATUS_CHOICES = [(STATUS_SENT, "Sent"), (STATUS_FAILED, "Failed")]

    booking = models.ForeignKey(
        Booking, null=True, blank=True, on_delete=models.SET_NULL, related_name="email_logs"
    )
    type = models.CharField(max_length=20, choices=TYPE_CHOICES)
    channel = models.CharField(max_length=10, choices=CHANNEL_CHOICES, default=CHANNEL_EMAIL)
    recipient_email = models.EmailField()
    subject = models.CharField(max_length=250, blank=True, default="")
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=STATUS_SENT)
    error = models.TextField(blank=True, default="")
    meta = models.CharField(
        max_length=60, blank=True, default="", help_text="Dedup key, e.g. 'reminder-24'."
    )
    retry_count = models.PositiveIntegerField(default=0)
    sent_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Email / notification log"

    def __str__(self):
        return f"{self.get_type_display()} to {self.recipient_email} ({self.status})"


class HostNotification(models.Model):
    """In-app notification raised ahead of a meeting (FR-6)."""

    KIND_UPCOMING = "upcoming"
    KIND_SUMMARY = "summary"
    KIND_FLAGS = "flag"
    KIND_CHOICES = [
        (KIND_UPCOMING, "Upcoming meeting"),
        (KIND_SUMMARY, "Same-day summary"),
        (KIND_FLAGS, "Attention flag"),
    ]

    host = models.ForeignKey(TeamMember, on_delete=models.CASCADE, related_name="notifications")
    booking = models.ForeignKey(
        Booking, null=True, blank=True, on_delete=models.SET_NULL, related_name="host_notifications"
    )
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="host_notifications")
    kind = models.CharField(max_length=10, choices=KIND_CHOICES, default=KIND_UPCOMING)
    title = models.CharField(max_length=200)
    message = models.TextField(blank=True, default="")
    meeting_datetime = models.DateTimeField()
    join_url = models.URLField(blank=True, default="", help_text="One-click join link for online meetings (FR-6.2).")
    venue_details = models.CharField(max_length=300, blank=True, default="")
    is_read = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-meeting_datetime"]

    def __str__(self):
        return f"[{self.get_kind_display()}] {self.title}"


class SchedulerHeartbeat(models.Model):
    """Records that the background job loop is alive.

    Reminder emails only go out while `manage.py runscheduler` is running. When
    it isn't, nothing errors — reminders simply never send, which is invisible
    until a visitor misses a meeting. Stamping a row each tick lets the
    dashboard say so plainly.
    """

    singleton_id = models.PositiveSmallIntegerField(primary_key=True, default=1)
    last_run_at = models.DateTimeField(null=True, blank=True)
    last_result = models.CharField(max_length=200, blank=True, default="")

    class Meta:
        verbose_name = "Scheduler heartbeat"

    def __str__(self):
        return f"Scheduler last ran {self.last_run_at or 'never'}"

    @classmethod
    def stamp(cls, result=""):
        from django.utils import timezone as tz

        obj, _ = cls.objects.get_or_create(pk=1)
        obj.last_run_at = tz.now()
        obj.last_result = result[:200]
        obj.save(update_fields=["last_run_at", "last_result"])
        return obj

    @classmethod
    def current(cls):
        return cls.objects.filter(pk=1).first()
