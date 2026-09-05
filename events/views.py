"""
Admin views for events, team members and slots (FR-1, FR-2, FR-3).

All views require a staff login (FR-8.1). Mutating views write an audit
entry (FR-9.1) and HTMX requests swap in refreshed partials (NFR-2).
"""
import datetime

from django.contrib import messages
from core.decorators import staff_member_required  # app login page, not the Django admin
from django.db import IntegrityError, transaction
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from audit.models import AuditLogEntry, log_action
from bookings.models import Booking
from core.runtime import public_base_url
from notifications.emails import send_attention_email
from notifications.jobs import close_expired_events
from notifications.models import HostNotification

from .forms import EventForm, HostPickForm, PersonForm, SlotForm, TeamMemberForm
from .forms_bulk import SlotBuilderForm
from .models import Event, EventDay, Person, Slot, TeamMember


def _team_section(request, event, *, form=None, member=None):
    """Render the whole team section, optionally with an open form inside it.

    The inline forms swap this section with ``outerHTML``, so a rejected form
    must come back *inside* the section — returning the bare form would delete
    the section (table and all) and with it the swap target, leaving the form
    orphaned and every later submit a no-op.
    """
    members = event.team_members.prefetch_related("slots")
    return render(request, "events/partials/team_section.html",
                  {"event": event, "members": members, "form": form, "member": member})


def _slot_section(request, event, *, form=None, slot=None, builder_form=None, host_count=None,
                  selected_date=None):
    """Render the whole slot section, optionally with an open form inside it.

    Same swap-target rule as ``_team_section``. ``selected_date`` narrows the
    table to one day (the calendar chip the admin clicked).
    """
    slots = event.slots.select_related("host").prefetch_related("bookings")
    if selected_date is not None:
        slots = [s for s in slots if s.date == selected_date]
    return render(request, "events/partials/slot_section.html",
                  {"event": event, "slots": slots, "form": form, "slot": slot,
                   "builder_form": builder_form, "host_count": host_count,
                   "selected_date": selected_date})


def _booking_section(request, event):
    bookings = (
        event.bookings.select_related("slot", "slot__host").order_by("-created_at")
    )
    return render(
        request,
        "events/partials/booking_section.html",
        {"event": event, "bookings": bookings, "available_slots": [
            s for s in event.slots.select_related("host") if s.is_publicly_bookable()
        ]},
    )


# ---------------------------------------------------------------------------
# Events (FR-1)
# ---------------------------------------------------------------------------
@staff_member_required
def event_list(request):
    close_expired_events()  # opportunistic FR-1.4 in addition to the scheduler
    # Annotate the counts the list shows, so the table is one query rather than
    # three per row.
    events = Event.objects.annotate(
        host_count=Count("team_members", distinct=True),
        slot_count=Count("slots", distinct=True),
        booking_count=Count(
            "bookings", distinct=True,
            filter=Q(bookings__status=Booking.STATUS_CONFIRMED),
        ),
    )
    return render(request, "events/event_list.html",
                  {"events": events, "base_url": public_base_url()})


@staff_member_required
def event_create(request):
    """Type first, then the form built for that type.

    Event type decides which fields even exist, so it is chosen up front on a
    small chooser screen rather than toggled inside the form.
    """
    event_type = request.POST.get("event_type") or request.GET.get("type")
    if event_type not in (Event.TYPE_ONLINE, Event.TYPE_OFFLINE):
        return render(request, "events/event_type_choice.html",
                      {"types": Event.TYPE_CHOICES, "title": "New event"})
    form = EventForm(request.POST or None, event_type=event_type)
    if request.method == "POST" and form.is_valid():
        event = form.save()
        log_action(request.user, AuditLogEntry.ACTION_CREATE, event,
                   f"Event created ({event.get_event_type_display()}).")
        messages.success(request, f"Event “{event.name}” created.")
        return redirect("events:detail", event.pk)
    return render(request, "events/event_form.html", {
        "form": form, "title": "New event", "event_type": event_type, "type_locked": False,
    })


