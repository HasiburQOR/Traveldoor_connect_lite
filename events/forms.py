"""Admin-facing forms for events, team members and slots (FR-1 — FR-3).

Event type drives everything here. An event is strictly online **or** offline,
chosen once at creation, and the type-irrelevant fields are *removed* from the
form (not merely hidden or blanked), so they can never be submitted, validated
or displayed for the wrong type.
"""
import datetime

from django import forms
from django.core.exceptions import ValidationError

from .models import Event, Person, Slot, TeamMember

ONLINE_FIELDS = ["default_video_provider", "default_meeting_link"]
OFFLINE_FIELDS = ["venue", "hall_name", "table_name"]


class PhotoUploadFormMixin:
    """Shared guard for the optional ``photo`` upload on Person/TeamMember.

    Keeps host photos small enough that a gunicorn-served ``/media/`` stays
    quick and uploads on venue Wi-Fi don't time out. Pillow still verifies the
    bytes are a real image (ImageField) before this size check runs.
    """

    MAX_PHOTO_MB = 5

    def clean_photo(self):
        photo = self.cleaned_data.get("photo")
        if photo and hasattr(photo, "size") and photo.size > self.MAX_PHOTO_MB * 1024 * 1024:
            raise ValidationError(
                f"Keep photos under {self.MAX_PHOTO_MB} MB — resize or compress the image first."
            )
        return photo


