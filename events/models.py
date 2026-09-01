"""
Events app models (SRS section 5 data model + FR-1, FR-2, FR-3).

Event        — time-boxed event with public booking link (FR-1, FR-4.1)
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
    PROVIDER_CHOICES = [
        (PROVIDER_GOOGLE_MEET, "Google Meet"),
        (PROVIDER_ZOOM, "Zoom"),
    ]

    name = models.CharField(max_length=200)
    description = models.TextField(blank=True, default="")
    start_date = models.DateField(help_text="First day the event is active.")
    duration_days = models.PositiveIntegerField(
        default=1, help_text="Number of days the event stays live (auto end date)."
    )
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=STATUS_DRAFT)
    public_slug = models.SlugField(max_length=80, unique=True, blank=True, null=True)

    event_type = models.CharField(
        max_length=10,
        choices=TYPE_CHOICES,
        default=TYPE_ONLINE,
        help_text="Set once at creation; locked as soon as the event has slots.",
    )

    # Event-level ONLINE defaults — used by every slot unless the slot
    # overrides the link (online events only).
    default_video_provider = models.CharField(
        max_length=20, choices=PROVIDER_CHOICES, blank=True, default="",
        help_text="Provider label shown to visitors (Google Meet or Zoom).",
    )
    default_meeting_link = models.URLField(
        blank=True, default="",
        help_text="Meeting link created manually in Meet/Zoom; slots inherit it unless they set their own.",
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

class TeamMember(models.Model):
    """Host / team member attached to one event (FR-2.1, FR-2.2)."""

    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="team_members")
    name = models.CharField(max_length=200)
    email = models.EmailField()
    role = models.CharField(max_length=120, blank=True, default="", help_text="Optional title shown to visitors.")
    photo_url = models.URLField(blank=True, default="", help_text="Optional photo shown on the roster (FR-4.2).")
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

    def save(self, *args, **kwargs):
        """Mode always mirrors the parent event's type (no per-slot override)."""
        if self.event_id:
            self.mode = self.event.event_type
            if self.mode == self.MODE_ONLINE:
                self.venue = self.hall_name = self.table_name = ""
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
        if self.mode == self.MODE_ONLINE and not (self.effective_provider and self.effective_meeting_link):
            # An online slot only opens for booking once the admin has chosen
            # a provider and pasted the meeting link (FR-3.4).
            return False
        return True

    def clean(self):
        errors = {}
        if not self.event_id:
            # Unattached slot (a form validating before the view assigns the
            # event) — the type-dependent checks run once the event is known.
            return
        self.mode = self.event.event_type
        if self.is_break:
            # Breaks carry no meeting details: nobody joins them.
            if self.capacity is not None and self.capacity < 1:
                raise ValidationError({"capacity": "Capacity must be at least 1."})
            return
        if self.mode == self.MODE_ONLINE:
            if not (self.effective_provider and self.effective_meeting_link):
                errors["meeting_link"] = (
                    "Online slots need a provider and a meeting link — set them here, "
                    "or as event defaults, before the slot opens for booking (FR-3.4)."
                )
        else:
            if not (self.effective_venue and self.effective_hall and self.effective_table):
                errors["venue"] = "Offline slots require Venue, Hall Name and Table Name (set here or as event defaults) (FR-3.3)."
        if self.capacity is not None and self.capacity < 1:
            errors["capacity"] = "Capacity must be at least 1."
        if errors:
            raise ValidationError(errors)

    @classmethod
    def visible_for_host(cls, host):
        """Every upcoming slot for a host — bookable or not (FR-4.3).

        The public page renders unbookable ones struck through so visitors see
        the whole day, not just the gaps.
        """
        return [
            s for s in cls.objects.filter(host=host)
            .exclude(status__in=[cls.STATUS_CLOSED, cls.STATUS_BREAK])
            .select_related("event", "host")
            if not s.is_past
        ]

    @classmethod
    def public_for_event(cls, event):
        """All publicly bookable slots across an event (used by the manage link)."""
        return [s for s in cls.objects.filter(event=event, status=cls.STATUS_AVAILABLE).select_related("event", "host") if s.is_publicly_bookable()]