@staff_member_required
def event_update(request, pk):
    """Edit an event; the type is locked as soon as any slot exists."""
    event = get_object_or_404(Event, pk=pk)
    locked = event.event_type_locked
    if locked:
        event_type = event.event_type
    else:
        event_type = request.POST.get("event_type") or request.GET.get("type") or event.event_type
        if event_type not in (Event.TYPE_ONLINE, Event.TYPE_OFFLINE):
            event_type = event.event_type
    form = EventForm(request.POST or None, instance=event, event_type=event_type, lock_type=locked)
    if request.method == "POST" and form.is_valid():
        event = form.save()
        log_action(request.user, AuditLogEntry.ACTION_UPDATE, event, "Event details updated.")
        messages.success(request, "Event updated.")
        return redirect("events:detail", event.pk)
    return render(request, "events/event_form.html", {
        "form": form,
        "title": f"Edit event — {event.name}",
        "event": event,
        "event_type": event_type,
        "type_locked": locked,
        "slot_count": event.slots.count() if locked else 0,
    })


@staff_member_required
def event_detail(request, pk):
    event = get_object_or_404(Event, pk=pk)
    slots = list(event.slots.select_related("host").prefetch_related("bookings"))
    bookable = [s for s in slots if not s.is_break]
    confirmed = event.bookings.filter(status=Booking.STATUS_CONFIRMED).count()
    today = timezone.localdate()
    event_days = event.event_dates()
    days_left = sum(1 for d in event_days if d >= today)
    skipped_dates = EventDay.skipped_dates_for(event)
    # Calendar chips: one per event day, with its live counts and skipped flag,
    # so the organiser sees the whole schedule as a day-by-day board.
    day_chips = []
    for d in event_days:
        day_slots = [s for s in slots if s.date == d and not s.is_break]
        day_chips.append({
            "date": d,
            "past": d < today,
            "skipped": d in skipped_dates,
            "slot_count": len(day_slots),
            "booked": sum(1 for s in day_slots if s.bookings.filter(status=Booking.STATUS_CONFIRMED).exists()),
        })
    # A chip click narrows the slot table to that day (?date=YYYY-MM-DD).
    selected_date = None
    raw_date = request.GET.get("date")
    if raw_date:
        try:
            selected_date = datetime.date.fromisoformat(raw_date)
        except ValueError:
            pass
    context = {
        "event": event,
        # Full shareable URL (domain included) — same source emails build their
        # links from, so the row shows exactly what a visitor receives.
        "public_url": (
            public_base_url() + reverse("bookings_public:public_event", args=[event.public_slug])
            if event.public_slug else ""
        ),
        "event_days": [{"date": d, "past": d < today} for d in event_days],
        "day_chips": day_chips,
        "selected_date": selected_date,
        "members": event.team_members.prefetch_related("slots"),
        "slots": [s for s in slots if selected_date is None or s.date == selected_date],
        "bookings": event.bookings.select_related("slot", "slot__host").order_by("-created_at"),
        "summary": {
            "hosts": event.team_members.filter(is_active=True).count(),
            "slots": len(bookable),
            "breaks": len(slots) - len(bookable),
            "booked": confirmed,
            "seats_left": sum(s.seats_left() for s in bookable),
            "days_left": max(days_left, 0) if not event.is_expired else 0,
        },
    }
    return render(request, "events/event_detail.html", context)


@staff_member_required
def event_confirm_delete(request, pk):
    """FR-1.2 — deleting an event with bookings requires confirmation."""
    event = get_object_or_404(Event, pk=pk)
    affected = list(event.bookings.filter(status=Booking.STATUS_CONFIRMED).select_related("slot", "slot__host"))
    return render(request, "events/event_confirm_delete.html", {"event": event, "affected": affected})


@staff_member_required
@require_POST
def event_delete(request, pk):
    """FR-1.2 — cascade cancellation notices, then remove the event."""
    from notifications.emails import send_cancellation_to_visitor

    event = get_object_or_404(Event, pk=pk)
    affected = list(event.bookings.filter(status=Booking.STATUS_CONFIRMED).select_related("slot", "slot__host"))
    now = timezone.now()
    for booking in affected:
        booking.status = Booking.STATUS_CANCELLED
        booking.cancelled_at = now
        booking.save(update_fields=["status", "cancelled_at", "updated_at"])
    # Visitor notices must go out while the booking rows still exist — the
    # NotificationLog FK would fail after the teardown transaction.
    for booking in affected:
        send_cancellation_to_visitor(booking, reason="The event was deleted by the organiser.")
    with transaction.atomic():
        log_action(
            request.user, AuditLogEntry.ACTION_DELETE, event,
            details=f"Event deleted; {len(affected)} active booking(s) cancelled with notice.",
        )
        Booking.objects.filter(event=event).delete()
        # Explicit teardown: Slot.host is PROTECT, so Django refuses the plain
        # event.delete() cascade even when hosts are in the same delete set.
        event.slots.all().delete()
        event.team_members.all().delete()
        event.delete()
    messages.success(request, f"Event “{event.name}” deleted; {len(affected)} visitor(s) notified.")
    return redirect("events:list")


