"""Admin-facing forms for events, team members and slots (FR-1 — FR-3).

Event type drives everything here. An event is strictly online **or** offline,
chosen once at creation, and the type-irrelevant fields are *removed* from the
form (not merely hidden or blanked), so they can never be submitted, validated
or displayed for the wrong type.
"""
import datetime

from django import forms
from django.core.exceptions import ValidationError

from .models import Event, Slot, TeamMember

ONLINE_FIELDS = ["default_video_provider", "default_meeting_link"]
OFFLINE_FIELDS = ["venue", "hall_name", "table_name"]


class EventForm(forms.ModelForm):
    """Branches on event type; irrelevant fields are dropped entirely.

    ``event_type`` is passed in by the view (from the creation chooser, or from
    the instance when editing). When the event already has slots the field is
    removed from the form altogether — the type is immutable at that point and
    the template explains why.
    """

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
            "default_meeting_link": "Meeting link",
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
                "Shown to visitors as the meeting provider. Links are created manually in Meet or Zoom."
            )
            self.fields["default_meeting_link"].required = True
        else:
            for name in OFFLINE_FIELDS:
                self.fields[name].required = True
            self.fields["venue"].help_text = "Default for every slot; a slot may override it."

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
        days = cleaned.get("duration_days")
        if start and days:
            cleaned["end_date_hint"] = start + datetime.timedelta(days=days - 1)
        return cleaned

    def save(self, commit=True):
        event = super().save(commit=False)
        event.event_type = self.event_type
        # Belt and braces: the other type's columns never keep stale data.
        if event.event_type == Event.TYPE_ONLINE:
            event.venue = event.hall_name = event.table_name = ""
        else:
            event.default_video_provider = event.default_meeting_link = ""
        if commit:
            event.save()
        return event


class TeamMemberForm(forms.ModelForm):
    class Meta:
        model = TeamMember
        fields = ["name", "email", "role", "photo_url", "linked_user", "is_active"]
        widgets = {"photo_url": forms.URLInput(attrs={"placeholder": "https://... (optional)"})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["linked_user"].required = False
        self.fields["linked_user"].queryset = self.fields["linked_user"].queryset.filter(
            is_active=True
        ).order_by("username")
        self.fields["linked_user"].help_text = "Optional login — lets this host see their own schedule & reminders."


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
            self.fields["video_provider"].help_text = "Leave blank to use the event default."
            self.fields["meeting_link"].help_text = "Leave blank to use the event default link."
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
            if not (cleaned.get("video_provider") or event.default_video_provider):
                self.add_error("video_provider", "Required — this event has no default provider yet.")
            if not (cleaned.get("meeting_link") or event.default_meeting_link):
                self.add_error("meeting_link", "Required — this event has no default link yet.")
        else:
            for name in OFFLINE_FIELDS:
                if not (cleaned.get(name) or getattr(event, name)):
                    self.add_error(name, "Required — this event has no default set.")
        return cleaned

    # Status is set in clean(); save() needs no special handling.
