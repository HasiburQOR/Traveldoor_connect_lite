"""
Admin views for events, team members and slots (FR-1, FR-2, FR-3).

All views require a staff login (FR-8.1). Mutating views write an audit
entry (FR-9.1) and HTMX requests swap in refreshed partials (NFR-2).
"""
import calendar
import datetime
from urllib.parse import urlsplit

from django.contrib import messages
from core.decorators import staff_member_required  # app login page, not the Django admin
from core.pagination import paginate
from django.db import IntegrityError, transaction
from django.db.models import Count, F, IntegerField, OuterRef, Q, Subquery, Sum, Value
from django.db.models.functions import Coalesce, Greatest
from django.http import QueryDict
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


# A long-running event holds tens of thousands of slots: the slot table is
# always read one month (or one day) at a time, and paged within that.
SLOTS_PER_PAGE = 100
# Recent bookings shown on the event screen; the full list has its own page.
EVENT_BOOKINGS_SHOWN = 50
# Upper bound for one slot-builder run, so a mistyped pattern can't try to
# write millions of rows in a single request.
MAX_SLOTS_PER_BUILD = 100_000

TIME_OF_DAY = {
    "morning": Q(start_time__lt=datetime.time(12, 0)),
    "afternoon": Q(start_time__gte=datetime.time(12, 0), start_time__lt=datetime.time(17, 0)),
    "evening": Q(start_time__gte=datetime.time(17, 0)),
}


def _members(event):
    """The event's hosts with their slot count and first slot.

    Counted in the database — prefetching every slot of every host just to
    show a number is what made long schedules slow to open.
    """
    members = list(event.team_members.annotate(slot_count=Count("slots")))
    for member in members:
        member.first_slot = member.slots.first() if member.slot_count else None
        if member.first_slot is not None:
            member.first_slot.event = event
    return members


def _team_section(request, event, *, form=None, member=None):
    """Render the whole team section, optionally with an open form inside it.

    The inline forms swap this section with ``outerHTML``, so a rejected form
    must come back *inside* the section — returning the bare form would delete
    the section (table and all) and with it the swap target, leaving the form
    orphaned and every later submit a no-op.
    """
    return render(request, "events/partials/team_section.html",
                  {"event": event, "members": _members(event), "form": form, "member": member})


def _view_params(request):
    """The filters of the page the admin is looking at.

    An HTMX swap is a request to some other URL, so the page's own query
    string (?month=…&date=…&page=…) is read from ``HX-Current-URL`` — the
    refreshed section then shows the same month, day and page as before.
    """
    if request.headers.get("HX-Request"):
        return QueryDict(urlsplit(request.headers.get("HX-Current-URL", "")).query)
    return request.GET


def _slot_context(request, event):
    """Everything the slot section shows: one month of day chips and one page
    of slots, each loaded with a bounded number of queries."""
    params = _view_params(request)
    today = timezone.localdate()
    event_days = event.event_dates()

    # Months come from the event's own calendar, plus any month that still
    # holds slots (e.g. after the event's dates were changed).
    month_keys = {(d.year, d.month) for d in event_days}
    month_keys.update((d.year, d.month) for d in event.slots.dates("date", "month"))
    month_keys = sorted(month_keys)

    selected_date = None
    try:
        selected_date = datetime.date.fromisoformat(params.get("date", ""))
    except ValueError:
        pass
    month = None
    if selected_date is not None:
        month = (selected_date.year, selected_date.month)
    else:
        try:
            parsed = datetime.datetime.strptime(params.get("month", ""), "%Y-%m")
            month = (parsed.year, parsed.month)
        except ValueError:
            pass
        if month not in month_keys:
            # Default to the current month, else the next one with event days.
            upcoming = [k for k in month_keys if k >= (today.year, today.month)]
            month = upcoming[0] if upcoming else (month_keys[-1] if month_keys else None)
    tod = params.get("tod", "")
    tod = tod if tod in TIME_OF_DAY else ""

    slots = event.slots.all()
    if month is not None:
        slots = slots.filter(date__year=month[0], date__month=month[1])

    # Calendar chips: one per event day of the month, counted in one query.
    counts = {
        row["date"]: row for row in
        slots.exclude(status=Slot.STATUS_BREAK).values("date").annotate(
            slot_count=Count("pk", distinct=True),
            booked=Count("pk", distinct=True,
                         filter=Q(bookings__status=Booking.STATUS_CONFIRMED)),
        ).order_by()
    }
    skipped_dates = EventDay.skipped_dates_for(event)
    day_chips = [
        {
            "date": d, "past": d < today, "skipped": d in skipped_dates,
            "slot_count": counts.get(d, {}).get("slot_count", 0),
            "booked": counts.get(d, {}).get("booked", 0),
        }
        for d in event_days if month is None or (d.year, d.month) == month
    ]

    if selected_date is not None:
        slots = slots.filter(date=selected_date)
    if tod:
        slots = slots.filter(TIME_OF_DAY[tod])
    page = paginate(
        request,
        # Explicit order: Meta.ordering is dropped once a query is grouped.
        Slot.with_confirmed_count(slots.select_related("host"))
        .order_by("date", "start_time", "pk"),
        per_page=SLOTS_PER_PAGE, page_number=params.get("page"),
    )
    page.object_list = list(page.object_list)
    for slot in page.object_list:
        slot.event = event  # share the one instance: no query per row

    selected_month = f"{month[0]:04d}-{month[1]:02d}" if month else ""
    page_params = QueryDict(mutable=True)
    for key, value in (("month", selected_month), ("tod", tod),
                       ("date", selected_date.isoformat() if selected_date else "")):
        if value:
            page_params[key] = value
    return {
        "day_chips": day_chips,
        "selected_date": selected_date,
        "selected_month": selected_month,
        "selected_tod": tod,
        "month_options": [
            {"value": f"{y:04d}-{m:02d}", "label": f"{calendar.month_name[m]} {y}"}
            for y, m in month_keys
        ],
        "slots": page.object_list,
        "slot_page": page,
        "slot_qs": page_params.urlencode() + "&" if page_params else "",
    }


