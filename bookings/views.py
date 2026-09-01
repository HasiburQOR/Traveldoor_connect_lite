"""
Booking views.

Three audiences:
  * Public visitors — roster, open slots, atomic booking (FR-4).
  * Visitors with a manage-token link — view / reschedule / cancel (FR-4.6).
  * Admins — full booking CRUD on visitors' behalf (FR-7.1, BR-12).
"""
import csv
import datetime

from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.db.models import Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from core.pagination import paginate, querystring_without_page

from audit.models import AuditLogEntry, log_action
from events.models import Event, Slot, TeamMember
from notifications.emails import (
    send_booking_confirmation,
    send_cancellation_to_host,
    send_cancellation_to_visitor,
)
from notifications.models import NotificationLog

from .forms import AdminBookingForm, VisitorBookingForm
from .models import Booking
from .services import BookingError, cancel_booking, create_booking, release_slot, reschedule_booking


# ---------------------------------------------------------------------------
# Public visitor flow (FR-4) — no login required
# ---------------------------------------------------------------------------
def public_event(request, slug):
    """FR-4.2 — landing page with the host roster."""
    event = get_object_or_404(Event, public_slug=slug)
    if not event.is_publicly_open:  # FR-1.4 auto-close
        return render(request, "public/event_closed.html", {"event": event})
    hosts = event.team_members.filter(is_active=True).prefetch_related("slots")
    return render(request, "public/event_page.html", {"event": event, "hosts": hosts})


def public_host(request, slug, pk):
    """FR-4.3 — a host's open slots + the booking form."""
    event = get_object_or_404(Event, public_slug=slug)
    if not event.is_publicly_open:
        return render(request, "public/event_closed.html", {"event": event})
    host = get_object_or_404(TeamMember, pk=pk, event=event, is_active=True)
    # Show taken/unavailable times too (struck through) so the visitor can see
    # the shape of the day rather than a mysteriously sparse list.
    slots = Slot.visible_for_host(host)
    form = VisitorBookingForm(event=event)
    return render(request, "public/host_page.html",
                  {"event": event, "host": host, "slots": slots, "form": form})


def public_book(request, slug, slot_pk):
    """FR-4.4 / FR-4.5 — submit booking; slot reservation is atomic.

    htmx swaps just the ticket stub. Without htmx (JS disabled or blocked, or
    the URL opened directly) the same stub is rendered inside the full ticket
    page instead, so the booking flow degrades gracefully rather than serving
    a bare unstyled fragment.
    """
    event = get_object_or_404(Event, public_slug=slug)
    if not event.is_publicly_open:
        return render(request, "public/event_closed.html", {"event": event})
    slot = get_object_or_404(Slot, pk=slot_pk, event=event)
    host = slot.host
    is_htmx = bool(request.headers.get("HX-Request"))

    def respond(stub_template, extra=None):
        context = {"event": event, "host": host, "slot": slot, "form": form}
        context.update(extra or {})
        if is_htmx:
            return render(request, stub_template, context)
        context.update({
            "slots": Slot.visible_for_host(host),
            "selected_slot": slot,
            "stub_template": stub_template,
        })
        return render(request, "public/host_page.html", context)

    if request.method == "POST":
        form = VisitorBookingForm(request.POST, event=event)
        if form.is_valid() and slot.is_publicly_bookable():
            try:
                booking = create_booking(
                    slot=slot,
                    visitor_name=form.cleaned_data["visitor_name"],
                    visitor_email=form.cleaned_data["visitor_email"],
                    visitor_phone=form.cleaned_data["visitor_phone"],
                    company=form.cleaned_data["company"],
                    notes=form.cleaned_data["notes"],
                )
            except BookingError as exc:
                form.add_error(None, exc.message)
            else:
                send_booking_confirmation(booking)  # FR-5.1 (with manage link)
                log_action(None, AuditLogEntry.ACTION_CREATE, booking,
                           details=f"Visitor {booking.visitor_email} booked {booking.slot}.")
                return respond("public/partials/booking_success.html", {"booking": booking})
        else:
            if not slot.is_publicly_bookable():
                form.add_error(None, "That slot is no longer available — please pick another slot below.")
    else:
        form = VisitorBookingForm(event=event)
    return respond("public/partials/booking_form.html")


