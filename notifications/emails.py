"""
Transactional email service (SRS FR-5, BRD BR-09/BR-10, NFR-3).

Every email is rendered from branded templates (FR-5.4), delivered through
Django's email backend (the company's own SMTP account), and recorded in
NotificationLog with its delivery status. Failures are logged and retried
by the background scheduler (NFR-3) up to settings.EMAIL_MAX_RETRIES.
"""
import logging

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.utils import timezone

from core.runtime import (
    company_name, email_connection, email_is_sending, from_email,
    public_base_url, site_settings,
)

from .calendar_links import google_calendar_url, outlook_calendar_url
from .models import NotificationLog

logger = logging.getLogger(__name__)


def _deliver(*, template_base, context, subject, to, notification_type, booking=None, meta=""):
    """Render + send one templated email and log the attempt.

    Sender, branding and SMTP connection come from the admin-editable Settings
    screen, falling back to the environment when it hasn't been filled in.
    """
    site = site_settings()
    context.setdefault("company_name", company_name(site))
    context.setdefault("current_year", timezone.now().year)
    context.setdefault("base_url", public_base_url(site))
    text_body = render_to_string(f"emails/{template_base}.txt", context)
    html_body = render_to_string(f"emails/{template_base}.html", context)
    message = EmailMultiAlternatives(
        subject=subject, body=text_body, to=[to],
        from_email=from_email(site), connection=email_connection(site),
    )
    message.attach_alternative(html_body, "text/html")
    error = ""
    ok = False
    try:
        message.send(fail_silently=False)
        ok = True
    except Exception as exc:  # SMTP misconfiguration / outage — logged for retry
        error = str(exc)
        logger.error("Email delivery failed (%s -> %s): %s", notification_type, to, exc)
    return NotificationLog.objects.create(
        booking=booking,
        type=notification_type,
        channel=NotificationLog.CHANNEL_EMAIL,
        recipient_email=to,
        subject=subject,
        status=NotificationLog.STATUS_SENT if ok else NotificationLog.STATUS_FAILED,
        error=error,
        meta=meta,
        sent_at=timezone.now() if ok else None,
    )


def _fmt_slot(booking):
    slot = booking.slot
    tz = timezone.get_current_timezone()
    dt = slot.start_datetime.astimezone(tz)
    return dt.strftime("%A, %d %B %Y at %H:%M (%Z)"), f"{slot.duration_minutes} minutes"


def _meeting_context(booking):
    """Where-details for the email templates, branched on the event type.

    Online events never expose venue/hall/table; offline events never expose a
    join link or provider. Templates read these keys instead of digging into
    the slot, so the branch is decided in exactly one place.
    """
    slot = booking.slot
    event = booking.event
    online = event.is_online
    return {
        "is_online": online,
        "join_link": slot.effective_meeting_link if online else "",
        "provider": slot.effective_provider_display if online else "",
        "venue": "" if online else slot.effective_venue,
        "hall_name": "" if online else slot.effective_hall,
        "table_name": "" if online else slot.effective_table,
        "google_calendar_url": google_calendar_url(booking),
        "outlook_calendar_url": outlook_calendar_url(booking),
    }


def send_booking_confirmation(booking, *, is_reschedule=False):
    """FR-5.1 — immediate confirmation after booking (or reschedule)."""
    when, duration = _fmt_slot(booking)
    slot = booking.slot
    context = {
        "booking": booking,
        "slot": slot,
        "host": slot.host,
        "event": booking.event,
        "when": when,
        "duration": duration,
        "manage_url": booking.get_manage_url(),
        **_meeting_context(booking),
    }
    ntype = NotificationLog.TYPE_RESCHEDULE if is_reschedule else NotificationLog.TYPE_CONFIRMATION
    template = "reschedule" if is_reschedule else "confirmation"
    subject = (
        f"Updated: your meeting with {slot.host.name} — {booking.event.name}"
        if is_reschedule
        else f"Confirmed: your meeting with {slot.host.name} — {booking.event.name}"
    )
    return _deliver(
        template_base=template,
        context=context,
        subject=subject,
        to=booking.visitor_email,
        notification_type=ntype,
        booking=booking,
        meta="reschedule" if is_reschedule else "confirmation",
    )


