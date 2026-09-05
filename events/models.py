"""
Events app models (SRS section 5 data model + FR-1, FR-2, FR-3).

Event        — time-boxed event with public booking link (FR-1, FR-4.1)
Person       — directory entry for someone who can host at events (FR-2)
TeamMember   — host attached to an event (FR-2)
Slot         — predefined availability slot owned by a host (FR-3)
"""
import datetime

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.urls import reverse
from django.utils import timezone
from django.utils.text import slugify


class Event(models.Model):
    """A time-boxed event such as a roadshow (FR-1.1)."""

    STATUS_DRAFT = "draft"
    STATUS_LIVE = "live"
    STATUS_CLOSED = "closed"
    STATUS_CHOICES = [
        (STATUS_DRAFT, "Draft"),
        (STATUS_LIVE, "Live"),
        (STATUS_CLOSED, "Closed"),
    ]

    # An event is strictly one type, chosen once at creation and immutable
    # once any slot exists. There are no per-slot overrides: every slot on an
    # online event is online, every slot on an offline event is offline.
    TYPE_ONLINE = "online"
    TYPE_OFFLINE = "offline"
    TYPE_CHOICES = [
        (TYPE_ONLINE, "Online — video call"),
        (TYPE_OFFLINE, "Offline — in person at a venue"),
    ]

    PROVIDER_GOOGLE_MEET = "google_meet"
    PROVIDER_ZOOM = "zoom"
    # A private, auto-generated per-slot room (see Slot.save()). Free, no
    # account or API needed — every slot gets its own room, so concurrent
    # meetings never share one.
    PROVIDER_JITSI = "jitsi"
    PROVIDER_CHOICES = [
        (PROVIDER_GOOGLE_MEET, "Google Meet"),
        (PROVIDER_ZOOM, "Zoom"),
        (PROVIDER_JITSI, "Auto (private room)"),
    ]

    # Calendar span guard — keeps event_dates() and the breakdown UI bounded.
    MAX_SPAN_DAYS = 366
    # Short labels for date.weekday() numbers (0=Monday).
    WEEKDAY_LABELS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
    WEEKDAYS_ALL = ",".join(str(d) for d in range(7))

    name = models.CharField(max_length=200)
    description = models.TextField(blank=True, default="")
    start_date = models.DateField(help_text="First day the event is active.")
    duration_days = models.PositiveIntegerField(
        default=1, help_text="Number of days the event stays live (auto end date)."
    )
    # Which weekdays the event actually runs on, 0=Monday … 6=Sunday (matching
    # date.weekday()). Blank = every day in the window counts, so events created
    # before this field existed keep their exact behaviour.
    active_weekdays = models.CharField(
        max_length=20,
        blank=True,
        default="",
        help_text="Comma-separated weekdays the event runs on, 0=Mon … 6=Sun. Blank = every day.",
    )
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=STATUS_DRAFT)
    public_slug = models.SlugField(max_length=80, unique=True, blank=True, null=True)

    event_type = models.CharField(
        max_length=10,
        choices=TYPE_CHOICES,
        default=TYPE_ONLINE,
        help_text="Set once at creation; locked as soon as the event has slots.",
    )

    # Event-level ONLINE defaults — the provider labels every slot that
    # doesn't pick its own; the link, when set, is one room shared by every
    # slot without its own (online events only). A blank link means each slot
    # gets its own auto-generated private room — see Slot.save().
    default_video_provider = models.CharField(
        max_length=20, choices=PROVIDER_CHOICES, blank=True, default="",
        help_text="Provider label shown to visitors (Google Meet or Zoom).",
    )
    default_meeting_link = models.URLField(
        blank=True, default="",
        help_text="Optional room shared by every slot without its own link; "
                  "blank means each slot gets its own auto-generated private room.",
    )

    # Event-level OFFLINE defaults — overridable per slot (FR-3.3)
    venue = models.CharField(max_length=200, blank=True, default="")
    hall_name = models.CharField(max_length=200, blank=True, default="")
    table_name = models.CharField(max_length=200, blank=True, default="")

    # Notification configuration (FR-5.2, FR-6.3 — configurable, sensible default)
    reminder_hours_csv = models.CharField(
        max_length=50,
        blank=True,
        default="",
        help_text="Comma-separated hours before the meeting to email a reminder, e.g. 24,1",
    )
    inapp_lead_minutes = models.PositiveIntegerField(
        default=30, help_text="Minutes before a meeting to raise the in-app host notification."
    )
    same_day_summary = models.BooleanField(
        default=True, help_text="Also send hosts a same-day summary of their meetings."
    )

    # Visitor form configuration (FR-4.4 — configurable required fields)
    require_phone = models.BooleanField(default=False)
    require_company = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-start_date"]

    def __str__(self):
        return self.name

    # ----- dates & status ---------------------------------------------------
    @property
    def end_date(self):
        """Auto-computed end date (FR-1.1): inclusive last active day."""
        return self.start_date + datetime.timedelta(days=max(self.duration_days, 1) - 1)

    # ----- weekday schedule -------------------------------------------------
    def weekday_set(self):
        """Active weekdays as a set of ints (0=Mon … 6=Sun); blank = all of them."""
        parsed = set()
        for part in (self.active_weekdays or "").split(","):
            part = part.strip()
            if part.isdigit() and int(part) <= 6:
                parsed.add(int(part))
        return parsed or set(range(7))

    @property
    def runs_selected_days(self):
        """True when only some weekdays are active (drives the summary text)."""
        return 0 < len(self.weekday_set()) < 7

    @property
    def weekday_summary(self):
        """Short human summary, e.g. "Mon, Wed & Fri"; empty when every day counts."""
        if not self.runs_selected_days:
            return ""
        labels = [self.WEEKDAY_LABELS[d] for d in sorted(self.weekday_set())]
        if len(labels) == 1:
            return labels[0]
        return ", ".join(labels[:-1]) + " & " + labels[-1]

    def event_dates(self):
        """The actual dates the event runs on: selected weekdays inside the span.

        Weekends (or any unticked day) between the first and last date are
        simply skipped — free days inside the event's window.
        """
        days = self.weekday_set()
        dates, current = [], self.start_date
        end = self.end_date
        while current <= end:
            if current.weekday() in days:
                dates.append(current)
            current += datetime.timedelta(days=1)
        return dates

    @property
    def last_event_date(self):
        """Last date the event actually runs on (end of the span as a fallback)."""
        dates = self.event_dates()
        return dates[-1] if dates else self.end_date

    @property
    def booking_close_at(self):
        """Public booking access closes at the very end of the end date (FR-1.4)."""
        close = datetime.datetime.combine(self.end_date, datetime.time.max)
        return timezone.make_aware(close) if timezone.is_naive(close) else close

    @property
    def is_expired(self):
        return timezone.now() > self.booking_close_at

    @property
    def is_publicly_open(self):
        """True when visitors may book (FR-1.4 auto-close rule)."""
        return self.status == self.STATUS_LIVE and not self.is_expired

    # ----- event type -------------------------------------------------------
    @property
    def is_online(self):
        return self.event_type == self.TYPE_ONLINE

    @property
    def is_offline(self):
        return self.event_type == self.TYPE_OFFLINE

    @property
    def has_slots(self):
        """True once any slot exists — the point where event_type locks."""
        return bool(self.pk) and self.slots.exists()

    @property
    def event_type_locked(self):
        """Event type is immutable after the first slot is created."""
        return self.has_slots

    @property
    def type_label(self):
        return "Video call" if self.is_online else "In person"

    def reminder_hours(self):
        """Parse reminder_hours_csv into a sorted list of ints (FR-5.2)."""
        from core.runtime import default_reminder_hours

        raw = self.reminder_hours_csv or default_reminder_hours()
        hours = []
        for part in raw.split(","):
            part = part.strip()
            if not part:
                continue
            try:
                value = int(part)
            except ValueError:
                continue
            if value > 0 and value not in hours:
                hours.append(value)
        return sorted(hours, reverse=True)

    def clean(self):
        errors = {}
        if self.duration_days and self.duration_days < 1:
            errors["duration_days"] = "Duration must be at least 1 day."
        if self.duration_days and self.duration_days > self.MAX_SPAN_DAYS:
            errors["duration_days"] = (
                f"Keep an event within {self.MAX_SPAN_DAYS} days — split longer runs into separate events."
            )
        if self.active_weekdays:
            parts = [p.strip() for p in self.active_weekdays.split(",") if p.strip()]
            bad = [p for p in parts if not p.isdigit() or int(p) > 6]
            if bad:
                errors["active_weekdays"] = "Weekdays must be numbers 0–6 (0=Monday), comma separated."
            elif len(parts) < 7 and self.start_date and not self.event_dates():
                errors["active_weekdays"] = (
                    f"None of the selected weekdays fall between "
                    f"{self.start_date:%d %b %Y} and {self.end_date:%d %b %Y} — "
                    f"pick other days or widen the window."
                )
        # Type-irrelevant fields never carry data — they are absent from the
        # form for the other type, so anything left over is stale.
        if self.is_online:
            self.venue = self.hall_name = self.table_name = ""
        else:
            self.default_video_provider = self.default_meeting_link = ""
        if errors:
            raise ValidationError(errors)

    def generate_public_slug(self):
        """Unique public slug for the shareable booking link (FR-4.1)."""
        base = slugify(self.name)[:60] or "event"
        candidate, suffix = base, ""
        while Event.objects.filter(public_slug=candidate).exclude(pk=self.pk).exists():
            suffix = (suffix or 0) + 1
            candidate = f"{base}-{suffix}"
        return candidate

    def public_url(self):
        if self.public_slug:
            return reverse("bookings:public_event", args=[self.public_slug])
        return ""