class EventForm(forms.ModelForm):
    """Branches on event type; irrelevant fields are dropped entirely.

    ``event_type`` is passed in by the view (from the creation chooser, or from
    the instance when editing). When the event already has slots the field is
    removed from the form altogether — the type is immutable at that point and
    the template explains why.

    The schedule itself is customisable: the admin either gives a start date
    plus a day count, or a start and end date (range mode — the count is then
    derived), and ticks which weekdays the event actually runs on. Unticked
    weekdays inside the window (weekends, typically) become free days. The
    template renders these fields as one "schedule" block with a live
    breakdown of the resulting dates.
    """

    MODE_COUNT = "count"
    MODE_RANGE = "range"
    MODE_CHOICES = [
        (MODE_COUNT, "Count days — start date + number of days"),
        (MODE_RANGE, "Date range — start date + last date"),
    ]

    schedule_mode = forms.ChoiceField(
        label="How long does it run?",
        choices=MODE_CHOICES,
        initial=MODE_COUNT,
        required=False,  # older callers post without the schedule block at all
        widget=forms.RadioSelect,
    )
    end_date_input = forms.DateField(
        label="Last day",
        required=False,
        widget=forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
        help_text="The event runs up to and including this day.",
    )
    # One checkbox per weekday, 0=Monday … 6=Sunday (matches date.weekday()).
    weekday_0 = forms.BooleanField(label="Mon", required=False, initial=True)
    weekday_1 = forms.BooleanField(label="Tue", required=False, initial=True)
    weekday_2 = forms.BooleanField(label="Wed", required=False, initial=True)
    weekday_3 = forms.BooleanField(label="Thu", required=False, initial=True)
    weekday_4 = forms.BooleanField(label="Fri", required=False, initial=True)
    weekday_5 = forms.BooleanField(label="Sat", required=False, initial=True)
    weekday_6 = forms.BooleanField(label="Sun", required=False, initial=True)

    SCHEDULE_FIELD_NAMES = {
        "schedule_mode", "start_date", "duration_days", "end_date_input",
        "weekday_0", "weekday_1", "weekday_2", "weekday_3",
        "weekday_4", "weekday_5", "weekday_6",
    }

    class Meta:
        model = Event
        fields = [
            # event_type is deliberately absent: it is never a form input.
            # The view supplies it and save() writes it.
            "name", "description", "start_date", "duration_days",
            "default_video_provider", "default_meeting_link",
            "venue", "hall_name", "table_name",
            "reminder_hours_csv", "inapp_lead_minutes", "same_day_summary",
            "require_phone", "require_company",
        ]
        widgets = {
            "start_date": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
            "description": forms.Textarea(attrs={"rows": 3}),
            "reminder_hours_csv": forms.TextInput(attrs={"placeholder": "24,1"}),
            "default_meeting_link": forms.URLInput(
                attrs={"placeholder": "https://meet.google.com/... or https://zoom.us/j/..."}
            ),
        }
        labels = {
            "default_video_provider": "Meeting provider",
            "default_meeting_link": "Shared meeting link (optional)",
            "venue": "Venue",
            "hall_name": "Hall name",
            "table_name": "Table name",
        }

    def __init__(self, *args, event_type=None, lock_type=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.event_type = event_type or getattr(self.instance, "event_type", None) or Event.TYPE_ONLINE
        self.type_locked = lock_type
        # The type is fixed for the life of this form — carry it on the instance
        # so model validation and save() see the right value.
        self.instance.event_type = self.event_type

        drop = OFFLINE_FIELDS if self.event_type == Event.TYPE_ONLINE else ONLINE_FIELDS
        for name in drop:
            self.fields.pop(name, None)

        if self.event_type == Event.TYPE_ONLINE:
            self.fields["default_video_provider"].required = True
            self.fields["default_video_provider"].help_text = (
                "The label visitors see when a slot doesn't name its own. "
                "Rooms are auto-generated per slot by default."
            )
            self.fields["default_meeting_link"].help_text = (
                "Leave blank and every slot gets its own private auto-generated "
                "video room. Paste a link only when all meetings should join the "
                "same room (e.g. one webinar) — any slot may still override it."
            )
        else:
            for name in OFFLINE_FIELDS:
                self.fields[name].required = True
            self.fields["venue"].help_text = "Default for every slot; a slot may override it."

        # --- schedule block (start date / count or range / weekdays) --------
        instance = self.instance
        stored = instance.weekday_set() if instance.pk else set(range(7))
        for day in range(7):
            self.fields[f"weekday_{day}"].initial = day in stored
        if instance.pk and instance.start_date:
            self.fields["end_date_input"].initial = instance.end_date
        self.fields["start_date"].label = "First day"
        self.fields["start_date"].help_text = "The event's first active day."
        self.fields["duration_days"].label = "How many days"
        self.fields["duration_days"].help_text = (
            "Calendar days from the first to the last date — weekends inside the "
            "range stay free unless you tick them below."
        )

    # The schedule block posts this marker so a POST without the block at all
    # (older callers, scripts) is read as "every day" rather than "no days".
    SCHEDULE_MARKER = "schedule_present"

    def _selected_weekdays(self):
        """Weekday ints currently ticked, honouring POST data or initials."""
        if not self.is_bound:
            return [d for d in range(7) if self[f"weekday_{d}"].initial]
        if self.add_prefix(self.SCHEDULE_MARKER) not in self.data:
            return list(range(7))
        return [d for d in range(7) if self.data.get(self.add_prefix(f"weekday_{d}"))]

    def _schedule_date(self, name):
        value = self[name].value()
        if isinstance(value, str):
            try:
                return datetime.date.fromisoformat(value)
            except ValueError:
                return None
        return value

    def _schedule_days(self):
        try:
            return int(self["duration_days"].value())
        except (TypeError, ValueError):
            return None

    def _schedule_mode(self):
        if self.is_bound:
            return self.data.get(self.add_prefix("schedule_mode")) or self.MODE_COUNT
        return self["schedule_mode"].initial or self.MODE_COUNT

    @property
    def other_fields(self):
        """Visible fields outside the schedule block (rendered generically)."""
        return [f for f in self.visible_fields() if f.name not in self.SCHEDULE_FIELD_NAMES]

    @property
    def weekday_fields(self):
        """The seven weekday checkboxes, Monday first, for the schedule block."""
        return [self[f"weekday_{d}"] for d in range(7)]

    def schedule_preview(self):
        """The dates the current schedule selections produce.

        Rendered by the template on load and after a rejected submit, and kept
        in sync live by the page's script — the no-JS path shows exactly the
        same breakdown, just only after a page load.
        """
        start = self._schedule_date("start_date")
        end_input = self._schedule_date("end_date_input")
        days = self._schedule_days()
        mode = self._schedule_mode()
        span_end = None
        if mode == self.MODE_RANGE and end_input:
            span_end = end_input if (not start or end_input >= start) else None
        elif start and days:
            span_end = start + datetime.timedelta(days=days - 1)

        weekdays = self._selected_weekdays()
        dates, free_runs, run = [], [], []
        if start and span_end and weekdays:
            current = start
            while current <= span_end:
                if current.weekday() in weekdays:
                    dates.append(current)
                    if run:
                        free_runs.append((run[0], run[-1]))
                        run = []
                elif current.weekday() >= 4:  # only Fri–Sun gaps earn a mention
                    run.append(current)
                else:
                    if run:
                        free_runs.append((run[0], run[-1]))
                        run = []
                current += datetime.timedelta(days=1)
            if run:
                free_runs.append((run[0], run[-1]))
        free_text = ", ".join(
            f"{a:%d %b}" if a == b else f"{a:%d %b}–{b:%d %b}" for a, b in free_runs
        )
        return {
            "dates": dates,
            "chips": dates[:42],
            "more": max(len(dates) - 42, 0),
            "free": free_text,
            "none_match": bool(start and span_end and weekdays and not dates),
            "no_weekday": not weekdays,
        }

    def clean_reminder_hours_csv(self):
        raw = self.cleaned_data.get("reminder_hours_csv", "")
        if not raw:
            return raw
        for part in raw.split(","):
            part = part.strip()
            if not part:
                continue
            if not part.isdigit() or int(part) < 1:
                raise ValidationError("Enter positive whole hours, comma separated — e.g. 24,1")
        return raw

    def clean(self):
        cleaned = super().clean()
        start = cleaned.get("start_date")
        mode = cleaned.get("schedule_mode") or self.MODE_COUNT
        days = cleaned.get("duration_days")
        end_input = cleaned.get("end_date_input")

        # Resolve the span: range mode derives the day count from the two dates.
        if mode == self.MODE_RANGE:
            if not end_input:
                self.add_error("end_date_input", "Pick the last day of the event.")
            elif start and end_input < start:
                self.add_error("end_date_input", "The last day can't be before the first day.")
            elif start:
                days = (end_input - start).days + 1
                if days > Event.MAX_SPAN_DAYS:
                    self.add_error("end_date_input", (
                        f"That range is {days} days — keep events within "
                        f"{Event.MAX_SPAN_DAYS} days and split longer runs."
                    ))
                else:
                    cleaned["duration_days"] = days
        elif days and days > Event.MAX_SPAN_DAYS:
            self.add_error("duration_days", (
                f"Keep an event within {Event.MAX_SPAN_DAYS} days — split longer runs into separate events."
            ))

        # Weekday selection: all seven ticked is the canonical "every day",
        # stored blank so pre-existing events stay byte-for-byte identical.
        weekdays = self._selected_weekdays()
        if self.is_bound and self.add_prefix(self.SCHEDULE_MARKER) in self.data and not weekdays:
            self.add_error(None, "Tick at least one day of the week for the event to run on.")
        else:
            cleaned["active_weekdays"] = (
                ",".join(str(d) for d in weekdays) if 0 < len(weekdays) < 7 else ""
            )

        if start and days and 0 < len(weekdays) < 7:
            span_end = start + datetime.timedelta(days=days - 1)
            has_match = any(
                (start + datetime.timedelta(days=i)).weekday() in weekdays
                for i in range(min(days, Event.MAX_SPAN_DAYS))
            )
            if not has_match:
                self.add_error(None, (
                    f"None of the selected weekdays fall between "
                    f"{start:%d %b %Y} and {span_end:%d %b %Y} — pick other days or widen the window."
                ))

        if start and days:
            cleaned["end_date_hint"] = start + datetime.timedelta(days=days - 1)
        return cleaned

    def save(self, commit=True):
        event = super().save(commit=False)
        event.event_type = self.event_type
        event.active_weekdays = self.cleaned_data.get("active_weekdays", "")
        # Belt and braces: the other type's columns never keep stale data.
        if event.event_type == Event.TYPE_ONLINE:
            event.venue = event.hall_name = event.table_name = ""
        else:
            event.default_video_provider = event.default_meeting_link = ""
        if commit:
            event.save()
        return event


class TeamMemberForm(PhotoUploadFormMixin, forms.ModelForm):
    class Meta:
        model = TeamMember
        fields = ["name", "email", "role", "photo_url", "photo", "linked_user", "is_active"]
        widgets = {
            "photo_url": forms.URLInput(attrs={"placeholder": "https://... (optional)"}),
            "photo": forms.ClearableFileInput(attrs={"accept": "image/*"}),
        }
        labels = {"photo_url": "Photo link", "photo": "Photo upload"}
        help_texts = {"photo": "Optional, up to 5 MB. Overrides the photo link when set."}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["linked_user"].required = False
        self.fields["linked_user"].queryset = self.fields["linked_user"].queryset.filter(
            is_active=True
        ).order_by("username")
        self.fields["linked_user"].help_text = "Optional login — lets this host see their own schedule & reminders."


class PersonForm(PhotoUploadFormMixin, forms.ModelForm):
    """Directory entry holding the details a host needs on an event roster.

    On edit, ``sync_hosts`` re-applies the (possibly changed) details to
    every event where this person is an active host, so fixing a typo or a
    job title in the directory does not leave stale host cards behind.
    Event-specific edits made via the event's host form are overwritten by
    a sync — the checkbox says so explicitly.
    """

    sync_hosts = forms.BooleanField(
        label="Also update hosts already added to events",
        required=False,
        initial=True,
        help_text="Re-applies this person's name, email, role, photo and login to every event "
                  "where they are an active host. Per-event edits are overwritten.",
    )

    class Meta:
        model = Person
        fields = ["name", "email", "role", "photo_url", "photo", "linked_user", "is_active"]
        widgets = {
            "photo_url": forms.URLInput(attrs={"placeholder": "https://... (optional)"}),
            "photo": forms.ClearableFileInput(attrs={"accept": "image/*"}),
        }
        labels = {"photo_url": "Photo link", "photo": "Photo upload"}
        help_texts = {"photo": "Optional, up to 5 MB. Overrides the photo link when set."}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Only active logins can be linked, but keep the one already linked
        # selectable so editing a person whose login was later deactivated
        # doesn't fail validation until the login is deliberately changed.
        user_qs = self.fields["linked_user"].queryset.filter(is_active=True)
        if self.instance.pk and self.instance.linked_user_id:
            user_qs = user_qs | self.fields["linked_user"].queryset.filter(
                pk=self.instance.linked_user_id)
        self.fields["linked_user"].queryset = user_qs.order_by("username")
        self.fields["linked_user"].required = False
        self.fields["linked_user"].help_text = "Optional login — lets this host see their own schedule & reminders."
        self.fields["is_active"].label = "Available as a host"
        if not self.instance.pk:
            self.fields.pop("sync_hosts")  # nothing to sync on a brand-new person


class PersonMultipleChoiceField(forms.ModelMultipleChoiceField):
    """Checkbox picker whose labels carry the person's role and email."""

    def label_from_instance(self, obj):
        label = obj.name
        if obj.role:
            label += f" — {obj.role}"
        return f"{label} ({obj.email})"


class HostPickForm(forms.Form):
    """Pick people from the directory to add as hosts on one event (FR-2.1).

    Only available people who are not already hosting this event (by
    email) are offered, so the picker itself enforces the one-host-per-
    email-per-event rule.
    """

    people = PersonMultipleChoiceField(
        queryset=None,
        widget=forms.CheckboxSelectMultiple,
        label="People to add as hosts",
        help_text="Their details are copied from the People directory onto this event.",
        error_messages={"required": "Tick at least one person to add as a host."},
    )

    def __init__(self, *args, event=None, **kwargs):
        super().__init__(*args, **kwargs)
        queryset = Person.objects.filter(is_active=True).order_by("name")
        if event is not None:
            queryset = queryset.exclude(email__in=event.team_members.values("email"))
        self.fields["people"].queryset = queryset


class SlotForm(forms.ModelForm):
    """Slot details for one host.

    There is no ``mode`` field: the slot inherits the event's type. Only the
    fields that type actually needs are built, so an offline event's slot form
    has no link field anywhere, and an online event's has no venue fields.
    """

    class Meta:
        model = Slot
        fields = [
            "host", "date", "start_time", "duration_minutes", "capacity",
            "video_provider", "meeting_link", "venue", "hall_name", "table_name",
        ]
        widgets = {
            "date": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
            "start_time": forms.TimeInput(attrs={"type": "time"}, format="%H:%M"),
            "meeting_link": forms.URLInput(
                attrs={"placeholder": "https://meet.google.com/... or https://zoom.us/j/..."}
            ),
        }
        labels = {
            "video_provider": "Meeting provider",
            "meeting_link": "Meeting link",
            "venue": "Venue",
            "hall_name": "Hall name",
            "table_name": "Table name",
        }

    is_break = forms.BooleanField(
        label="This is a break, not a bookable meeting",
        required=False,
        help_text="Blocks the time in the host's day. Visitors never see it.",
    )

    def __init__(self, *args, event=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.event = event or (self.instance.event if self.instance.event_id else None)
        self.fields["is_break"].initial = self.instance.is_break if self.instance.pk else False
        if self.event is not None:
            # Attach the event up front: model validation runs during
            # _post_clean, before the view gets a chance to set it, and the
            # type-dependent checks need to know which event this slot is on.
            self.instance.event = self.event
            self.fields["host"].queryset = TeamMember.objects.filter(event=self.event).order_by("name")
        self.event_type = getattr(self.event, "event_type", Event.TYPE_ONLINE)
        drop = OFFLINE_FIELDS if self.event_type == Event.TYPE_ONLINE else ["video_provider", "meeting_link"]
        for name in drop:
            self.fields.pop(name, None)
        if self.event_type == Event.TYPE_ONLINE:
            self.fields["video_provider"].required = False
            self.fields["video_provider"].help_text = (
                "Leave blank to use the event default — or the auto-generated "
                "room when there is none."
            )
            self.fields["meeting_link"].help_text = (
                "Leave blank and this slot gets its own private auto-generated "
                "room — or the event's shared link, when one is set. Paste a "
                "Meet/Zoom link to use your own room instead."
            )
        else:
            for name in OFFLINE_FIELDS:
                self.fields[name].help_text = "Leave blank to use the event default."

    def clean(self):
        cleaned = super().clean()
        event = self.event
        # Decide the status here, not in save(): model validation runs in
        # _post_clean() straight after this, and it needs to know that a break
        # is exempt from the meeting-detail requirements.
        if cleaned.get("is_break"):
            self.instance.status = Slot.STATUS_BREAK
        elif self.instance.status == Slot.STATUS_BREAK:
            # Unticked on an existing break — hand the time back to visitors.
            self.instance.status = Slot.STATUS_AVAILABLE
        if event is None or cleaned.get("is_break"):
            # Nobody joins a break, so it needs neither a link nor a venue.
            return cleaned
        if event.event_type == Event.TYPE_ONLINE:
            if not (cleaned.get("meeting_link") or event.default_meeting_link):
                # No link of its own and no event default: this slot gets its
                # own private auto-generated room (Jitsi). Materialise it here
                # rather than only in save() — model validation and the
                # concurrent-link check need a concrete link to work with. The
                # room is deterministic, so clearing the field on an existing
                # slot regenerates the same room.
                if cleaned.get("date") and cleaned.get("start_time") and cleaned.get("host"):
                    # clean() runs before construct_instance() copies
                    # cleaned_data onto the instance, so put the values the
                    # generator reads onto the instance first.
                    self.instance.date = cleaned["date"]
                    self.instance.start_time = cleaned["start_time"]
                    self.instance.host = cleaned["host"]
                    cleaned["meeting_link"] = self.instance._auto_meeting_link()
                    if not cleaned.get("video_provider"):
                        cleaned["video_provider"] = Event.PROVIDER_JITSI
                # A missing date/time/host has already raised its own field
                # error — there is nothing to generate a room from.
            if not (cleaned.get("video_provider") or event.default_video_provider):
                self.add_error("video_provider", "Required — this event has no default provider yet.")
        else:
            for name in OFFLINE_FIELDS:
                if not (cleaned.get(name) or getattr(event, name)):
                    self.add_error(name, "Required — this event has no default set.")
        return cleaned

    # Status is set in clean(); save() needs no special handling.