# ---------------------------------------------------------------------------
# Visitor manage links (FR-4.6) — tokenised, no login required
# ---------------------------------------------------------------------------
def _manage_booking(token):
    return get_object_or_404(
        Booking.objects.select_related("slot", "slot__host", "event"),
        manage_token=token,
    )


def manage_view(request, token):
    """FR-4.6 — booking summary with cancel + reschedule options."""
    booking = _manage_booking(token)
    options = Slot.public_for_event(booking.event) if booking.slot else []
    return render(request, "manage/manage_view.html",
                  {"booking": booking, "options": options})
def manage_reschedule(request, token):
    """FR-4.6 — move to another open slot atomically."""
    booking = _manage_booking(token)
    if not booking.is_active:
        messages.error(request, "This booking is cancelled and can no longer be changed.")
        return redirect("bookings_manage:manage", token)
    if request.method == "POST":
        new_slot = Slot.objects.filter(
            pk=request.POST.get("slot"), event=booking.event
        ).select_related("host").first()
        try:
            new_booking = reschedule_booking(booking, new_slot=new_slot)
        except BookingError as exc:
            messages.error(request, exc.message)
            return redirect("bookings_manage:manage", token)
        log_action(None, AuditLogEntry.ACTION_RESCHEDULE, new_booking,
                   details=f"Visitor {booking.visitor_email} rescheduled via manage link.")
        send_booking_confirmation(new_booking, is_reschedule=True)  # FR-5.1
        send_cancellation_to_host(booking, reason="Visitor rescheduled to another slot.")
        messages.success(request, "Your meeting has been moved. A confirmation email is on its way.")
        return redirect("bookings_manage:manage", new_booking.manage_token)
    options = [s for s in Slot.public_for_event(booking.event) if s.pk != booking.slot_id]
    return render(request, "manage/reschedule.html", {"booking": booking, "options": options})


@require_POST
def manage_cancel(request, token):
    """FR-4.6 — cancel from the manage link; slot reopens immediately (FR-3.3)."""
    booking = _manage_booking(token)
    if booking.is_active:
        cancel_booking(booking, reason="Cancelled by visitor.")
        log_action(None, AuditLogEntry.ACTION_CANCEL, booking,
                   details=f"Visitor {booking.visitor_email} cancelled via manage link.")
        send_cancellation_to_host(booking, reason="Visitor cancelled their booking.")
        messages.success(request, "Your booking was cancelled — the slot is available again.")
    return redirect("bookings_manage:manage", token)


# ---------------------------------------------------------------------------
# Admin booking management (FR-7) — /panel side
# ---------------------------------------------------------------------------
def _filtered_bookings(request):
    """The booking queryset behind both the list and the CSV export.

    Sharing it is what makes "what you see is what you export" true — the
    export can never drift from the filters shown on screen.
    """
    qs = Booking.objects.select_related("slot", "slot__host", "event").order_by("-created_at")
    q = (request.GET.get("q") or "").strip()
    status = request.GET.get("status") or ""
    event_id = request.GET.get("event") or ""
    only_attention = request.GET.get("attention") == "1"

    if q:
        qs = qs.filter(
            Q(visitor_name__icontains=q)
            | Q(visitor_email__icontains=q)
            | Q(company__icontains=q)
        )
    if status:
        qs = qs.filter(status=status)
    if event_id.isdigit():
        qs = qs.filter(event_id=event_id)
    if only_attention:
        qs = qs.filter(needs_attention=True)
    return qs, {"q": q, "status": status, "event": event_id, "only_attention": only_attention}


@staff_member_required
def booking_list(request):
    """FR-7.4 — searchable booking overview incl. needs-attention filter."""
    qs, filters = _filtered_bookings(request)
    page = paginate(request, qs)
    return render(request, "bookings/booking_list.html", {
        "page": page,
        "bookings": page.object_list,
        "qs": querystring_without_page(request),
        "statuses": Booking.STATUS_CHOICES,
        "events": Event.objects.order_by("-start_date"),
        **filters,
    })


