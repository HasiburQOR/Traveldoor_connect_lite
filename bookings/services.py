"""
Booking services — the transactional core of the platform.

Implements the concurrency-safe booking primitive (FR-4.5 / NFR-5),
visitor cancellation that reopens the slot (FR-7.2), and reschedule-as-
one-transaction (FR-7.3).

Concurrency strategy
--------------------
* capacity == 1 (default): the slot flip is a single atomic conditional
  UPDATE (``UPDATE ... WHERE status='available'``) — two racing requests
  can never both succeed, on any database backend.
* capacity  > 1: the slot row is locked with SELECT ... FOR UPDATE
  (real locking on PostgreSQL; on SQLite writes are serialised anyway)
  inside the same transaction before counting confirmed bookings.
"""
import datetime

from django.db import transaction
from django.utils import timezone

from events.models import Slot

from .models import Booking


class BookingError(Exception):
    """Raised when a slot cannot be booked/rescheduled (never a crash)."""

    def __init__(self, message, code="unavailable"):
        super().__init__(message)
        self.message = message
        self.code = code


def _atomic_reserve(slot_id):
    """Reserve one seat on a slot. Returns the refreshed Slot.

    Raises BookingError when the slot is not bookable.
    """
    with transaction.atomic():
        if slot_id is None:
            raise BookingError("No slot selected.", "no_slot")
        slot = Slot.objects.select_for_update().filter(pk=slot_id).first()
        if slot is None:
            raise BookingError("The selected slot no longer exists.", "gone")
        if slot.capacity <= 1:
            # Race-safe single UPDATE: only succeeds while still available.
            updated = Slot.objects.filter(
                pk=slot.pk, status=Slot.STATUS_AVAILABLE
            ).update(status=Slot.STATUS_BOOKED)
            if not updated:
                raise BookingError(
                    "Sorry — that slot was just booked by someone else. Please pick another slot.",
                    "double_booked",
                )
        else:
            confirmed = slot.bookings.filter(status=Booking.STATUS_CONFIRMED).count()
            if confirmed >= slot.capacity:
                raise BookingError(
                    "Sorry — that slot is fully booked. Please pick another slot.", "full"
                )
            if confirmed + 1 >= slot.capacity:
                Slot.objects.filter(pk=slot.pk).update(status=Slot.STATUS_BOOKED)
        slot.refresh_from_db()
        return slot


def release_slot(slot):
    """Return the slot to Available after a cancellation/removal (FR-7.2)."""
    if slot is None:
        return
    with transaction.atomic():
        slot = Slot.objects.select_for_update().get(pk=slot.pk)
        confirmed = slot.bookings.filter(status=Booking.STATUS_CONFIRMED).count()
        if confirmed < slot.capacity:
            slot.status = Slot.STATUS_AVAILABLE
            slot.save(update_fields=["status", "updated_at"])


def create_booking(*, slot, visitor_name, visitor_email, visitor_phone="", company="", notes="", rescheduled_from=None, actor=None):
    """Create a confirmed booking atomically. Sends no emails (caller does)."""
    reserved = _atomic_reserve(slot.pk if hasattr(slot, "pk") else slot)
    booking = Booking.objects.create(
        event=reserved.event,
        slot=reserved,
        visitor_name=visitor_name,
        visitor_email=visitor_email,
        visitor_phone=visitor_phone or "",
        company=company or "",
        notes=notes or "",
        rescheduled_from=rescheduled_from,
    )
    return booking


def cancel_booking(booking, *, cancelled_by_admin=False, reason=""):
    """Cancel a booking and immediately reopen its slot (FR-7.2)."""
    if booking.status == Booking.STATUS_CANCELLED:
        return booking
    booking.status = Booking.STATUS_CANCELLED
    booking.cancelled_at = timezone.now()
    booking.needs_attention = False
    booking.save(update_fields=["status", "cancelled_at", "needs_attention", "updated_at"])
    release_slot(booking.slot)
    return booking


def reschedule_booking(booking, *, new_slot):
    """Cancel old slot (reopens it) + book new slot as ONE transaction (FR-7.3).

    Implementation note: we perform the reserve step first; only if the new
    slot could be secured do we release the old one — all inside a single
    outer transaction so callers never see a half-reschedule.
    """
    with transaction.atomic():
        reserved_new = _atomic_reserve(new_slot.pk)
        # Release old booking + slot.
        booking.status = Booking.STATUS_CANCELLED
        booking.cancelled_at = timezone.now()
        booking.save(update_fields=["status", "cancelled_at", "updated_at"])
        release_slot(booking.slot)
        new_booking = Booking.objects.create(
            event=reserved_new.event,
            slot=reserved_new,
            visitor_name=booking.visitor_name,
            visitor_email=booking.visitor_email,
            visitor_phone=booking.visitor_phone,
            company=booking.company,
            notes=booking.notes,
            rescheduled_from=booking,
        )
        return new_booking