class Person(models.Model):
    """A person in the staff directory who can host at events (FR-2).

    People are created once, with their details, on the People page. When
    an event needs hosts the admin simply picks people from this
    directory; each pick becomes a TeamMember snapshot on that event, so
    past events keep the details they were published with even if the
    directory entry changes later (and editing a person can re-sync their
    active host records).
    """

    name = models.CharField(max_length=200)
    email = models.EmailField(
        unique=True, help_text="One directory entry per email address."
    )
    role = models.CharField(
        max_length=120, blank=True, default="", help_text="Optional title shown to visitors."
    )
    photo_url = models.URLField(
        blank=True, default="", help_text="Optional photo shown on the host roster (FR-4.2)."
    )
    # Uploaded photo — an easier alternative to the URL field. When both are
    # set, the upload wins (see photo_display_url, which every public template
    # reads; photo_url stays a plain URL so pasted links keep working).
    photo = models.ImageField(
        upload_to="people/%Y/%m/", null=True, blank=True,
        help_text="Optional uploaded photo — shown instead of the photo link when present.",
    )
    linked_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="person_profiles",
        help_text="Optional admin login; when signed in, this person sees their own schedule and reminders.",
    )
    is_active = models.BooleanField(
        default=True,
        help_text="Inactive people are hidden from the “add hosts” picker but keep their hosting history.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]
        verbose_name_plural = "people"

    def __str__(self):
        return self.name

    @property
    def photo_display_url(self):
        """The photo visitors see: an uploaded file wins over a pasted link."""
        if self.photo and self.photo.name:
            return self.photo.url
        return self.photo_url


class TeamMember(models.Model):
    """Host / team member attached to one event (FR-2.1, FR-2.2)."""

    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="team_members")
    # Directory link: kept as a snapshot when a Person is added to an event,
    # so deleting or editing the directory entry never rewrites history on
    # its own (the People page offers an explicit re-sync instead).
    person = models.ForeignKey(
        Person,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="team_members",
        help_text="Directory entry this host was picked from.",
    )
    name = models.CharField(max_length=200)
    email = models.EmailField()
    role = models.CharField(max_length=120, blank=True, default="", help_text="Optional title shown to visitors.")
    photo_url = models.URLField(blank=True, default="", help_text="Optional photo shown on the roster (FR-4.2).")
    # Snapshot of Person.photo at pick/sync time. Assigning the FileField
    # shares the stored file (no copy on disk) — deleting the event cleans up
    # the row; the file itself lives on in the directory entry.
    photo = models.ImageField(
        upload_to="people/%Y/%m/", null=True, blank=True,
        help_text="Optional uploaded photo — shown instead of the photo link when present.",
    )
    linked_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="host_profiles",
        help_text="Optional admin login; when signed in, this host sees their own schedule and reminders.",
    )
    is_active = models.BooleanField(
        default=True,
        help_text="Inactive hosts are hidden from visitors; their bookings get flagged for admin attention (FR-2.3).",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(fields=["event", "email"], name="unique_host_per_event"),
        ]

    def __str__(self):
        return f"{self.name} ({self.event.name})"

    @property
    def photo_display_url(self):
        """The photo visitors see: an uploaded file wins over a pasted link."""
        if self.photo and self.photo.name:
            return self.photo.url
        return self.photo_url

    @classmethod
    def create_from_person(cls, event, person):
        """Add a directory person to an event as a host (details copied)."""
        return cls.objects.create(
            event=event,
            person=person,
            name=person.name,
            email=person.email,
            role=person.role,
            photo_url=person.photo_url,
            photo=person.photo,
            linked_user=person.linked_user,
        )