def send_booking_confirmation_to_host(booking, *, is_reschedule=False):
    """New (or moved) booking notice to the HOST — with the join link.

    Visitors get the confirmation email with their manage link; the host gets
    the same meeting details (join link for online, venue for offline) so both
    parties always know where the meeting happens.
    """
    when, duration = _fmt_slot(booking)
    slot = booking.slot
    context = {
        "booking": booking,
        "slot": slot,
        "host": slot.host,
        "event": booking.event,
        "when": when,
        "duration": duration,
        "is_reschedule": is_reschedule,
        "google_calendar_url": google_calendar_url(booking),
        "outlook_calendar_url": outlook_calendar_url(booking),
        **_meeting_context(booking),
    }
    ntype = NotificationLog.TYPE_RESCHEDULE if is_reschedule else NotificationLog.TYPE_CONFIRMATION
    subject = (
        f"Moved: your meeting with {booking.visitor_name} — {booking.event.name}"
        if is_reschedule
        else f"New booking: {booking.visitor_name} — {when}"
    )
    return _deliver(
        template_base="host_confirmation",
        context=context,
        subject=subject,
        to=slot.host.email,
        notification_type=ntype,
        booking=booking,
        meta="host-reschedule" if is_reschedule else "host-confirmation",
    )


def send_reminder(booking, hours_before):
    """FR-5.2 — reminder email N hours before the meeting."""
    when, duration = _fmt_slot(booking)
    slot = booking.slot
    context = {
        "booking": booking,
        "slot": slot,
        "host": slot.host,
        "event": booking.event,
        "when": when,
        "duration": duration,
        "hours_before": hours_before,
        "manage_url": booking.get_manage_url(),
        **_meeting_context(booking),
    }
    return _deliver(
        template_base="reminder",
        context=context,
        subject=f"Reminder: meeting with {slot.host.name} on {when}",
        to=booking.visitor_email,
        notification_type=NotificationLog.TYPE_REMINDER,
        booking=booking,
        meta=f"reminder-{hours_before}",
    )




def send_cancellation_to_visitor(booking, *, reason="", actor_label="the organiser"):
    """FR-5.3 — cancellation notice to the visitor (admin action / slot delete)."""
    slot = booking.slot
    when, _ = _fmt_slot(booking)
    context = {
        "booking": booking,
        "slot": slot,
        "host": slot.host,
        "event": booking.event,
        "when": when,
        "reason": reason,
        "actor_label": actor_label,
        **_meeting_context(booking),
    }
    return _deliver(
        template_base="cancellation_visitor",
        context=context,
        subject=f"Cancelled: your meeting with {slot.host.name} — {booking.event.name}",
        to=booking.visitor_email,
        notification_type=NotificationLog.TYPE_CANCELLATION,
        booking=booking,
        meta="cancellation-visitor",
    )


def send_cancellation_to_host(booking, *, reason=""):
    """FR-5.3 — cancellation notice to the host (visitor cancelled)."""
    slot = booking.slot
    when, _ = _fmt_slot(booking)
    context = {
        "booking": booking,
        "slot": slot,
        "host": slot.host,
        "event": booking.event,
        "when": when,
        "reason": reason,
        **_meeting_context(booking),
    }
    return _deliver(
        template_base="cancellation_host",
        context=context,
        subject=f"Cancellation: {booking.visitor_name} cancelled the meeting on {when}",
        to=slot.host.email,
        notification_type=NotificationLog.TYPE_CANCELLATION,
        booking=booking,
        meta="cancellation-host",
    )


def send_admin_account_email(user, raw_password):
    """SRS 1.2 — welcome/credentials email for a newly created admin account."""
    context = {
        "user": user,
        "raw_password": raw_password,
        "login_path": "/accounts/login/",
    }
    return _deliver(
        template_base="admin_welcome",
        context=context,
        subject=f"Your {company_name()} Connect admin account",
        to=user.email,
        notification_type=NotificationLog.TYPE_ADMIN_ACCOUNT,
        meta="admin-welcome",
    )


def send_attention_email(booking, *, reason=""):
    """FR-2.3 — notice to a visitor whose booking needs admin attention."""
    slot = booking.slot
    when, _ = _fmt_slot(booking)
    context = {
        "booking": booking,
        "slot": slot,
        "host": slot.host,
        "event": booking.event,
        "when": when,
        "reason": reason,
        "manage_url": booking.get_manage_url(),
        **_meeting_context(booking),
    }
    return _deliver(
        template_base="attention",
        context=context,
        subject=f"Action needed: your meeting for {booking.event.name} requires rearranging",
        to=booking.visitor_email,
        notification_type=NotificationLog.TYPE_ATTENTION,
        booking=booking,
        meta="attention",
    )