def _slot_section(request, event, *, form=None, slot=None, builder_form=None, host_count=None):
    """Render the whole slot section, optionally with an open form inside it.

    Same swap-target rule as ``_team_section``.
    """
    context = _slot_context(request, event)
    context.update({"event": event, "form": form, "slot": slot,
                    "builder_form": builder_form, "host_count": host_count})
    return render(request, "events/partials/slot_section.html", context)


def _per_event_count(queryset):
    """Rows of ``queryset`` per event, as a subquery.

    Separate subqueries instead of three joined Count()s: joining hosts ×
    slots × bookings multiplies the rows before counting them, which stops
    scaling the moment one event has a year of slots.
    """
    counted = (
        queryset.filter(event=OuterRef("pk")).order_by().values("event")
        .annotate(n=Count("pk")).values("n")
    )
    return Coalesce(Subquery(counted, output_field=IntegerField()), Value(0))


# ---------------------------------------------------------------------------
# Events (FR-1)
# ---------------------------------------------------------------------------
@staff_member_required
def event_list(request):
    close_expired_events()  # opportunistic FR-1.4 in addition to the scheduler
    # Annotate the counts the list shows, so the table is one query rather than
    # three per row.
    events = Event.objects.annotate(
        host_count=_per_event_count(TeamMember.objects.all()),
        slot_count=_per_event_count(Slot.objects.all()),
        booking_count=_per_event_count(
            Booking.objects.filter(status=Booking.STATUS_CONFIRMED)),
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
    today = timezone.localdate()
    event_days = event.event_dates()
    days_left = sum(1 for d in event_days if d >= today)
    # Summary numbers are aggregated in the database — the event may hold a
    # year or two of slots, far too many to load just to count them.
    totals = event.slots.aggregate(
        slots=Count("pk", filter=~Q(status=Slot.STATUS_BREAK)),
        breaks=Count("pk", filter=Q(status=Slot.STATUS_BREAK)),
    )
    seats_left = Slot.with_confirmed_count(
        event.slots.exclude(status__in=[Slot.STATUS_BREAK, Slot.STATUS_CLOSED])
    ).aggregate(n=Sum(Greatest(F("capacity") - F("confirmed_count"), Value(0))))["n"] or 0
    bookings = event.bookings.select_related("slot", "slot__host").order_by("-created_at")
    booking_total = bookings.count()
    context = {
        "event": event,
        # Full shareable URL (domain included) — same source emails build their
        # links from, so the row shows exactly what a visitor receives.
        "public_url": (
            public_base_url() + reverse("bookings_public:public_event", args=[event.public_slug])
            if event.public_slug else ""
        ),
        # The all-events directory — one stable link to paste into a website.
        "directory_url": public_base_url() + reverse("bookings_public:public_events"),
        "event_days": [{"date": d, "past": d < today} for d in event_days],
        "members": _members(event),
        "bookings": bookings[:EVENT_BOOKINGS_SHOWN],
        "bookings_hidden": max(booking_total - EVENT_BOOKINGS_SHOWN, 0),
        # First open slot, to pre-load the "Book for a visitor" form.
        "available_slots": Slot.public_for_event(event, limit=1),
        "summary": {
            "hosts": event.team_members.filter(is_active=True).count(),
            "slots": totals["slots"],
            "breaks": totals["breaks"],
            "booked": event.bookings.filter(status=Booking.STATUS_CONFIRMED).count(),
            "seats_left": seats_left,
            "days_left": max(days_left, 0) if not event.is_expired else 0,
        },
    }
    context.update(_slot_context(request, event))
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

        copies = []
        for slot in source.slots.iterator(chunk_size=2000):
            host = host_map.get(slot.host_id)
            if host is None:
                continue  # slot belonged to a host that was removed
            offset = slot.date - source.start_date
            slot.pk = None
            slot._state.adding = True
            slot.event = clone
            slot.host = host
            slot.date = clone.start_date + offset
            # Carry breaks across; anything else starts open again.
            slot.status = Slot.STATUS_BREAK if slot.is_break else Slot.STATUS_AVAILABLE
            if slot.video_provider == Event.PROVIDER_JITSI:
                # An auto-generated room belongs to the source slot — the
                # copy gets its own, or both events would meet in one room.
                slot.meeting_link = ""
            slot.sync_from_event()  # keeps `mode` mirrored from the event type
            copies.append(slot)
        Slot.objects.bulk_create(copies, batch_size=500)

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
    plan = None
    if request.method == "POST" and form.is_valid():
        times = form.generate_times()
        windows = form.break_windows() if form.cleaned_data.get("mark_breaks") else []
        target_hosts = [form.cleaned_data["host"]] if form.cleaned_data.get("host") else list(hosts)
        dates = form.selected_dates()
        total = len(target_hosts) * len(dates) * (len(times) + len(windows))
        if total > MAX_SLOTS_PER_BUILD:
            form.add_error(None, (
                f"That would create about {total:,} slots in one go — the limit is "
                f"{MAX_SLOTS_PER_BUILD:,}. Build fewer days or one host at a time."
            ))
        else:
            plan = (times, windows, target_hosts, dates)
    if plan is not None:
        times, windows, target_hosts, dates = plan
        capacity = form.cleaned_data["capacity"]
        duration = form.cleaned_data["duration_minutes"]
        # One read for what already exists and one bulk insert for the rest —
        # a year of days is thousands of slots per host, far too many to
        # get_or_create one at a time.
        taken = set()
        if target_hosts and dates:
            taken = set(
                Slot.objects.filter(host__in=target_hosts, date__range=(min(dates), max(dates)))
                .values_list("host_id", "date", "start_time")
            )
        new_slots = []
        for host in target_hosts:
            for date in dates:
                for start in times:
                    key = (host.pk, date, start)
                    if key in taken:
                        skipped += 1
                        continue
                    taken.add(key)
                    # mode is set by sync_from_event() from the event type.
                    new_slots.append(Slot(
                        event=event, host=host, date=date, start_time=start,
                        capacity=capacity, duration_minutes=duration,
                    ))
                for break_start, break_end in windows:
                    # One non-bookable entry per host per day, so each break is
                    # visible in the schedule rather than an unexplained gap.
                    key = (host.pk, date, break_start)
                    if key in taken:
                        continue
                    taken.add(key)
                    minutes = int(
                        (datetime.datetime.combine(date, break_end)
                         - datetime.datetime.combine(date, break_start)).total_seconds() // 60
                    )
                    new_slots.append(Slot(
                        event=event, host=host, date=date, start_time=break_start,
                        capacity=1, duration_minutes=max(minutes, 5),
                        status=Slot.STATUS_BREAK,
                    ))
        for new_slot in new_slots:
            new_slot.sync_from_event()  # bulk_create() never calls save()
        with transaction.atomic():
            # ignore_conflicts: a double-submitted form must not fail the run.
            Slot.objects.bulk_create(new_slots, batch_size=500, ignore_conflicts=True)
        created = len(new_slots)
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
    for booking in affected:
        booking.status = Booking.STATUS_CANCELLED
        booking.cancelled_at = timezone.now()
        booking.save(update_fields=["status", "cancelled_at", "updated_at"])
    # Visitor notices must go out while the booking rows still exist — the
    # NotificationLog FK fails once they are deleted (same rule as event_delete).
    for booking in affected:
        send_cancellation_to_visitor(booking, reason="The organiser removed this meeting slot.")
    with transaction.atomic():
        log_action(request.user, AuditLogEntry.ACTION_DELETE, slot,
                   details=f"Slot deleted; {len(affected)} booking(s) cancelled with notice.")
        slot.bookings.all().delete()
        slot.delete()
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
    # Not seats_left(): that reports 0 for a closed slot, which is what this is.
    has_room = slot.active_bookings_count() < slot.capacity
    slot.status = Slot.STATUS_AVAILABLE if has_room else Slot.STATUS_BOOKED
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

