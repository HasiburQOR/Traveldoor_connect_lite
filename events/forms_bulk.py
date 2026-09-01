"""Bulk slot generation — helps "full event setup in under 15 minutes" (BRD 9)."""
import datetime

from django import forms


class BulkSlotForm(forms.Form):
    """Fill a date range with back-to-back slots for every active host.

    The form is event-aware: it pre-fills the event's own date window and a
    normal working day, and constrains the date pickers to that window. Getting
    the defaults right matters — an empty time input makes the browser offer
    "now", which is how you end up generating a window that ends before it
    starts.
    """

    date_from = forms.DateField(
        label="First day",
        widget=forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
    )
    date_to = forms.DateField(
        label="Last day",
        widget=forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
    )
    start_time = forms.TimeField(
        label="Day starts at",
        widget=forms.TimeInput(attrs={"type": "time"}, format="%H:%M"),
    )
    end_time = forms.TimeField(
        label="Day ends at",
        widget=forms.TimeInput(attrs={"type": "time"}, format="%H:%M"),
        help_text="The last slot finishes by this time.",
    )
    duration_minutes = forms.IntegerField(
        label="Each slot lasts", min_value=5, max_value=480, initial=30,
        widget=forms.NumberInput(attrs={"step": 5}), help_text="Minutes.",
    )
    capacity = forms.IntegerField(
        label="Visitors per slot", min_value=1, initial=1,
        help_text="Usually 1 — raise it for group sessions.",
    )
    gap_minutes = forms.IntegerField(
        label="Gap between slots", min_value=0, max_value=240, initial=0, required=False,
        widget=forms.NumberInput(attrs={"step": 5}),
        help_text="Minutes of breathing room after each meeting. 0 for back-to-back.",
    )
    break_start = forms.TimeField(
        label="Break starts", required=False,
        widget=forms.TimeInput(attrs={"type": "time"}, format="%H:%M"),
        help_text="Optional — e.g. lunch. Leave blank for no break.",
    )
    break_end = forms.TimeField(
        label="Break ends", required=False,
        widget=forms.TimeInput(attrs={"type": "time"}, format="%H:%M"),
    )
    mark_break = forms.BooleanField(
        label="Show the break in the schedule", required=False, initial=True,
        help_text="Adds a non-bookable “Break” entry so the gap is visible to you and to hosts.",
    )
    # No mode field: generated slots inherit the parent event's type, exactly
    # like every other slot (an event is strictly one type).

    def __init__(self, *args, event=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.event = event
        if event is None:
            return
        # Default to the event's own window and a standard working day.
        self.fields["date_from"].initial = event.start_date
        self.fields["date_to"].initial = event.end_date
        self.fields["start_time"].initial = datetime.time(9, 0)
        self.fields["end_time"].initial = datetime.time(17, 0)
        window = {"min": event.start_date.isoformat(), "max": event.end_date.isoformat()}
        self.fields["date_from"].widget.attrs.update(window)
        self.fields["date_to"].widget.attrs.update(window)
        self.fields["date_from"].help_text = (
            f"The event runs {event.start_date:%d %b} – {event.end_date:%d %b}."
        )

    def clean(self):
        cleaned = super().clean()
        date_from, date_to = cleaned.get("date_from"), cleaned.get("date_to")
        start, end = cleaned.get("start_time"), cleaned.get("end_time")

        # Attach each problem to the field that caused it, so the message
        # appears under the input the admin needs to change.
        if date_from and date_to and date_to < date_from:
            self.add_error("date_to", "The last day can't be before the first day.")
        if start and end and end <= start:
            self.add_error(
                "end_time",
                f"The day has to end after it starts — you set it to end at "
                f"{end:%H:%M}, which is before {start:%H:%M}.",
            )
        break_start, break_end = cleaned.get("break_start"), cleaned.get("break_end")
        if break_start and not break_end:
            self.add_error("break_end", "Tell us when the break finishes.")
        if break_end and not break_start:
            self.add_error("break_start", "Tell us when the break starts.")
        if break_start and break_end:
            if break_end <= break_start:
                self.add_error("break_end", "The break has to end after it starts.")
            elif start and end and (break_start < start or break_end > end):
                self.add_error("break_start", (
                    f"The break has to sit inside the day "
                    f"({start:%H:%M}–{end:%H:%M})."
                ))

        if start and end and end > start:
            minutes = cleaned.get("duration_minutes")
            if minutes:
                span = (
                    datetime.datetime.combine(datetime.date.today(), end)
                    - datetime.datetime.combine(datetime.date.today(), start)
                ).total_seconds() / 60
                if span < minutes:
                    self.add_error(
                        "duration_minutes",
                        f"A {int(minutes)}-minute slot doesn't fit in a "
                        f"{int(span)}-minute day. Shorten the slot or widen the window.",
                    )

        if self.event is not None:
            if date_from and date_from < self.event.start_date:
                self.add_error("date_from", (
                    f"The event doesn't start until "
                    f"{self.event.start_date:%d %b %Y}."
                ))
            if date_to and date_to > self.event.end_date:
                self.add_error("date_to", (
                    f"The event finishes on {self.event.end_date:%d %b %Y}."
                ))
        return cleaned

    def generate_times(self):
        """Slot start times for one day, skipping anything that hits the break.

        A slot is dropped when it overlaps the break window at all — a meeting
        that runs into lunch is no more usable than one held during it.
        """
        today = datetime.date.today()
        start = datetime.datetime.combine(today, self.cleaned_data["start_time"])
        end = datetime.datetime.combine(today, self.cleaned_data["end_time"])
        length = datetime.timedelta(minutes=self.cleaned_data["duration_minutes"])
        step = length + datetime.timedelta(minutes=self.cleaned_data.get("gap_minutes") or 0)

        break_start = self.cleaned_data.get("break_start")
        break_end = self.cleaned_data.get("break_end")
        gap_from = datetime.datetime.combine(today, break_start) if break_start else None
        gap_to = datetime.datetime.combine(today, break_end) if break_end else None

        times = []
        current = start
        while current + length <= end:
            overlaps_break = (
                gap_from is not None and current < gap_to and current + length > gap_from
            )
            if not overlaps_break:
                times.append(current.time())
                current += step
            else:
                # Resume at the end of the break rather than stepping through it.
                current = gap_to
        return times

    def break_window(self):
        """The break as (start, end), or None when no break was set."""
        start = self.cleaned_data.get("break_start")
        end = self.cleaned_data.get("break_end")
        if start and end and self.cleaned_data.get("mark_break"):
            return start, end
        return None

    def generate_dates(self):
        dates = []
        current = self.cleaned_data["date_from"]
        while current <= self.cleaned_data["date_to"]:
            dates.append(current)
            current += datetime.timedelta(days=1)
        return dates