@staff_member_required
def booking_export(request):
    """FR-7.4 — CSV of exactly the rows the current filters produce."""
    qs, _ = _filtered_bookings(request)
    stamp = timezone.localtime().strftime("%Y%m%d-%H%M")
    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = f'attachment; filename="bookings-{stamp}.csv"'
    writer = csv.writer(response)
    writer.writerow([
        "Date", "Time", "Visitor", "Email", "Phone", "Company",
        "Host", "Event", "Format", "Status", "Booked at", "Notes",
    ])
    for b in qs:
        writer.writerow([
            b.slot.date.isoformat() if b.slot else "",
            b.slot.start_time.strftime("%H:%M") if b.slot else "",
            b.visitor_name,
            b.visitor_email,
            b.visitor_phone,
            b.company,
            b.slot.host.name if b.slot else "",
            b.event.name,
            "Video call" if b.event.is_online else "In person",
            b.get_status_display(),
            timezone.localtime(b.created_at).strftime("%Y-%m-%d %H:%M"),
            b.notes,
        ])
    # Exports carry visitor personal data, so record who took one. log_action()
    # needs a concrete entity; an export spans many, so write the row directly.
    AuditLogEntry.objects.create(
        actor=request.user if request.user.is_authenticated else None,
        action=AuditLogEntry.ACTION_UPDATE,
        entity_type=AuditLogEntry.ENTITY_BOOKING,
        entity_id=0,
        entity_repr="Booking CSV export",
        details=f"Exported {qs.count()} booking(s) to CSV.",
    )
    return response


def _reminder_states(booking):
    """Per-offset reminder status: sent when, or when it falls due.

    Answers "did the visitor actually get their reminder?" without digging
    through the notification log.
    """
    sent = {
        log.meta: log
        for log in NotificationLog.objects.filter(
            booking=booking,
            type=NotificationLog.TYPE_REMINDER,
            status=NotificationLog.STATUS_SENT,
        )
    }
    now = timezone.now()
    meeting = booking.meeting_datetime
    states = []
    for hours in booking.event.reminder_hours():
        log = sent.get(f"reminder-{hours}")
        due_at = meeting - datetime.timedelta(hours=hours)
        if log is not None:
            state, detail = "sent", log.sent_at or log.created_at
        elif not booking.is_active:
            state, detail = "skipped", None
        elif due_at <= now:
            state, detail = "overdue", due_at
        else:
            state, detail = "scheduled", due_at
        states.append({"hours": hours, "state": state, "at": detail})
    return states


@staff_member_required
def booking_detail(request, pk):
    booking = get_object_or_404(
        Booking.objects.select_related("slot", "slot__host", "event"), pk=pk
    )
    trail = AuditLogEntry.objects.filter(
        entity_type=AuditLogEntry.ENTITY_BOOKING, entity_id=booking.pk
    ).order_by("-created_at")[:50]
    emails = NotificationLog.objects.filter(booking=booking).order_by("-created_at")[:50]
    return render(request, "bookings/booking_detail.html", {
        "booking": booking,
        "trail": trail,
        "emails": emails,
        "reminders": _reminder_states(booking),
    })


@staff_member_required
@require_POST
def booking_resend_confirmation(request, pk):
    """Re-send the confirmation email — the most common admin request."""
    booking = get_object_or_404(
        Booking.objects.select_related("slot", "slot__host", "event"), pk=pk
    )
    log = send_booking_confirmation(booking)
    if log.status == NotificationLog.STATUS_SENT:
        log_action(request.user, AuditLogEntry.ACTION_UPDATE, booking,
                   details=f"Confirmation email re-sent to {booking.visitor_email}.")
        messages.success(request, f"Confirmation re-sent to {booking.visitor_email}.")
    else:
        messages.error(request, f"Could not send: {log.error or 'unknown error'}")
    return redirect("bookings_admin:admin_detail", booking.pk)