# ---------------------------------------------------------------------------
# Event lifecycle (FR-1.3, FR-4.1, FR-1.4)
# ---------------------------------------------------------------------------
@staff_member_required
@require_POST
def event_publish(request, pk):
    """FR-4.1 — generate the public link and mark the event Live."""
    event = get_object_or_404(Event, pk=pk)
    if not event.public_slug:
        event.public_slug = event.generate_public_slug()
    event.status = Event.STATUS_LIVE
    event.save(update_fields=["public_slug", "status", "updated_at"])
    log_action(request.user, AuditLogEntry.ACTION_PUBLISH, event,
               details=f"Event published; public link /b/{event.public_slug}/.")
    messages.success(request, "Event is live — share the public booking link below.")
    return redirect("events:detail", event.pk)


@staff_member_required
@require_POST
def event_close(request, pk):
    event = get_object_or_404(Event, pk=pk)
    event.status = Event.STATUS_CLOSED
    event.save(update_fields=["status", "updated_at"])
    log_action(request.user, AuditLogEntry.ACTION_CLOSE, event, "Event closed manually.")
    messages.success(request, "Event closed — the public booking link no longer accepts bookings.")
    return redirect("events:detail", event.pk)


@staff_member_required
@require_POST
def event_reopen(request, pk):
    event = get_object_or_404(Event, pk=pk)
    if event.is_expired:
        messages.error(request, "This event's active duration has elapsed — create a new event instead.")
        return redirect("events:detail", event.pk)
    event.status = Event.STATUS_LIVE
    event.save(update_fields=["status", "updated_at"])
    log_action(request.user, AuditLogEntry.ACTION_REACTIVATE, event, "Event re-opened.")
    messages.success(request, "Event re-opened for booking.")
    return redirect("events:detail", event.pk)


@staff_member_required
@require_POST
def event_clone(request, pk):
    """Copy an event's setup so the next roadshow doesn't start from scratch.

    Copies the event settings, its active hosts, and the slot pattern (shifted
    to keep the same offset from day one, breaks included). Bookings are never
    copied — the new event starts empty, as a Draft, with no public link.
    """
    source = get_object_or_404(Event, pk=pk)
    with transaction.atomic():
        clone = Event.objects.get(pk=source.pk)
        clone.pk = None
        clone.name = f"{source.name} (copy)"
        clone.status = Event.STATUS_DRAFT
        clone.public_slug = None
        clone.save()

        host_map = {}
        for member in source.team_members.filter(is_active=True):
            copy = TeamMember.objects.get(pk=member.pk)
            copy.pk = None
            copy.event = clone
            copy.save()
            host_map[member.pk] = copy

        for slot in source.slots.all():
            host = host_map.get(slot.host_id)
            if host is None:
                continue  # slot belonged to a host that was removed
            offset = slot.date - source.start_date
            new_slot = Slot.objects.get(pk=slot.pk)
            new_slot.pk = None
            new_slot.event = clone
            new_slot.host = host
            new_slot.date = clone.start_date + offset
            # Carry breaks across; anything else starts open again.
            new_slot.status = (
                Slot.STATUS_BREAK if slot.is_break else Slot.STATUS_AVAILABLE
            )
            new_slot.save()  # keeps `mode` mirrored from the event type

    log_action(request.user, AuditLogEntry.ACTION_CREATE, clone,
               details=f"Cloned from “{source.name}”.")
    messages.success(
        request,
        f"Copied “{source.name}”. Review the dates and publish when you're ready.",
    )
    return redirect("events:detail", clone.pk)


# ---------------------------------------------------------------------------
# People directory (FR-2) — manage people once, pick them as hosts per event
# ---------------------------------------------------------------------------
def _people_queryset():
    """The directory list, annotated with how many events each person hosts."""
    return Person.objects.annotate(
        host_count=Count("team_members", distinct=True,
                         filter=Q(team_members__is_active=True)),
    )