def _meta_hours(meta):
    try:
        return int(str(meta).split("-")[-1])
    except (TypeError, ValueError):
        return None


def retry_failed_emails():
    """NFR-3 — retry failed email deliveries.

    Re-renders from the original template using the stored booking where
    possible; context-less types (admin welcome) are abandoned after the
    retry limit without a re-send.
    """
    limit = getattr(settings, "EMAIL_MAX_RETRIES", 3)
    candidates = NotificationLog.objects.filter(
        status=NotificationLog.STATUS_FAILED, retry_count__lt=limit
    ).select_related("booking__slot", "booking__event")[:50]
    for log in candidates:
        booking = log.booking
        result = None
        if booking is not None:
            if str(log.meta or "").startswith("host-"):
                # Host-side notice — retry with the host variant so the
                # visitor is never spammed by a host-delivery failure.
                result = send_booking_confirmation_to_host(
                    booking, is_reschedule=(log.meta == "host-reschedule")
                )
            elif log.type == NotificationLog.TYPE_CONFIRMATION:
                result = send_booking_confirmation(booking)
            elif log.type == NotificationLog.TYPE_RESCHEDULE:
                result = send_booking_confirmation(booking, is_reschedule=True)
            elif log.type == NotificationLog.TYPE_REMINDER:
                hours = _meta_hours(log.meta)
                result = send_reminder(booking, hours) if hours else None
            elif log.type == NotificationLog.TYPE_CANCELLATION:
                result = send_cancellation_to_visitor(booking, reason="retry delivery")
        log.retry_count += 1
        if result is not None and result.status == NotificationLog.STATUS_SENT:
            log.status = NotificationLog.STATUS_SENT
            log.sent_at = result.sent_at
            log.error = ""
            log.save(update_fields=["status", "sent_at", "error", "retry_count"])
        else:
            if result is not None and result.error:
                log.error = result.error[:500]
            if log.retry_count >= limit:
                log.error = (log.error + " | abandoned after max retries").strip(" |")
            log.save(update_fields=["retry_count", "error"])


# ---------------------------------------------------------------------------
# Delivery diagnostics
# ---------------------------------------------------------------------------
def email_health():
    """Is outbound email actually going to reach anyone?

    Two defaults silently break delivery in a fresh deployment: the console
    email backend (mail is printed, never sent) and a localhost
    PUBLIC_BASE_URL (every manage link in every email points at the admin's own
    machine). Both look like "the emails don't work".
    """
    site = site_settings()
    sending = email_is_sending(site)
    backend = (
        f"SMTP via {site.email_host}" if site.smtp_configured
        else getattr(settings, "EMAIL_BACKEND", "")
    )
    base_url = public_base_url(site)
    local_base = any(host in base_url for host in ("127.0.0.1", "localhost"))
    failed = NotificationLog.objects.filter(status=NotificationLog.STATUS_FAILED).count()
    return {
        "sending": sending,
        "backend": backend,
        "from_email": from_email(site),
        "configured_in_ui": site.smtp_configured,
        "base_url": base_url,
        "base_url_is_local": local_base,
        "failed": failed,
        "ok": sending and not local_base and not failed,
    }


def send_test_email(to):
    """Send a throwaway email so misconfiguration surfaces in one click.

    Returns (ok, message). The SMTP error text is passed straight through —
    that string is usually the whole diagnosis.
    """
    health = email_health()
    site = site_settings()
    subject = f"{company_name(site)} Connect — test email"
    body = "\n".join([
        "This is a test from TravelDoor Connect.",
        "",
        f"Backend: {health['backend']}",
        f"Public base URL: {health['base_url']}",
        "",
        f"From address: {health['from_email']}",
        "",
        "If you received this, visitor confirmations and reminders can be delivered too.",
    ])
    message = EmailMultiAlternatives(
        subject=subject, body=body, to=[to],
        from_email=from_email(site), connection=email_connection(site),
    )
    try:
        message.send(fail_silently=False)
    except Exception as exc:
        logger.error("Test email to %s failed: %s", to, exc)
        return False, str(exc)
    if not health["sending"]:
        return True, (
            "Written to the console, not sent. Set EMAIL_BACKEND=smtp (plus the "
            "EMAIL_* values) to deliver real mail."
        )
    return True, f"Sent to {to}."