@staff_member_required
def booking_create(request):
    """FR-7.1 / BR-12 — admin books on a visitor's behalf (e.g. by phone)."""
    form = AdminBookingForm(request.POST or None)
    slot = None
    slot_id = request.POST.get("slot") or request.GET.get("slot")
    if slot_id:
        slot = Slot.objects.filter(pk=slot_id).select_related("host", "event").first()
    if request.method == "POST" and form.is_valid() and slot is not None:
        if not slot.is_publicly_bookable():
            form.add_error(None, "That slot is not bookable — pick another one.")
        else:
            try:
                booking = create_booking(
                    slot=slot,
                    visitor_name=form.cleaned_data["visitor_name"],
                    visitor_email=form.cleaned_data["visitor_email"],
                    visitor_phone=form.cleaned_data["visitor_phone"],
                    company=form.cleaned_data["company"],
                    notes=form.cleaned_data["notes"],
                )
            except BookingError as exc:
                form.add_error(None, exc.message)
            else:
                log_action(request.user, AuditLogEntry.ACTION_CREATE, booking,
                           details="Booking created by admin on visitor's behalf.")
                send_booking_confirmation(booking)
                messages.success(request, "Booking created; confirmation email sent.")
                return redirect("bookings_admin:admin_detail", booking.pk)
    events = Event.objects.filter(status=Event.STATUS_LIVE)
    slots = []
    if request.GET.get("event"):
        slots = Slot.public_for_event(Event.objects.get(pk=request.GET["event"]))
    return render(request, "bookings/booking_form.html",
                  {"form": form, "events": events, "slots": slots, "selected_slot": slot,
                   "title": "Book for a visitor"})


@staff_member_required
def booking_update(request, pk):
    """FR-7.1 — edit visitor details on an existing booking."""
    booking = get_object_or_404(Booking.objects.select_related("slot", "slot__host", "event"), pk=pk)
    form = AdminBookingForm(request.POST or None, instance=booking)
    if request.method == "POST" and form.is_valid():
        booking = form.save()
        log_action(request.user, AuditLogEntry.ACTION_UPDATE, booking, "Booking details edited by admin.")
        messages.success(request, "Booking updated.")
        return redirect("bookings_admin:admin_detail", booking.pk)
    return render(request, "bookings/booking_form.html",
                  {"form": form, "booking": booking, "title": f"Edit booking — {booking.visitor_name}"})


@staff_member_required
@require_POST
def booking_cancel(request, pk):
    """FR-7.2 — admin cancels; slot reopens; both parties notified."""
    booking = get_object_or_404(Booking.objects.select_related("slot", "slot__host"), pk=pk)
    reason = request.POST.get("reason", "Cancelled by organiser.")
    if booking.is_active:
        cancel_booking(booking, cancelled_by_admin=True, reason=reason)
        log_action(request.user, AuditLogEntry.ACTION_CANCEL, booking,
                   details=f"Admin cancelled booking. Reason: {reason}")
        send_cancellation_to_visitor(booking, reason=reason)
        send_cancellation_to_host(booking, reason=reason)
        messages.success(request, "Booking cancelled — both parties have been notified.")
    return redirect("bookings_admin:admin_detail", booking.pk)


@staff_member_required
def booking_reschedule(request, pk):
    """FR-7.3 — admin moves the booking to a new slot in one transaction."""
    booking = get_object_or_404(Booking.objects.select_related("slot", "slot__host", "event"), pk=pk)
    if request.method == "POST":
        new_slot = Slot.objects.filter(
            pk=request.POST.get("slot"), event=booking.event
        ).select_related("host").first()
        try:
            new_booking = reschedule_booking(booking, new_slot=new_slot)
        except BookingError as exc:
            messages.error(request, exc.message)
        else:
            log_action(request.user, AuditLogEntry.ACTION_RESCHEDULE, new_booking,
                       details=f"Admin moved booking to {new_booking.slot}.")
            send_booking_confirmation(new_booking, is_reschedule=True)
            send_cancellation_to_host(booking, reason="Organiser moved this booking to another slot.")
            messages.success(request, "Booking moved — visitor notified.")
            return redirect("bookings_admin:admin_detail", new_booking.pk)
        return redirect("bookings_admin:admin_detail", booking.pk)
    options = Slot.public_for_event(booking.event)
    return render(request, "bookings/booking_reschedule.html",
                  {"booking": booking, "options": options})


@staff_member_required
@require_POST
def booking_clear_flag(request, pk):
    """FR-2.3 follow-up — mark a needs-attention booking as handled."""
    booking = get_object_or_404(Booking, pk=pk)
    booking.needs_attention = False
    booking.save(update_fields=["needs_attention", "updated_at"])
    log_action(request.user, AuditLogEntry.ACTION_UPDATE, booking, "Attention flag cleared by admin.")
    if request.headers.get("HX-Request"):
        return render(request, "bookings/partials/booking_rows.html", {"b": booking})
    return redirect("bookings_admin:admin_list")