def _sync_hosts_from_person(person):
    """Re-apply a person's directory details onto their active host records.

    Returns ``(synced, skipped)``; a record is skipped only when the email
    change would collide with a different host on the same event.
    """
    synced = skipped = 0
    for member in person.team_members.filter(is_active=True):
        member.name = person.name
        member.email = person.email
        member.role = person.role
        member.photo_url = person.photo_url
        member.photo = person.photo  # shares the stored file, no copy on disk
        member.linked_user = person.linked_user
        try:
            member.save()
            synced += 1
        except IntegrityError:
            skipped += 1  # another host on that event already uses this email
    return synced, skipped


@staff_member_required
def person_list(request):
    return render(request, "events/people_list.html",
                  {"people": _people_queryset()})


@staff_member_required
def person_create(request):
    form = PersonForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        person = form.save()
        log_action(request.user, AuditLogEntry.ACTION_CREATE, person,
                   "Person added to the directory.")
        messages.success(
            request, f"{person.name} added. Select them as a host on any event's Hosts section.")
        return redirect("events_people:list")
    return render(request, "events/person_form.html", {
        "form": form, "title": "New person",
        "subtitle": "People live in one directory — when an event needs hosts you just pick them.",
    })


@staff_member_required
def person_update(request, pk):
    person = get_object_or_404(Person, pk=pk)
    form = PersonForm(request.POST or None, request.FILES or None, instance=person)
    if request.method == "POST" and form.is_valid():
        person = form.save()
        synced = skipped = 0
        if form.cleaned_data.get("sync_hosts"):
            synced, skipped = _sync_hosts_from_person(person)
        log_action(request.user, AuditLogEntry.ACTION_UPDATE, person,
                   f"Person updated; {synced} active host record(s) synced.")
        message = f"{person.name} updated."
        if synced:
            message += f" {synced} event host record(s) refreshed."
        if skipped:
            message += (f" {skipped} host record(s) kept their old email "
                        "(another host on that event already uses it).")
        messages.success(request, message)
        return redirect("events_people:list")
    hosting = person.team_members.filter(is_active=True).count()
    return render(request, "events/person_form.html", {
        "form": form, "person": person, "title": f"Edit person — {person.name}",
        "subtitle": (f"Currently hosting at {hosting} event{'s' if hosting != 1 else ''}."
                     if hosting else "Not hosting any events right now."),
    })


@staff_member_required
@require_POST
def person_delete(request, pk):
    """Remove a directory entry. Host records on events keep their snapshot
    (TeamMember.person is SET_NULL), so past events and their bookings are
    never touched."""
    person = get_object_or_404(Person, pk=pk)
    name = person.name
    hosting = person.team_members.filter(is_active=True).count()
    log_action(request.user, AuditLogEntry.ACTION_DELETE, person,
               details=f"Person deleted; {hosting} active host record(s) kept their details.")
    person.delete()
    if request.headers.get("HX-Request"):
        return render(request, "events/partials/people_table.html",
                      {"people": _people_queryset()})
    messages.success(
        request, f"{name} removed from the directory."
        + (f" Their {hosting} active event host record(s) keep the details they were added with."
           if hosting else ""))
    return redirect("events_people:list")


# ---------------------------------------------------------------------------
# Team management (FR-2)
# ---------------------------------------------------------------------------
@staff_member_required
def team_add(request, event_pk):
    """Select people from the directory and add them as hosts (FR-2.1).

    Each pick copies the person's details onto a TeamMember snapshot for
    this event — the visitor-facing roster keeps working exactly as before.
    """
    event = get_object_or_404(Event, pk=event_pk)
    form = HostPickForm(request.POST or None, event=event)
    if request.method == "POST" and form.is_valid():
        added, skipped = [], []
        with transaction.atomic():
            for person in form.cleaned_data["people"]:
                if event.team_members.filter(email=person.email).exists():
                    skipped.append(person.name)  # e.g. a removed host: reactivate instead
                    continue
                member = TeamMember.create_from_person(event, person)
                log_action(request.user, AuditLogEntry.ACTION_CREATE, member,
                           details=f"Host added to “{event.name}” (picked from the people directory).")
                added.append(member.name)
        if request.headers.get("HX-Request"):
            return _team_section(request, event)
        if added:
            label = added[0] if len(added) == 1 else f"{len(added)} hosts"
            messages.success(request, f"{label} added to the team.")
        if skipped:
            messages.warning(request,
                             f"Already on this event's team (removed hosts can be reactivated): "
                             f"{', '.join(skipped)}.")
        return redirect("events:detail", event.pk)
    if request.method == "POST" and request.headers.get("HX-Request"):
        return _team_section(request, event, form=form)
    return render(request, "events/partials/team_pick_form.html",
                  {"form": form, "event": event})