class EventDay(models.Model):
    """Per-day flag board for an event's own dates (the calendar's state).

    ``skipped`` means the organiser is not holding meetings that day at all:
    the day disappears from visitors, new slots cannot be created on it, and
    skipping cancels that day's confirmed bookings with notice. Unskipping
    simply makes the existing slots visible again — nothing is destroyed, so
    the action is always reversible.
    """

    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="day_settings")
    date = models.DateField()
    skipped = models.BooleanField(default=False)
    note = models.CharField(max_length=200, blank=True, default="")

    class Meta:
        ordering = ["date"]
        constraints = [
            models.UniqueConstraint(fields=["event", "date"], name="unique_event_day"),
        ]

    def __str__(self):
        return f"{self.event.name} — {self.date:%Y-%m-%d}{' (skipped)' if self.skipped else ''}"

    @classmethod
    def skipped_dates_for(cls, event):
        """Set of skipped dates — one tiny query, safe to call per request."""
        return set(
            cls.objects.filter(event=event, skipped=True).values_list("date", flat=True)
        )


class Slot(models.Model):
    """A predefined meeting slot owned by a host (FR-3.1 — FR-3.6)."""

    STATUS_AVAILABLE = "available"
    STATUS_BOOKED = "booked"
    STATUS_CANCELLED = "cancelled"
    STATUS_CLOSED = "closed"
    # A break is protected time — lunch, travel, a reset between meetings. It
    # occupies the host's day so nothing can be generated or booked over it,
    # and it never reaches a visitor.
    STATUS_BREAK = "break"
    STATUS_CHOICES = [
        (STATUS_AVAILABLE, "Available"),
        (STATUS_BOOKED, "Booked"),
        (STATUS_CANCELLED, "Cancelled / Reopened"),
        (STATUS_CLOSED, "Closed"),
        (STATUS_BREAK, "Break"),
    ]

    # Mode is NOT chosen per slot — it is a denormalised copy of
    # Event.event_type kept in sync by save(), so downstream code can read it
    # from the slot without another query. Never expose it on a slot form.
    MODE_ONLINE = Event.TYPE_ONLINE
    MODE_OFFLINE = Event.TYPE_OFFLINE
    MODE_CHOICES = [
        (MODE_ONLINE, "Online (video call)"),
        (MODE_OFFLINE, "Offline (at venue)"),
    ]

    PROVIDER_GOOGLE_MEET = Event.PROVIDER_GOOGLE_MEET
    PROVIDER_ZOOM = Event.PROVIDER_ZOOM
    PROVIDER_CHOICES = Event.PROVIDER_CHOICES

    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="slots")
    host = models.ForeignKey(TeamMember, on_delete=models.PROTECT, related_name="slots")
    date = models.DateField()
    start_time = models.TimeField()
    duration_minutes = models.PositiveIntegerField(default=30)
    mode = models.CharField(
        max_length=10, choices=MODE_CHOICES, default=MODE_ONLINE,
        help_text="Mirrors the parent event's type — never set directly.",
    )

    # Online details — manual entry only (FR-3.4, BR-05; no API in v1)
    video_provider = models.CharField(max_length=20, choices=PROVIDER_CHOICES, blank=True, default="")
    meeting_link = models.URLField(blank=True, default="")

    # Offline details — fall back to event-level defaults (FR-3.3)
    venue = models.CharField(max_length=200, blank=True, default="")
    hall_name = models.CharField(max_length=200, blank=True, default="")
    table_name = models.CharField(max_length=200, blank=True, default="")

    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default=STATUS_AVAILABLE)
    capacity = models.PositiveIntegerField(
        default=1, help_text="How many visitors may hold this slot (BR-03; default 1)."
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["date", "start_time"]
        constraints = [
            models.UniqueConstraint(fields=["host", "date", "start_time"], name="unique_host_date_time"),
        ]

    def __str__(self):
        return f"{self.host.name} — {self.date} {self.start_time.strftime('%H:%M')} ({self.get_mode_display()})"

    def _auto_meeting_link(self):
        """A private, deterministic room URL for this slot (online events).

        Room identity is (event, host, date, start time) — exactly the tuple
        the ``unique_host_date_time`` constraint makes unique — so two slots
        can never generate the same room, and the same slot always generates
        the same room. No API, no account: Jitsi rooms materialise on demand
        when the first person opens the link and never expire.
        """
        import re

        def slug(text):
            return re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-") or "x"

        parts = [
            "tdc", slug(self.event.name), self.event.pk,
            self.date.isoformat(), self.start_time.strftime("%H%M"), self.host.pk,
        ]
        return "https://meet.jit.si/" + "-".join(str(p) for p in parts)

    def save(self, *args, **kwargs):
        """Mode always mirrors the parent event's type (no per-slot override).

        Online slots with no link of their own and no event default get a
        private auto-generated room, so every meeting has its own link and
        concurrent meetings never share one.
        """
        if self.event_id:
            self.mode = self.event.event_type
            if self.mode == self.MODE_ONLINE:
                self.venue = self.hall_name = self.table_name = ""
                if not self.meeting_link and not self.event.default_meeting_link:
                    self.meeting_link = self._auto_meeting_link()
                    if not self.video_provider:
                        self.video_provider = Event.PROVIDER_JITSI
            else:
                self.video_provider = self.meeting_link = ""
        update_fields = kwargs.get("update_fields")
        if update_fields is not None:
            kwargs["update_fields"] = list(
                dict.fromkeys(list(update_fields) + ["mode", "venue", "hall_name",
                                                     "table_name", "video_provider", "meeting_link"])
            )
        return super().save(*args, **kwargs)



    # ----- computed helpers -------------------------------------------------
    @property
    def start_datetime(self):
        naive = datetime.datetime.combine(self.date, self.start_time)
        return timezone.make_aware(naive) if timezone.is_naive(naive) else naive

    @property
    def end_datetime(self):
        return self.start_datetime + datetime.timedelta(minutes=self.duration_minutes)

    @property
    def is_break(self):
        return self.status == self.STATUS_BREAK

    @property
    def is_past(self):
        return timezone.now() >= self.start_datetime

    @property
    def _event_or_none(self):
        """The parent event, or None while the slot is still unattached."""
        return self.event if self.event_id else None

    @property
    def effective_provider(self):
        """Slot value wins; otherwise the event-level default (online events)."""
        event = self._event_or_none
        return self.video_provider or (event.default_video_provider if event else "")

    @property
    def effective_provider_display(self):
        return dict(self.PROVIDER_CHOICES).get(self.effective_provider, "")

    @property
    def effective_meeting_link(self):
        event = self._event_or_none
        return self.meeting_link or (event.default_meeting_link if event else "")

    @property
    def effective_venue(self):
        event = self._event_or_none
        return self.venue or (event.venue if event else "")

    @property
    def effective_hall(self):
        event = self._event_or_none
        return self.hall_name or (event.hall_name if event else "")

    @property
    def effective_table(self):
        event = self._event_or_none
        return self.table_name or (event.table_name if event else "")

    def active_bookings_count(self):
        return self.bookings.filter(status="confirmed").count()

    def seats_left(self):
        if self.status == self.STATUS_CLOSED:
            return 0
        return max(self.capacity - self.active_bookings_count(), 0)

    def is_publicly_bookable(self):
        """Can a visitor book this slot right now? (FR-3.4, FR-4.3)"""
        if not self.event.is_publicly_open:
            return False
        if self.status != self.STATUS_AVAILABLE or self.is_past:
            return False
        if self.seats_left() <= 0:
            return False
        if self.date in EventDay.skipped_dates_for(self.event):
            return False  # the organiser is not holding meetings this day
        if self.mode == self.MODE_ONLINE and not (self.effective_provider and self.effective_meeting_link):
            # An online slot only opens for booking once the admin has chosen
            # a provider and pasted the meeting link (FR-3.4).
            return False
        return True

    def _overlapping_slots(self):
        """Non-break slots on this event whose time window touches this one.

        Python-side comparison because each slot carries its own duration.
        """
        overlaps = []
        for other in (
            Slot.objects.filter(event=self.event, date=self.date)
            .exclude(status=Slot.STATUS_BREAK)
            .exclude(pk=self.pk)
            .select_related("host")
        ):
            if other.start_time < self.end_datetime.time() and self.start_time < other.end_datetime.time():
                overlaps.append(other)
        return overlaps

    def clean(self):
        errors = {}
        if not self.event_id:
            # Unattached slot (a form validating before the view assigns the
            # event) — the type-dependent checks run once the event is known.
            return
        self.mode = self.event.event_type
        skipped = EventDay.skipped_dates_for(self.event)
        if self.date in skipped and not self.is_break:
            errors["date"] = (
                f"{self.date:%d %b} is marked as a skipped day — no meetings are "
                "held on it. Open the day again from the day list first."
            )
        if self.is_break:
            # Breaks carry no meeting details: nobody joins them.
            if self.capacity is not None and self.capacity < 1:
                raise ValidationError({"capacity": "Capacity must be at least 1."})
            if errors:
                raise ValidationError(errors)
            return
        if self.mode == self.MODE_ONLINE:
            if not (self.effective_provider and self.effective_meeting_link):
                errors["meeting_link"] = (
                    "Online slots need a provider and a meeting link — set them here, "
                    "or as event defaults, before the slot opens for booking (FR-3.4)."
                )
            elif self.meeting_link:
                # A manually pasted link must not collide with another slot
                # running at the same time on the same link (auto-generated
                # links are unique by construction, so only manual ones check).
                for other in self._overlapping_slots():
                    if other.mode == self.MODE_ONLINE and other.effective_meeting_link == self.effective_meeting_link:
                        errors["meeting_link"] = (
                            f"{other.host.name} already uses this link at "
                            f"{other.start_time.strftime('%H:%M')} — concurrent meetings "
                            "need their own link (or clear the field to get an auto room)."
                        )
                        break
        else:
            if not (self.effective_venue and self.effective_hall and self.effective_table):
                errors["venue"] = "Offline slots require Venue, Hall Name and Table Name (set here or as event defaults) (FR-3.3)."
            else:
                # Two hosts cannot hold meetings at the same physical spot at
                # the same time — the offline equivalent of a shared link.
                mine = (self.effective_venue, self.effective_hall, self.effective_table)
                for other in self._overlapping_slots():
                    if other.mode == self.MODE_OFFLINE:
                        theirs = (other.effective_venue, other.effective_hall, other.effective_table)
                        if theirs == mine:
                            errors["table_name"] = (
                                f"{other.effective_venue} · {other.effective_hall} · "
                                f"{other.effective_table} is already used by {other.host.name} at "
                                f"{other.start_time.strftime('%H:%M')} — pick a free table or move the time."
                            )
                            break
        if self.capacity is not None and self.capacity < 1:
            errors["capacity"] = "Capacity must be at least 1."
        if errors:
            raise ValidationError(errors)

    @classmethod
    def visible_for_host(cls, host):
        """Every upcoming slot for a host — bookable or not (FR-4.3).

        The public page renders unbookable ones struck through so visitors see
        the whole day, not just the gaps. Whole days the organiser skipped are
        hidden entirely — a day with no meetings has nothing to show.
        """
        skipped = EventDay.skipped_dates_for(host.event)
        return [
            s for s in cls.objects.filter(host=host)
            .exclude(status__in=[cls.STATUS_CLOSED, cls.STATUS_BREAK])
            .select_related("event", "host")
            if not s.is_past and s.date not in skipped
        ]

    @classmethod
    def public_for_event(cls, event):
        """All publicly bookable slots across an event (used by the manage link)."""
        return [s for s in cls.objects.filter(event=event, status=cls.STATUS_AVAILABLE).select_related("event", "host") if s.is_publicly_bookable()]

