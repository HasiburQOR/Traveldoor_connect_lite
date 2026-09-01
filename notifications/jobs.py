"""
Background jobs (SRS FR-1.4, FR-5.2, FR-6; NFR-4).

These plain functions are invoked periodically by the `runscheduler`
management command (APScheduler) so reminders and notifications fire
reliably even when no admin browser is open (NFR-4) — they can also be
called from Celery/Django-Q unchanged in a bigger deployment.
"""
import datetime
import logging

from django.utils import timezone

from audit.models import AuditLogEntry, log_action
from bookings.models import Booking
from events.models import Event, Slot

from .emails import retry_failed_emails, send_reminder
from .models import HostNotification, NotificationLog, SchedulerHeartbeat

logger = logging.getLogger(__name__)


def close_expired_events():
    """FR-1.4 — auto-close public booking access once duration elapsed."""
    closed = 0
    for event in Event.objects.filter(status=Event.STATUS_LIVE):
        if event.is_expired:
            event.status = Event.STATUS_CLOSED
            event.save(update_fields=["status", "updated_at"])
            log_action(None, AuditLogEntry.ACTION_CLOSE, event, "Auto-closed: active duration elapsed.")
            closed += 1
    if closed:
        logger.info("Auto-closed %d expired event(s).", closed)
    return closed


def process_due_reminders():
    """FR-5.2 — send each event's reminder emails at its configured hours.

    Deduplicated via NotificationLog.meta (`reminder-<hours>`), so each
    visitor receives at most one reminder per configured offset per booking.
    """
    now = timezone.now()
    sent = 0
    upcoming = (
        Booking.objects.filter(status=Booking.STATUS_CONFIRMED)
        .select_related("slot__host", "slot__event", "event")
        .filter(slot__date__gte=now.date() - datetime.timedelta(days=1))
    )
    for booking in upcoming:
        meeting = booking.meeting_datetime
        if meeting <= now:
            continue
        for hours in booking.event.reminder_hours():
            due_at = meeting - datetime.timedelta(hours=hours)
            if now < due_at:
                continue  # not due yet
            already = NotificationLog.objects.filter(
                booking=booking,
                type=NotificationLog.TYPE_REMINDER,
                meta=f"reminder-{hours}",
                status=NotificationLog.STATUS_SENT,
            ).exists()
            if already:
                continue
            log = send_reminder(booking, hours)
            sent += 1 if log.status == NotificationLog.STATUS_SENT else 0
    if sent:
        logger.info("Sent %d reminder email(s).", sent)
    return sent


def _venue_line(slot):
    parts = [p for p in (slot.effective_venue, slot.effective_hall, slot.effective_table) if p]
    return " — ".join(parts)


def create_inapp_notifications():
    """FR-6 — raise host/admin in-app notifications ahead of meetings.

    * `upcoming`: at event.inapp_lead_minutes before the meeting (FR-6.3),
      with a one-click join link for online meetings (FR-6.2).
    * `summary`: a same-day digest per host (when enabled) listing that
      host's remaining meetings today.
    """
    now = timezone.now()
    created = 0
    confirmed = list(
        Booking.objects.filter(status=Booking.STATUS_CONFIRMED, needs_attention=False)
        .select_related("slot__host", "slot__event", "event")
        .filter(slot__date=now.date())
        .order_by("slot__start_time")
    )
    for booking in confirmed:
        slot = booking.slot
        meeting = slot.start_datetime
        lead = booking.event.inapp_lead_minutes or 30
        if meeting > now and meeting - datetime.timedelta(minutes=lead) <= now:
            if not HostNotification.objects.filter(
                booking=booking, kind=HostNotification.KIND_UPCOMING
            ).exists():
                HostNotification.objects.create(
                    host=slot.host,
                    booking=booking,
                    event=booking.event,
                    kind=HostNotification.KIND_UPCOMING,
                    title=f"Meeting with {booking.visitor_name} at {meeting.strftime('%H:%M')}",
                    message=(
                        f"{booking.visitor_name}"
                        + (f" ({booking.company})" if booking.company else "")
                        + f" — {booking.event.name}. Starts in "
                        + f"{max(int((meeting - now).total_seconds() // 60), 0)} min."
                    ),
                    meeting_datetime=meeting,
                    join_url=slot.effective_meeting_link if slot.mode == Slot.MODE_ONLINE else "",
                    venue_details=_venue_line(slot) if slot.mode == Slot.MODE_OFFLINE else "",
                )
                created += 1

    # Same-day summary per (host, event) — FR-6.3.
    summaries_done = set()
    for booking in confirmed:
        event = booking.event
        if not event.same_day_summary:
            continue
        key = (booking.slot.host_id, event.pk)
        if key in summaries_done:
            continue
        if HostNotification.objects.filter(
            host_id=booking.slot.host_id, event=event,
            kind=HostNotification.KIND_SUMMARY, created_at__date=now.date(),
        ).exists():
            summaries_done.add(key)
            continue
        host_bookings = [b for b in confirmed if b.slot.host_id == booking.slot.host_id]
        lines = [
            f"{b.slot.start_time.strftime('%H:%M')} — {b.visitor_name}"
            + (f" ({b.company})" if b.company else "")
            for b in host_bookings
        ]
        HostNotification.objects.create(
            host=booking.slot.host,
            booking=None,
            event=event,
            kind=HostNotification.KIND_SUMMARY,
            title=f"Today: {len(host_bookings)} meeting(s) — {event.name}",
            message="\n".join(lines),
            meeting_datetime=host_bookings[0].slot.start_datetime,
        )
        created += 1
        summaries_done.add(key)
    if created:
        logger.info("Created %d in-app notification(s).", created)
    return created


def run_all_jobs():
    """One scheduler tick — used by `runscheduler` and by tests."""
    result = {
        "closed_events": close_expired_events(),
        "reminders_sent": process_due_reminders(),
        "inapp_created": create_inapp_notifications(),
        "emails_retried": retry_failed_emails(),
    }
    # Record the tick so the dashboard can tell whether reminders are running.
    SchedulerHeartbeat.stamp(", ".join(f"{k}={v}" for k, v in result.items()))
    return result