@staff_member_required
def team_edit(request, event_pk, pk):
    event = get_object_or_404(Event, pk=event_pk)
    member = get_object_or_404(TeamMember, pk=pk, event=event)
    form = TeamMemberForm(request.POST or None, request.FILES or None, instance=member)
    if request.method == "POST" and form.is_valid():
        member = form.save()
        log_action(request.user, AuditLogEntry.ACTION_UPDATE, member, "Host details updated.")
        if request.headers.get("HX-Request"):
            return _team_section(request, event)
        messages.success(request, "Team member updated.")
        return redirect("events:detail", event.pk)
    if request.method == "POST" and request.headers.get("HX-Request"):
        return _team_section(request, event, form=form, member=member)
    return render(request, "events/partials/team_form.html",
                  {"form": form, "event": event, "member": member})


@staff_member_required
@require_POST
def team_remove(request, event_pk, pk):
    """FR-2.3 — remove host from event; flag live bookings for admin attention."""
    event = get_object_or_404(Event, pk=event_pk)
    member = get_object_or_404(TeamMember, pk=pk, event=event)
    with transaction.atomic():
        flagged = list(
            Booking.objects.filter(slot__host=member, status=Booking.STATUS_CONFIRMED)
            .select_related("slot", "slot__host", "event")
        )
        for booking in flagged:
            booking.needs_attention = True
            booking.save(update_fields=["needs_attention", "updated_at"])
        member.is_active = False
        member.save(update_fields=["is_active", "updated_at"])
        log_action(
            request.user, AuditLogEntry.ACTION_UPDATE, member,
            details=f"Host removed from event; {len(flagged)} booking(s) flagged for admin attention.",
        )
        HostNotification.objects.create(
            host=member, event=event, kind=HostNotification.KIND_FLAGS,
            title=f"Attention: host {member.name} removed from {event.name}",
            message=f"{len(flagged)} live booking(s) need to be rearranged.",
            meeting_datetime=timezone.now(),
        )
    for booking in flagged:
        send_attention_email(booking, reason="Your host is no longer available for this event.")
    if request.headers.get("HX-Request"):
        return _team_section(request, event)
    messages.success(request, f"{member.name} removed; {len(flagged)} booking(s) flagged.")
    return redirect("events:detail", event.pk)


@staff_member_required
@require_POST
def team_reactivate(request, event_pk, pk):
    event = get_object_or_404(Event, pk=event_pk)
    member = get_object_or_404(TeamMember, pk=pk, event=event)
    member.is_active = True
    member.save(update_fields=["is_active", "updated_at"])
    log_action(request.user, AuditLogEntry.ACTION_UPDATE, member, "Host re-activated on the event.")
    if request.headers.get("HX-Request"):
        return _team_section(request, event)
    return redirect("events:detail", event.pk)


@staff_member_required
@require_POST
def team_delete(request, event_pk, pk):
    """Hard delete — only possible when the host has no slots at all."""
    event = get_object_or_404(Event, pk=event_pk)
    member = get_object_or_404(TeamMember, pk=pk, event=event)
    if member.slots.exists():
        messages.error(request, "Remove their slots first (this host still has slots).")
        return redirect("events:detail", event.pk)
    log_action(request.user, AuditLogEntry.ACTION_DELETE, member, "Host deleted.")
    member.delete()
    if request.headers.get("HX-Request"):
        return _team_section(request, event)
    messages.success(request, "Team member deleted.")
    return redirect("events:detail", event.pk)


