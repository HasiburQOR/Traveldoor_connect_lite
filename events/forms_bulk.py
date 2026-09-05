"""Day-based slot builder — helps "full event setup in under 15 minutes" (BRD 9).

Replaces the old date-range bulk generator. Instead of typing a date window,
the admin ticks the actual days — the options are derived from the event's own
calendar (``Event.event_dates()``), so free weekdays never appear and the
builder is always in sync with the event's dates. One meeting pattern is set
once, optionally for a single host, with any number of break windows.
"""
import datetime

from django import forms

from .models import EventDay, TeamMember


class SlotBuilderForm(forms.Form):
    """Build one day pattern and stamp it onto the chosen event days.

    * ``host`` — blank means "Every active host": the whole team gets the same
      pattern, so building the schedule stays a one-screen job.
    * ``days`` — checkboxes generated from the event's own dates. This is the
      auto-sync with the event schedule: change the event's dates or weekday
      selection and the builder's options change with it. A day that isn't an
      event day can't even be submitted, let alone built.
    * ``break_start_1``/``break_end_1`` … ``break_start_10``/``break_end_10`` —
      up to ``MAX_BREAKS`` break windows. Blank rows are ignored, so the
      template can always render all ten and hide the unused ones with JS.
    """

    MAX_BREAKS = 10

    host = forms.ModelChoiceField(
        queryset=TeamMember.objects.none(), required=False,
        label="Host",
        help_text="Leave on “Every active host” to give the whole team the same pattern.",
    )
    days = forms.MultipleChoiceField(
        widget=forms.CheckboxSelectMultiple,
        label="Event days",
        error_messages={"required": "Tick at least one day."},
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
    mark_breaks = forms.BooleanField(
        label="Show breaks in the schedule", required=False, initial=True,
        help_text="Adds a non-bookable “Break” entry for each window so the gap is visible to you and to hosts.",
    )
    # No mode field: built slots inherit the parent event's type, exactly
    # like every other slot (an event is strictly one type).

    def __init__(self, *args, event=None, host=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.event = event
        self.dates = list(event.event_dates()) if event is not None else []
        if event is not None:
            # Days the organiser marked as skipped hold no meetings — they are
            # not offered here, so a pattern can never be stamped onto them.
            skipped = EventDay.skipped_dates_for(event)
            self.dates = [d for d in self.dates if d not in skipped]

        self.fields["days"].choices = [
            (d.isoformat(), f"{d:%a %d %b}") for d in self.dates
        ]
        if event is not None:
            self.fields["host"].queryset = event.team_members.filter(is_active=True)
            self.fields["host"].empty_label = "Every active host"
            # Default to a standard working day — an empty time input makes the
            # browser offer "now", which is how you end up with days that end
            # before they start.
            self.fields["start_time"].initial = datetime.time(9, 0)
            self.fields["end_time"].initial = datetime.time(17, 0)
            self.fields["days"].help_text = (
                f"Only this event's days are listed — it runs "
                f"{event.start_date:%d %b} – {event.last_event_date:%d %b}"
                + (f", {event.weekday_summary} only." if event.runs_selected_days else ".")
            )
        if host is not None:
            self.fields["host"].initial = host  # the per-host "Schedule" shortcut

        for i in range(1, self.MAX_BREAKS + 1):
            self.fields[f"break_start_{i}"] = forms.TimeField(
                required=False, label=f"Break {i} starts",
                widget=forms.TimeInput(attrs={"type": "time"}, format="%H:%M"),
            )
            self.fields[f"break_end_{i}"] = forms.TimeField(
                required=False, label=f"Break {i} ends",
                widget=forms.TimeInput(attrs={"type": "time"}, format="%H:%M"),
            )

    # ----- template helpers -------------------------------------------------
    def break_rows(self):
        """Rows for the template: all MAX_BREAKS; filled ones stay visible."""
        rows = []
        for i in range(1, self.MAX_BREAKS + 1):
            start, end = self[f"break_start_{i}"], self[f"break_end_{i}"]
            rows.append({
                "index": i, "start": start, "end": end,
                "filled": bool(start.value() or end.value()),
            })
        return rows

    def selected_dates(self):
        """The ticked days as date objects, in the event's own order."""
        by_iso = {d.isoformat(): d for d in self.dates}
        return [by_iso[value] for value in self.cleaned_data.get("days", [])]

    def break_windows(self):
        """The validated (start, end) break windows, or an empty list."""
        return getattr(self, "_break_windows", [])

    # ----- validation -------------------------------------------------------
    def clean(self):
        cleaned = super().clean()
        start = cleaned.get("start_time")
        end = cleaned.get("end_time")

        # Attach each problem to the field that caused it, so the message
        # appears under the input the admin needs to change.
        day_ok = False
        if start and end:
            if end <= start:
                self.add_error(
                    "end_time",
                    f"The day has to end after it starts — you set {start:%H:%M} to {end:%H:%M}.",
                )
            else:
                day_ok = True
                length = (cleaned.get("duration_minutes") or 0) + (cleaned.get("gap_minutes") or 0)
                today = datetime.date.today()
                span = int(
                    (datetime.datetime.combine(today, end)
                     - datetime.datetime.combine(today, start)).total_seconds() // 60
                )
                if length > span:
                    self.add_error(
                        "end_time",
                        f"A {int(length)}-minute slot doesn't fit in a {span}-minute day. "
                        f"Shorten the slot or widen the window.",
                    )

        windows = []
        for i in range(1, self.MAX_BREAKS + 1):
            b_start = cleaned.get(f"break_start_{i}")
            b_end = cleaned.get(f"break_end_{i}")
            if not b_start and not b_end:
                continue  # blank row — ignored
            if not b_start:
                self.add_error(f"break_start_{i}", "Give the break both a start and an end.")
                continue
            if not b_end:
                self.add_error(f"break_end_{i}", "Give the break both a start and an end.")
                continue
            if b_end <= b_start:
                self.add_error(
                    f"break_end_{i}",
                    f"Break {i} has to end after it starts — you set {b_start:%H:%M} to {b_end:%H:%M}.",
                )
            elif day_ok and (b_start < start or b_end > end):
                self.add_error(
                    f"break_start_{i}",
                    f"Break {i} sits outside the {start:%H:%M}–{end:%H:%M} day.",
                )
            else:
                windows.append((b_start, b_end))

        # Overlapping breaks are almost always a mistake — ask for one window.
        windows.sort()
        for (prev_start, prev_end), (next_start, next_end) in zip(windows, windows[1:]):
            if next_start < prev_end:
                self.add_error(None, (
                    f"Two breaks overlap ({prev_start:%H:%M}–{prev_end:%H:%M} and "
                    f"{next_start:%H:%M}–{next_end:%H:%M}) — merge them into one window."
                ))
                break

        self._break_windows = windows
        return cleaned

    # ----- generation -------------------------------------------------------
    def generate_times(self):
        """Slot start times for one day, skipping anything that hits a break.

        A slot is dropped when it overlaps any break window — a meeting that
        runs into lunch is no more usable than one held during it.
        """
        today = datetime.date.today()
        start = datetime.datetime.combine(today, self.cleaned_data["start_time"])
        end = datetime.datetime.combine(today, self.cleaned_data["end_time"])
        length = datetime.timedelta(minutes=self.cleaned_data["duration_minutes"])
        step = length + datetime.timedelta(minutes=self.cleaned_data.get("gap_minutes") or 0)

        windows = [
            (datetime.datetime.combine(today, s), datetime.datetime.combine(today, e))
            for s, e in self.break_windows()
        ]

        times = []
        current = start
        while current + length <= end:
            hit = next((w for w in windows if current < w[1] and current + length > w[0]), None)
            if hit is None:
                times.append(current.time())
                current += step
            else:
                # Resume at the end of the break rather than stepping through it.
                current = hit[1]
        return times
