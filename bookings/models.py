"""
Bookings app models (SRS section 5 data model + FR-7).

Booking — one visitor reservation on one slot, manageable without an
account through a unique manage_token link (FR-4.6).
"""
import uuid

from django.db import models

from events.models import Event, Slot


def generate_manage_token():
    """Unique token for the visitor's manage link (FR-4.6)."""
    return uuid.uuid4().hex


class Booking(models.Model):
    STATUS_CONFIRMED = "confirmed"
    STATUS_CANCELLED = "cancelled"
    STATUS_CHOICES = [
        (STATUS_CONFIRMED, "Confirmed"),
        (STATUS_CANCELLED, "Cancelled"),
    ]

    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="bookings")
    slot = models.ForeignKey(Slot, on_delete=models.PROTECT, related_name="bookings")
    visitor_name = models.CharField(max_length=200)
    visitor_email = models.EmailField()
    visitor_phone = models.CharField(max_length=40, blank=True, default="")
    company = models.CharField(max_length=200, blank=True, default="")
    notes = models.TextField(blank=True, default="", help_text="Optional message from the visitor.")
    manage_token = models.CharField(max_length=64, unique=True, default=generate_manage_token)
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default=STATUS_CONFIRMED)
    needs_attention = models.BooleanField(
        default=False,
        help_text="Flagged for admin attention (e.g. host was removed from the event) — FR-2.3.",
    )
    rescheduled_from = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL, related_name="rescheduled_to"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["slot__date", "slot__start_time"]

    def __str__(self):
        return f"{self.visitor_name} — {self.slot}"

    # ----- convenience ------------------------------------------------------
    @property
    def meeting_datetime(self):
        return self.slot.start_datetime

    @property
    def is_active(self):
        return self.status == self.STATUS_CONFIRMED

    def get_manage_url(self):
        from django.urls import reverse

        return reverse("bookings_manage:manage", args=[self.manage_token])