# ---------------------------------------------------------------------------
# Slot management (FR-3)
# ---------------------------------------------------------------------------
@staff_member_required
def slot_add(request, event_pk):
    event = get_object_or_404(Event, pk=event_pk)
    initial = {}
    if request.GET.get("date"):
        try:
            initial["date"] = datetime.date.fromisoformat(request.GET["date"])
        except ValueError:
            pass
    form = SlotForm(request.POST or None, event=event, initial=initial or None)
    if request.method == "POST" and form.is_valid():
        slot = form.save(commit=False)
        slot.event = event
        slot.save()
        log_action(request.user, AuditLogEntry.ACTION_CREATE, slot, "Slot created.")
        if request.headers.get("HX-Request"):
            return _slot_section(request, event)
        messages.success(request, "Slot created.")
        return redirect("events:detail", event.pk)
    if request.method == "POST" and request.headers.get("HX-Request"):
        return _slot_section(request, event, form=form)
    return render(request, "events/partials/slot_form.html",
                  {"form": form, "event": event, "slot": None})


@staff_member_required
def slot_edit(request, event_pk, pk):
    event = get_object_or_404(Event, pk=event_pk)
    slot = get_object_or_404(Slot, pk=pk, event=event)
    form = SlotForm(request.POST or None, instance=slot, event=event)
    if request.method == "POST" and form.is_valid():
        slot = form.save()
        log_action(request.user, AuditLogEntry.ACTION_UPDATE, slot, "Slot updated.")
        if request.headers.get("HX-Request"):
            return _slot_section(request, event)
        messages.success(request, "Slot updated.")
        return redirect("events:detail", event.pk)
    if request.method == "POST" and request.headers.get("HX-Request"):
        return _slot_section(request, event, form=form, slot=slot)
    return render(request, "events/partials/slot_form.html",
                  {"form": form, "event": event, "slot": slot})


@staff_member_required
def slot_build(request, event_pk):
    """Build a day pattern once and stamp it onto the chosen event days.

    The days come from the event's own calendar (auto-synced with the event's
    dates and weekday selection), the host is optional (blank = every active
    host), and any number of break windows can be kept clear — optionally
    shown in the schedule as non-bookable "Break" entries.
    """
    event = get_object_or_404(Event, pk=event_pk)
    hosts = event.team_members.filter(is_active=True)
    form = SlotBuilderForm(request.POST or None, event=event,
                           host=request.GET.get("host") or None)
    created, skipped = 0, 0
    if request.method == "POST" and form.is_valid():
        times = form.generate_times()
        capacity = form.cleaned_data["capacity"]
        duration = form.cleaned_data["duration_minutes"]
        windows = form.break_windows() if form.cleaned_data.get("mark_breaks") else []
        target_hosts = [form.cleaned_data["host"]] if form.cleaned_data.get("host") else list(hosts)
        for host in target_hosts:
            for date in form.selected_dates():
                for start in times:
                    _, made = Slot.objects.get_or_create(
                        host=host, date=date, start_time=start,
                        defaults={
                            # mode is set by Slot.save() from the event type.
                            "event": event, "capacity": capacity,
                            "duration_minutes": duration,
                        },
                    )
                    if made:
                        created += 1
                    else:
                        skipped += 1
                for break_start, break_end in windows:
                    # One non-bookable entry per host per day, so each break is
                    # visible in the schedule rather than an unexplained gap.
                    minutes = int(
                        (datetime.datetime.combine(date, break_end)
                         - datetime.datetime.combine(date, break_start)).total_seconds() // 60
                    )
                    _, made = Slot.objects.get_or_create(
                        host=host, date=date, start_time=break_start,
                        defaults={
                            "event": event, "capacity": 1,
                            "duration_minutes": max(minutes, 5),
                            "status": Slot.STATUS_BREAK,
                        },
                    )
                    if made:
                        created += 1
        if created or skipped:
            log_action(request.user, AuditLogEntry.ACTION_CREATE, event,
                       details=f"Slot builder: {created} slot(s) created ({skipped} already existed).")
        if request.headers.get("HX-Request"):
            return _slot_section(request, event)
        messages.success(request, f"{created} slot(s) created ({skipped} already existed).")
        return redirect("events:detail", event.pk)
    if request.method == "POST" and request.headers.get("HX-Request"):
        return _slot_section(request, event, builder_form=form, host_count=hosts.count())
    return render(request, "events/partials/slot_builder_form.html",
                  {"form": form, "event": event, "host_count": hosts.count()})


@staff_member_required
@require_POST
def slot_delete(request, event_pk, pk):
    """FR-3.5 — deleting a booked slot cancels its booking with notice."""
    from notifications.emails import send_cancellation_to_visitor

    event = get_object_or_404(Event, pk=event_pk)
    slot = get_object_or_404(Slot, pk=pk, event=event)
    affected = list(slot.bookings.filter(status=Booking.STATUS_CONFIRMED).select_related("slot", "slot__host"))
    with transaction.atomic():
        for booking in affected:
            booking.status = Booking.STATUS_CANCELLED
            booking.cancelled_at = timezone.now()
            booking.save(update_fields=["status", "cancelled_at", "updated_at"])
        log_action(request.user, AuditLogEntry.ACTION_DELETE, slot,
                   details=f"Slot deleted; {len(affected)} booking(s) cancelled with notice.")
        slot.bookings.all().delete()
        slot.delete()
    for booking in affected:
        send_cancellation_to_visitor(booking, reason="The organiser removed this meeting slot.")
    if request.headers.get("HX-Request"):
        return _slot_section(request, event)
    messages.success(request, f"Slot deleted; {len(affected)} visitor(s) notified.")
    return redirect("events:detail", event.pk)


@staff_member_required
@require_POST
def slot_close(request, event_pk, pk):
    """FR-3.6 — close a slot so it can no longer be booked."""
    event = get_object_or_404(Event, pk=event_pk)
    slot = get_object_or_404(Slot, pk=pk, event=event)
    slot.status = Slot.STATUS_CLOSED
    slot.save(update_fields=["status", "updated_at"])
    log_action(request.user, AuditLogEntry.ACTION_CLOSE, slot, "Slot closed for booking.")
    if request.headers.get("HX-Request"):
        return _slot_section(request, event)
    return redirect("events:detail", event.pk)


@staff_member_required
@require_POST
def slot_reopen(request, event_pk, pk):
    event = get_object_or_404(Event, pk=event_pk)
    slot = get_object_or_404(Slot, pk=pk, event=event)
    slot.status = Slot.STATUS_AVAILABLE if slot.seats_left() > 0 else Slot.STATUS_BOOKED
    slot.save(update_fields=["status", "updated_at"])
    log_action(request.user, AuditLogEntry.ACTION_REACTIVATE, slot, "Slot re-opened.")
    if request.headers.get("HX-Request"):
        return _slot_section(request, event)
    return redirect("events:detail", event.pk)


@staff_member_required
@require_POST
def day_toggle_skip(request, event_pk, date):
    """Skip (or re-open) one event day — the calendar's day-level control.

    Skipping hides the whole day from visitors, blocks new slot creation on
    it, and cancels that day's confirmed bookings with notice emails so both
    the visitor and the host know the meeting is off. Re-opening flips the
    flag back; the day's slots were never touched, so they simply return.
    """
    from bookings.services import cancel_booking
    from notifications.emails import send_cancellation_to_visitor

    event = get_object_or_404(Event, pk=event_pk)
    try:
        day = datetime.date.fromisoformat(date)
    except ValueError:
        return redirect("events:detail", event.pk)
    if day not in event.event_dates():
        messages.warning(request, "That date is not one of this event's days.")
        return redirect("events:detail", event.pk)
    entry, _ = EventDay.objects.get_or_create(event=event, date=day)
    entry.skipped = not entry.skipped
    entry.save(update_fields=["skipped"])
    if entry.skipped:
        affected = list(
            Booking.objects.filter(
                event=event, slot__date=day, status=Booking.STATUS_CONFIRMED
            ).select_related("slot", "slot__host")
        )
        with transaction.atomic():
            for booking in affected:
                cancel_booking(booking)
            log_action(
                request.user, AuditLogEntry.ACTION_CLOSE, event,
                details=f"Day {day:%d %b %Y} skipped; {len(affected)} booking(s) cancelled with notice.",
            )
        for booking in affected:
            send_cancellation_to_visitor(
                booking, reason=f"The organiser is not holding meetings on {day:%d %B}."
            )
        verb = f"Day skipped — {len(affected)} meeting(s) cancelled and visitors notified."
    else:
        log_action(request.user, AuditLogEntry.ACTION_REACTIVATE, event,
                   details=f"Day {day:%d %b %Y} re-opened.")
        verb = "Day re-opened — its slots are bookable again."
    if request.headers.get("HX-Request"):
        return _slot_section(request, event)
    messages.success(request, verb)
    return redirect("events:detail", event.pk)

