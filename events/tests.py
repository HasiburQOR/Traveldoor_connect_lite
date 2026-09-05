"""
Event type is the spine of the setup flow, so it gets real coverage.

The two guarantees under test:

  * an **offline** event never shows a meeting-link field, or a link, anywhere
    downstream — forms, slot screens, the public booking page, the emails, or
    the admin booking view;
  * an **online** event never shows Venue / Hall / Table anywhere downstream.

Plus the immutability rule: the type locks as soon as the event has slots.
"""
import datetime
import io
import tempfile
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from PIL import Image

from audit.models import AuditLogEntry
from bookings.models import Booking
from bookings.services import create_booking
from events.forms import EventForm, HostPickForm, SlotForm
from events.forms_bulk import SlotBuilderForm
from events.models import Event, Person, Slot, TeamMember
from notifications.emails import send_booking_confirmation, send_reminder

LINK = "https://meet.google.com/abc-defg-hij"
VENUE_WORDS = ["Grand Hall", "Hall A", "Table 7"]


class EventTypeSetupMixin:
    """Builds a complete online and offline event, each with a live booking."""

    def setUp(self):
        super().setUp()
        User = get_user_model()
        self.admin = User.objects.create_user(
            username="admin", password="pw-for-tests-123", is_staff=True, is_superuser=True
        )
        self.client.force_login(self.admin)
        self.day = datetime.date.today() + datetime.timedelta(days=2)

    def make_event(self, event_type):
        common = dict(
            name=f"{event_type.title()} Roadshow",
            start_date=self.day - datetime.timedelta(days=1),
            duration_days=5,
            status=Event.STATUS_LIVE,
            public_slug=f"{event_type}-roadshow",
            event_type=event_type,
        )
        if event_type == Event.TYPE_ONLINE:
            common.update(default_video_provider=Event.PROVIDER_GOOGLE_MEET, default_meeting_link=LINK)
        else:
            common.update(venue="Grand Hall", hall_name="Hall A", table_name="Table 7")
        event = Event.objects.create(**common)
        host = TeamMember.objects.create(event=event, name="Ripon Shikdar",
                                         email="ripon@example.com", role="IT Manager")
        slot = Slot.objects.create(event=event, host=host, date=self.day,
                                   start_time=datetime.time(10, 30), duration_minutes=30)
        return event, host, slot

    def book(self, slot):
        return create_booking(
            slot=slot, visitor_name="Anika Rahman", visitor_email="anika@example.com",
            visitor_phone="", company="Wanderlust Travels", notes="",
        )


class OfflineEventNeverShowsLinksTest(EventTypeSetupMixin, TestCase):
    """An offline event must not surface a meeting-link field or link anywhere."""

    def setUp(self):
        super().setUp()
        self.event, self.host, self.slot = self.make_event(Event.TYPE_OFFLINE)

    def test_event_form_has_no_link_fields(self):
        form = EventForm(event_type=Event.TYPE_OFFLINE)
        self.assertNotIn("default_meeting_link", form.fields)
        self.assertNotIn("default_video_provider", form.fields)
        for name in ("venue", "hall_name", "table_name"):
            self.assertIn(name, form.fields)

    def test_slot_form_has_no_link_fields(self):
        form = SlotForm(event=self.event)
        self.assertNotIn("meeting_link", form.fields)
        self.assertNotIn("video_provider", form.fields)
        self.assertNotIn("mode", form.fields)

    def test_admin_screens_render_no_link_field(self):
        for url in (
            reverse("events:create") + "?type=offline",
            reverse("events:update", args=[self.event.pk]),
            reverse("events:slot_add", args=[self.event.pk]),
            reverse("events:slot_edit", args=[self.event.pk, self.slot.pk]),
            reverse("events:detail", args=[self.event.pk]),
        ):
            html = self.client.get(url).content.decode()
            self.assertNotIn('name="meeting_link"', html, url)
            self.assertNotIn('name="video_provider"', html, url)
            self.assertNotIn("meet.google.com", html, url)

    def test_public_booking_page_shows_venue_not_link(self):
        html = self.client.get(
            reverse("bookings_public:public_host", args=[self.event.public_slug, self.host.pk])
        ).content.decode()
        self.assertIn("All hosts", html)
        self.assertNotIn(LINK, html)

        stub = self.client.get(
            reverse("bookings_public:public_book", args=[self.event.public_slug, self.slot.pk])
        ).content.decode()
        self.assertIn("In person", stub)
        self.assertNotIn("Video call", stub)
        self.assertNotIn(LINK, stub)
        for word in VENUE_WORDS:
            self.assertIn(word, stub)

    def test_confirmation_and_reminder_emails_carry_address_not_link(self):
        booking = self.book(self.slot)
        mail.outbox.clear()
        send_booking_confirmation(booking)
        send_reminder(booking, 24)
        self.assertEqual(len(mail.outbox), 2)
        for message in mail.outbox:
            bodies = [message.body] + [b for b, _ in message.alternatives]
            for body in bodies:
                self.assertNotIn(LINK, body)
                self.assertNotIn("Join link", body)
                for word in VENUE_WORDS:
                    self.assertIn(word, body)
        # The confirmation must tell the visitor to bring it to the venue.
        confirmation = mail.outbox[0]
        self.assertIn("venue", confirmation.body.lower())

    def test_admin_booking_detail_has_no_link_row(self):
        booking = self.book(self.slot)
        html = self.client.get(
            reverse("bookings_admin:admin_detail", args=[booking.pk])
        ).content.decode()
        self.assertIn("In person", html)
        self.assertNotIn("Join link", html)
        self.assertNotIn(LINK, html)

    def test_inapp_notification_shows_venue_and_no_link(self):
        from notifications.models import HostNotification

        booking = self.book(self.slot)
        notice = HostNotification.objects.create(
            host=self.host, booking=booking, event=self.event,
            kind=HostNotification.KIND_UPCOMING, title="Meeting with Anika Rahman",
            meeting_datetime=self.slot.start_datetime,
            venue_details="Grand Hall — Hall A — Table 7",
        )
        html = self.client.get(reverse("notifications:list")).content.decode()
        self.assertIn("Table 7", html)
        self.assertNotIn("Join the video call", html)
        self.assertEqual(notice.join_url, "")


class OnlineEventNeverShowsVenueTest(EventTypeSetupMixin, TestCase):
    """An online event must not surface Venue / Hall / Table anywhere."""

    def setUp(self):
        super().setUp()
        self.event, self.host, self.slot = self.make_event(Event.TYPE_ONLINE)

    def test_event_form_has_no_venue_fields(self):
        form = EventForm(event_type=Event.TYPE_ONLINE)
        for name in ("venue", "hall_name", "table_name"):
            self.assertNotIn(name, form.fields)
        self.assertIn("default_meeting_link", form.fields)

    def test_slot_form_has_no_venue_fields(self):
        form = SlotForm(event=self.event)
        for name in ("venue", "hall_name", "table_name"):
            self.assertNotIn(name, form.fields)
        self.assertIn("meeting_link", form.fields)

    def test_admin_screens_render_no_venue_field(self):
        for url in (
            reverse("events:create") + "?type=online",
            reverse("events:update", args=[self.event.pk]),
            reverse("events:slot_add", args=[self.event.pk]),
            reverse("events:slot_edit", args=[self.event.pk, self.slot.pk]),
            reverse("events:detail", args=[self.event.pk]),
        ):
            html = self.client.get(url).content.decode()
            self.assertNotIn('name="venue"', html, url)
            self.assertNotIn('name="hall_name"', html, url)
            self.assertNotIn('name="table_name"', html, url)

    def test_public_booking_page_shows_video_call_not_venue(self):
        stub = self.client.get(
            reverse("bookings_public:public_book", args=[self.event.public_slug, self.slot.pk])
        ).content.decode()
        self.assertIn("Google Meet", stub)
        self.assertNotIn("In person", stub)
        for word in ("Venue", "Hall", "Table"):
            self.assertNotIn(f">{word}<", stub)

    def test_confirmation_and_reminder_emails_carry_link_not_address(self):
        booking = self.book(self.slot)
        mail.outbox.clear()
        send_booking_confirmation(booking)
        send_reminder(booking, 24)
        self.assertEqual(len(mail.outbox), 2)
        for message in mail.outbox:
            bodies = [message.body] + [b for b, _ in message.alternatives]
            for body in bodies:
                self.assertIn(LINK, body)
                self.assertNotIn("Venue:", body)
                self.assertNotIn("Hall:", body)
                self.assertNotIn("Table:", body)

    def test_admin_booking_detail_has_no_venue_rows(self):
        booking = self.book(self.slot)
        html = self.client.get(
            reverse("bookings_admin:admin_detail", args=[booking.pk])
        ).content.decode()
        self.assertIn("Video call", html)
        self.assertIn(LINK, html)
        self.assertNotIn("<th>Venue</th>", html)
        self.assertNotIn("<th>Table</th>", html)


class EventTypeIsImmutableTest(EventTypeSetupMixin, TestCase):
    def test_type_unlocked_until_the_first_slot_exists(self):
        event = Event.objects.create(
            name="Fresh", start_date=self.day, duration_days=1,
            event_type=Event.TYPE_ONLINE,
            default_video_provider=Event.PROVIDER_ZOOM, default_meeting_link=LINK,
        )
        self.assertFalse(event.event_type_locked)
        html = self.client.get(reverse("events:update", args=[event.pk])).content.decode()
        self.assertIn("Switch to in person", html)

    def test_type_locks_once_a_slot_exists(self):
        event, host, slot = self.make_event(Event.TYPE_OFFLINE)
        self.assertTrue(event.event_type_locked)
        html = self.client.get(reverse("events:update", args=[event.pk])).content.decode()
        self.assertIn("Event type is locked", html)
        self.assertNotIn("Switch to", html)

    def test_posting_another_type_to_a_locked_event_is_ignored(self):
        event, host, slot = self.make_event(Event.TYPE_OFFLINE)
        self.client.post(reverse("events:update", args=[event.pk]), {
            "name": event.name,
            "description": "",
            "start_date": event.start_date.isoformat(),
            "duration_days": "5",
            "event_type": Event.TYPE_ONLINE,  # attacker-supplied; must be ignored
            "venue": "Grand Hall", "hall_name": "Hall A", "table_name": "Table 7",
            "reminder_hours_csv": "", "inapp_lead_minutes": "30",
        })
        event.refresh_from_db()
        self.assertEqual(event.event_type, Event.TYPE_OFFLINE)


class SlotModeMirrorsEventTypeTest(EventTypeSetupMixin, TestCase):
    def test_slot_mode_follows_the_event_and_clears_the_other_fields(self):
        event, host, _ = self.make_event(Event.TYPE_OFFLINE)
        # Even if stale values are forced onto the instance, save() normalises.
        slot = Slot(event=event, host=host, date=self.day, start_time=datetime.time(14, 0),
                    mode=Slot.MODE_ONLINE, meeting_link=LINK,
                    video_provider=Event.PROVIDER_ZOOM)
        slot.save()
        slot.refresh_from_db()
        self.assertEqual(slot.mode, Slot.MODE_OFFLINE)
        self.assertEqual(slot.meeting_link, "")
        self.assertEqual(slot.video_provider, "")

    def test_builder_slots_inherit_the_event_type(self):
        event, host, _ = self.make_event(Event.TYPE_ONLINE)
        self.client.post(reverse("events:slot_build", args=[event.pk]), {
            "host": "", "days": [self.day.isoformat()],
            "start_time": "09:00", "end_time": "10:00",
            "duration_minutes": "30", "capacity": "1",
        })
        modes = set(event.slots.values_list("mode", flat=True))
        self.assertEqual(modes, {Slot.MODE_ONLINE})

    def test_online_slot_inherits_the_event_default_link(self):
        event, host, slot = self.make_event(Event.TYPE_ONLINE)
        self.assertEqual(slot.meeting_link, "")  # nothing stored on the slot
        self.assertEqual(slot.effective_meeting_link, LINK)
        self.assertTrue(slot.is_publicly_bookable())


class BreakTimeTest(EventTypeSetupMixin, TestCase):
    """Breaks block time for a host and never reach a visitor."""

    def _build(self, event, **overrides):
        data = {
            "host": "", "days": [self.day.isoformat()],
            "start_time": "09:00", "end_time": "12:00",
            "duration_minutes": "60", "capacity": "1", "gap_minutes": "0",
        }
        data.update(overrides)
        return self.client.post(reverse("events:slot_build", args=[event.pk]), data,
                                HTTP_HX_REQUEST="true")

    def test_builder_skips_slots_that_hit_the_break(self):
        event, host, slot = self.make_event(Event.TYPE_OFFLINE)
        slot.delete()
        self._build(event, break_start_1="10:00", break_end_1="11:00", mark_breaks="")
        starts = sorted(s.start_time.strftime("%H:%M")
                        for s in event.slots.exclude(status=Slot.STATUS_BREAK))
        # 09:00 runs to 10:00; 10:00 and the 10:00-11:00 window are skipped.
        self.assertEqual(starts, ["09:00", "11:00"])

    def test_a_slot_overlapping_the_break_is_dropped_not_shifted(self):
        event, host, slot = self.make_event(Event.TYPE_OFFLINE)
        slot.delete()
        # 30-min slots, break 10:15-10:45 -> 10:00 overlaps and must go.
        self._build(event, duration_minutes="30", end_time="11:30",
                    break_start_1="10:15", break_end_1="10:45", mark_breaks="")
        starts = sorted(s.start_time.strftime("%H:%M")
                        for s in event.slots.exclude(status=Slot.STATUS_BREAK))
        self.assertNotIn("10:00", starts)
        self.assertIn("09:30", starts)
        self.assertIn("10:45", starts)

    def test_break_can_be_shown_in_the_schedule(self):
        event, host, slot = self.make_event(Event.TYPE_OFFLINE)
        slot.delete()
        self._build(event, break_start_1="10:00", break_end_1="11:00", mark_breaks="on")
        breaks = event.slots.filter(status=Slot.STATUS_BREAK)
        self.assertEqual(breaks.count(), 1)
        self.assertEqual(breaks.first().duration_minutes, 60)

    def test_gap_between_slots_is_respected(self):
        event, host, slot = self.make_event(Event.TYPE_OFFLINE)
        slot.delete()
        self._build(event, duration_minutes="30", gap_minutes="15", end_time="11:00")
        starts = sorted(s.start_time.strftime("%H:%M") for s in event.slots.all())
        self.assertEqual(starts, ["09:00", "09:45", "10:30"])

    def test_a_break_is_never_bookable_or_visible_to_visitors(self):
        event, host, slot = self.make_event(Event.TYPE_ONLINE)
        slot.status = Slot.STATUS_BREAK
        slot.save()
        self.assertFalse(slot.is_publicly_bookable())
        self.assertNotIn(slot, Slot.visible_for_host(host))
        html = self.client.get(
            reverse("bookings_public:public_host", args=[event.public_slug, host.pk])
        ).content.decode()
        self.assertNotIn("10:30", html, "a break must not appear as a bookable time")
        self.assertIn("No open times", html)

    def test_break_slot_needs_no_meeting_details(self):
        """An online event still requires a link — except for a break."""
        event, host, slot = self.make_event(Event.TYPE_ONLINE)
        event.default_video_provider = ""
        event.default_meeting_link = ""
        event.save()
        form = SlotForm(
            {"host": host.pk, "date": self.day.isoformat(), "start_time": "13:00",
             "duration_minutes": "60", "capacity": "1",
             "video_provider": "", "meeting_link": "", "is_break": "on"},
            event=event,
        )
        self.assertTrue(form.is_valid(), form.errors)
        created = form.save(commit=False)
        created.event = event
        created.save()
        self.assertEqual(created.status, Slot.STATUS_BREAK)

    def test_unticking_break_returns_the_time_to_visitors(self):
        event, host, slot = self.make_event(Event.TYPE_OFFLINE)
        slot.status = Slot.STATUS_BREAK
        slot.save()
        form = SlotForm(
            {"host": host.pk, "date": slot.date.isoformat(),
             "start_time": slot.start_time.strftime("%H:%M"),
             "duration_minutes": "30", "capacity": "1",
             "venue": "", "hall_name": "", "table_name": ""},
            instance=slot, event=event,
        )
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.save().status, Slot.STATUS_AVAILABLE)

    def test_break_must_sit_inside_the_day(self):
        event, _, _ = self.make_event(Event.TYPE_OFFLINE)
        form = SlotBuilderForm({
            "host": "", "days": [self.day.isoformat()],
            "start_time": "09:00", "end_time": "12:00",
            "duration_minutes": "30", "capacity": "1",
            "break_start_1": "13:00", "break_end_1": "14:00",
        }, event=event)
        self.assertFalse(form.is_valid())
        self.assertIn("break_start_1", form.errors)

    def test_half_a_break_is_rejected(self):
        event, _, _ = self.make_event(Event.TYPE_OFFLINE)
        form = SlotBuilderForm({
            "host": "", "days": [self.day.isoformat()],
            "start_time": "09:00", "end_time": "12:00",
            "duration_minutes": "30", "capacity": "1", "break_start_1": "10:00",
        }, event=event)
        self.assertFalse(form.is_valid())
        self.assertIn("break_end_1", form.errors)


class CloneEventTest(EventTypeSetupMixin, TestCase):
    """Duplicating an event copies its setup, never its bookings."""

    def _source(self):
        event, host, slot = self.make_event(Event.TYPE_OFFLINE)
        # A second day, a second host, and a break — the whole shape.
        TeamMember.objects.create(event=event, name="Nino K", email="nino@example.com")
        Slot.objects.create(event=event, host=host,
                            date=self.day + datetime.timedelta(days=1),
                            start_time=datetime.time(9, 0))
        Slot.objects.create(event=event, host=host, date=self.day,
                            start_time=datetime.time(13, 0), duration_minutes=60,
                            status=Slot.STATUS_BREAK)
        self.book(slot)
        return event

    def test_clone_copies_setup_as_a_fresh_draft(self):
        source = self._source()
        self.client.post(reverse("events:clone", args=[source.pk]))
        clone = Event.objects.exclude(pk=source.pk).get()
        self.assertEqual(clone.name, f"{source.name} (copy)")
        self.assertEqual(clone.status, Event.STATUS_DRAFT)
        self.assertIsNone(clone.public_slug)
        self.assertEqual(clone.event_type, source.event_type)
        self.assertEqual(clone.venue, source.venue)
        self.assertEqual(clone.team_members.count(), source.team_members.count())
        self.assertEqual(clone.slots.count(), source.slots.count())

    def test_clone_carries_no_bookings(self):
        source = self._source()
        self.assertEqual(source.bookings.count(), 1)
        self.client.post(reverse("events:clone", args=[source.pk]))
        clone = Event.objects.exclude(pk=source.pk).get()
        self.assertEqual(clone.bookings.count(), 0)
        # Every copied slot is open again, breaks excepted.
        statuses = set(clone.slots.exclude(status=Slot.STATUS_BREAK)
                       .values_list("status", flat=True))
        self.assertEqual(statuses, {Slot.STATUS_AVAILABLE})

    def test_clone_preserves_breaks_and_day_offsets(self):
        source = self._source()
        self.client.post(reverse("events:clone", args=[source.pk]))
        clone = Event.objects.exclude(pk=source.pk).get()
        self.assertEqual(clone.slots.filter(status=Slot.STATUS_BREAK).count(), 1)
        # Day-two slot stays on day two relative to the start date.
        offsets = sorted({(s.date - clone.start_date).days for s in clone.slots.all()})
        source_offsets = sorted({(s.date - source.start_date).days for s in source.slots.all()})
        self.assertEqual(offsets, source_offsets)

    def test_clone_keeps_slot_mode_mirrored(self):
        source = self._source()
        self.client.post(reverse("events:clone", args=[source.pk]))
        clone = Event.objects.exclude(pk=source.pk).get()
        self.assertEqual(set(clone.slots.values_list("mode", flat=True)),
                         {Slot.MODE_OFFLINE})

    def test_clone_belongs_to_the_new_event_not_the_old_one(self):
        source = self._source()
        self.client.post(reverse("events:clone", args=[source.pk]))
        clone = Event.objects.exclude(pk=source.pk).get()
        for slot in clone.slots.all():
            self.assertEqual(slot.event_id, clone.pk)
            self.assertEqual(slot.host.event_id, clone.pk)


class CalendarLinkTest(EventTypeSetupMixin, TestCase):
    """Add-to-calendar links follow the event type, like everything else."""

    def test_online_calendar_link_carries_the_join_link_not_a_venue(self):
        event, host, slot = self.make_event(Event.TYPE_ONLINE)
        booking = self.book(slot)
        mail.outbox.clear()
        send_booking_confirmation(booking)
        for body in [mail.outbox[0].body] + [b for b, _ in mail.outbox[0].alternatives]:
            self.assertIn("calendar.google.com", body)
            self.assertIn("outlook.live.com", body)
            self.assertNotIn("Grand+Hall", body)

    def test_offline_calendar_link_carries_the_venue_not_a_link(self):
        event, host, slot = self.make_event(Event.TYPE_OFFLINE)
        booking = self.book(slot)
        mail.outbox.clear()
        send_booking_confirmation(booking)
        for body in [mail.outbox[0].body] + [b for b, _ in mail.outbox[0].alternatives]:
            self.assertIn("calendar.google.com", body)
            self.assertIn("outlook.live.com", body)
            # Venue lands in the calendar location, and no meeting URL appears.
            self.assertIn("Grand", body)
            self.assertNotIn("meet.google.com", body)

    def test_calendar_urls_are_encoded_and_time_bounded(self):
        from notifications.calendar_links import google_calendar_url, outlook_calendar_url

        event, host, slot = self.make_event(Event.TYPE_ONLINE)
        booking = self.book(slot)
        google = google_calendar_url(booking)
        self.assertIn("action=TEMPLATE", google)
        self.assertIn("dates=", google)
        self.assertNotIn(" ", google, "URL must be encoded")
        outlook = outlook_calendar_url(booking)
        self.assertIn("rru=addevent", outlook)
        self.assertNotIn(" ", outlook)


class EventScheduleTest(TestCase):
    """FR-1.1 extension — the customisable schedule.

    An event's ``duration_days`` stays a plain calendar span (first → last
    date); ``active_weekdays`` decides which days inside that span actually
    run, so weekends become free gaps unless they are ticked. Blank means
    every day, which keeps pre-existing events exactly as they were.
    """

    def setUp(self):
        User = get_user_model()
        self.admin = User.objects.create_user(
            username="admin", password="pw-for-tests-123", is_staff=True, is_superuser=True
        )
        self.client.force_login(self.admin)
        # Monday 1 June 2026 — a fixed anchor so the weekday maths is deterministic.
        self.monday = datetime.date(2026, 6, 1)

    def make_event(self, **overrides):
        data = dict(
            name="Roadshow", start_date=self.monday, duration_days=12,
            event_type=Event.TYPE_ONLINE, status=Event.STATUS_LIVE,
        )
        data.update(overrides)
        return Event.objects.create(**data)

    def post_create(self, overrides=None, include_schedule=True):
        data = {
            "name": "Roadshow", "description": "",
            "start_date": "2026-06-01", "duration_days": "5",
            "event_type": Event.TYPE_ONLINE,
            "default_video_provider": Event.PROVIDER_GOOGLE_MEET,
            "default_meeting_link": LINK,
            "reminder_hours_csv": "", "inapp_lead_minutes": "30",
        }
        if include_schedule:
            data.update({
                "schedule_present": "1", "schedule_mode": "count",
                "weekday_0": "on", "weekday_1": "on", "weekday_2": "on",
                "weekday_3": "on", "weekday_4": "on",
            })
        data.update(overrides or {})
        return self.client.post(reverse("events:create"), data)

    # ----- model: blank weekdays keep the old every-day behaviour ----------
    def test_blank_weekdays_means_every_day(self):
        event = self.make_event(duration_days=3)
        self.assertEqual(
            event.event_dates(),
            [self.monday, self.monday + datetime.timedelta(days=1),
             self.monday + datetime.timedelta(days=2)],
        )
        self.assertEqual(event.weekday_summary, "")
        self.assertFalse(event.runs_selected_days)

    def test_weekday_selection_skips_the_weekend_inside_the_span(self):
        event = self.make_event(active_weekdays="0,1,2,3,4")  # Mon–Fri
        dates = event.event_dates()  # span Mon 1 Jun → Fri 12 Jun
        self.assertEqual(len(dates), 10)
        self.assertNotIn(datetime.date(2026, 6, 6), dates)  # Sat 6 Jun is free
        self.assertNotIn(datetime.date(2026, 6, 7), dates)  # Sun 7 Jun is free
        self.assertEqual(dates[0], self.monday)
        self.assertEqual(event.last_event_date, datetime.date(2026, 6, 12))
        self.assertEqual(event.weekday_summary, "Mon, Tue, Wed, Thu & Fri")
        self.assertTrue(event.runs_selected_days)

    def test_weekends_themselves_can_be_the_event_days(self):
        event = self.make_event(active_weekdays="5,6")
        self.assertEqual(
            event.event_dates(),
            [datetime.date(2026, 6, 6), datetime.date(2026, 6, 7)],
        )
        self.assertEqual(event.weekday_summary, "Sat & Sun")

    def test_all_seven_days_is_stored_blank(self):
        event = self.make_event(active_weekdays="0,1,2,3,4,5,6")
        self.assertFalse(event.runs_selected_days)  # canonical form: every day
        self.assertEqual(len(event.event_dates()), 12)

    def test_model_clean_rejects_a_selection_that_matches_no_date(self):
        event = self.make_event(duration_days=1, active_weekdays="1")  # Monday, Tue only
        with self.assertRaises(ValidationError):
            event.full_clean()

    # ----- form: weekday selection + range mode -----------------------------
    def test_weekdays_round_trip_through_the_form(self):
        self.assertEqual(self.post_create().status_code, 302)
        event = Event.objects.get(name="Roadshow")
        self.assertEqual(event.active_weekdays, "0,1,2,3,4")
        self.assertEqual(event.weekday_summary, "Mon, Tue, Wed, Thu & Fri")

    def test_all_seven_ticked_is_stored_blank(self):
        response = self.post_create({"weekday_5": "on", "weekday_6": "on"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Event.objects.get(name="Roadshow").active_weekdays, "")

    def test_range_mode_derives_the_day_count(self):
        response = self.post_create({
            "schedule_mode": "range", "duration_days": "99",
            "end_date_input": "2026-06-03",  # Mon → Wed
        })
        self.assertEqual(response.status_code, 302)
        event = Event.objects.get(name="Roadshow")
        self.assertEqual(event.duration_days, 3)
        self.assertEqual(event.end_date, datetime.date(2026, 6, 3))

    def test_range_mode_rejects_an_end_before_the_start(self):
        response = self.post_create({"schedule_mode": "range", "end_date_input": "2026-05-31"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "before the first day")
        self.assertFalse(Event.objects.filter(name="Roadshow").exists())

    def test_count_mode_caps_the_span(self):
        response = self.post_create({"duration_days": "400"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "split longer runs")

    def test_every_weekday_unticked_is_rejected(self):
        response = self.post_create({
            "weekday_0": "", "weekday_1": "", "weekday_2": "",
            "weekday_3": "", "weekday_4": "",
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Tick at least one day")
        self.assertFalse(Event.objects.filter(name="Roadshow").exists())

    def test_a_selection_with_no_matching_date_is_rejected(self):
        response = self.post_create({
            "duration_days": "1",  # Monday only…
            "weekday_0": "", "weekday_1": "on",  # …but Tuesday alone is ticked.
            "weekday_2": "", "weekday_3": "", "weekday_4": "",
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "None of the selected weekdays")

    def test_post_without_the_schedule_block_keeps_every_day(self):
        """Older callers (scripts, API posts) send no schedule keys at all."""
        response = self.post_create(include_schedule=False)
        self.assertEqual(response.status_code, 302)
        event = Event.objects.get(name="Roadshow")
        self.assertEqual(event.active_weekdays, "")
        self.assertEqual(event.duration_days, 5)

    def test_the_form_renders_the_date_breakdown(self):
        response = self.client.get(reverse("events:create") + "?type=online")
        self.assertContains(response, "event-schedule")
        self.assertContains(response, "Days of the week")
        event = self.make_event(active_weekdays="0,1,2,3,4")
        html = self.client.get(reverse("events:update", args=[event.pk])).content.decode()
        self.assertIn("Mon 1 Jun", html)     # server-rendered chip
        self.assertNotIn("Sat 6 Jun", html)  # weekend gap never becomes a chip

    # ----- slot builder syncs with the weekday selection ---------------------
    def test_builder_only_offers_the_events_own_days(self):
        event = self.make_event(active_weekdays="0,1,2,3,4")
        form = SlotBuilderForm(event=event)
        offered = [value for value, _ in form.fields["days"].choices]
        self.assertEqual(offered, [d.isoformat() for d in event.event_dates()])
        self.assertNotIn("2026-06-06", offered)  # Saturday is a free day
        form = SlotBuilderForm({
            "host": "", "days": ["2026-06-01", "2026-06-02"],  # the first Mon & Tue
            "start_time": "09:00", "end_time": "10:00",
            "duration_minutes": "30", "capacity": "1",
        }, event=event)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(
            form.selected_dates(), [datetime.date(2026, 6, d) for d in (1, 2)]
        )

    def test_builder_rejects_a_day_the_event_doesnt_run(self):
        event = self.make_event(active_weekdays="0,1,2,3,4")
        form = SlotBuilderForm({
            "host": "", "days": ["2026-06-06"],  # Saturday — a free day
            "start_time": "09:00", "end_time": "10:00",
            "duration_minutes": "30", "capacity": "1",
        }, event=event)
        self.assertFalse(form.is_valid())
        self.assertIn("days", form.errors)

    # ----- screens -----------------------------------------------------------
    def test_detail_shows_the_date_breakdown_and_counts_days_left(self):
        event = self.make_event(
            start_date=datetime.date.today(), duration_days=7,
            active_weekdays="0,1,2,3,4",
        )
        response = self.client.get(reverse("events:detail", args=[event.pk]))
        html = response.content.decode()
        self.assertIn("Event days", html)
        self.assertIn("Mon, Tue, Wed, Thu &amp; Fri", html)
        # Any 7-day window holds exactly five Mon–Fri days.
        self.assertEqual(response.context["summary"]["days_left"], 5)

    @override_settings(PUBLIC_BASE_URL="https://booking.example.com")
    def test_detail_shows_the_full_public_link_with_copy_button(self):
        event = self.make_event(public_slug="bike-show-2026")
        html = self.client.get(reverse("events:detail", args=[event.pk])).content.decode()
        full = "https://booking.example.com/b/bike-show-2026/"
        self.assertIn(full, html)                   # domain included, ready to paste
        self.assertIn(f'data-copy="{full}"', html)  # the copy button carries the same URL

    def test_public_page_mentions_the_weekday_summary(self):
        # The public page only renders while the event is live, so anchor it
        # on the next Monday strictly after today.
        today = datetime.date.today()
        next_monday = today + datetime.timedelta(days=(7 - today.weekday()) % 7 or 7)
        event = self.make_event(start_date=next_monday, duration_days=5,
                                active_weekdays="0,1,2,3,4", public_slug="sched")
        html = self.client.get(
            reverse("bookings_public:public_event", args=["sched"])
        ).content.decode()
        self.assertIn("Mon, Tue, Wed, Thu &amp; Fri", html)


# ---------------------------------------------------------------------------
# People directory + host picker (FR-2)
# ---------------------------------------------------------------------------


class PeopleDirectoryTest(EventTypeSetupMixin, TestCase):
    """Hosts are picked from a people directory, not retyped per event.

    The guarantees under test:

      * a pick becomes a copied host snapshot on the event, never a live
        reference — editing or deleting a person never rewrites an event's
        published history on its own;
      * the picker only offers available people who are not already hosting
        that event (one host per email per event);
      * editing a person can re-sync their active host records, and the sync
        never touches hosts that were removed from an event.
    """

    def setUp(self):
        super().setUp()
        self.event, self.existing_host, self.slot = self.make_event(Event.TYPE_OFFLINE)
        self.nadia = Person.objects.create(name="Nadia Islam", email="nadia@example.com",
                                           role="Sales Lead")
        self.tarik = Person.objects.create(name="Tarik Hasan", email="tarik@example.com",
                                           role="Travel Consultant")
        self.alum = Person.objects.create(name="Ex Employee", email="ex@example.com",
                                          is_active=False)

    def person_payload(self, **overrides):
        payload = {"name": "Nadia Islam", "email": "nadia@example.com", "role": "Sales Lead",
                   "photo_url": "", "linked_user": "", "is_active": "on"}
        payload.update(overrides)
        return payload

    # ----- directory CRUD ----------------------------------------------------
    def test_person_crud_is_logged_and_round_trips(self):
        response = self.client.post(reverse("events_people:create"),
                                    self.person_payload(name="Farhana Yeasmin",
                                                        email="farhana@example.com",
                                                        role="Operations"))
        self.assertRedirects(response, reverse("events_people:list"))
        person = Person.objects.get(email="farhana@example.com")
        self.assertTrue(AuditLogEntry.objects.filter(
            entity_type=AuditLogEntry.ENTITY_PERSON,
            action=AuditLogEntry.ACTION_CREATE, entity_id=person.pk).exists())

        response = self.client.post(reverse("events_people:edit", args=[person.pk]),
                                    self.person_payload(name="Farhana Yeasmin",
                                                        email="farhana@example.com",
                                                        role="Head of Operations"))
        self.assertRedirects(response, reverse("events_people:list"))
        person.refresh_from_db()
        self.assertEqual(person.role, "Head of Operations")

        response = self.client.post(reverse("events_people:delete", args=[person.pk]))
        self.assertRedirects(response, reverse("events_people:list"))
        self.assertFalse(Person.objects.filter(pk=person.pk).exists())
        self.assertTrue(AuditLogEntry.objects.filter(
            entity_type=AuditLogEntry.ENTITY_PERSON,
            action=AuditLogEntry.ACTION_DELETE, entity_id=person.pk).exists())

    def test_one_directory_entry_per_email(self):
        response = self.client.post(reverse("events_people:create"),
                                    self.person_payload(name="Nadia Again"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "errorlist")
        self.assertEqual(Person.objects.filter(email=self.nadia.email).count(), 1)

    def test_the_people_page_lists_the_directory(self):
        response = self.client.get(reverse("events_people:list"))
        self.assertContains(response, "Nadia Islam")
        self.assertContains(response, "Tarik Hasan")

    # ----- the picker --------------------------------------------------------
    def test_the_picker_only_offers_new_active_people(self):
        offered = HostPickForm(event=self.event).fields["people"].queryset
        self.assertIn(self.nadia, offered)
        self.assertIn(self.tarik, offered)
        self.assertNotIn(self.alum, offered)  # inactive: hidden from the picker
        twin = Person.objects.create(name="Ripon Twin", email=self.existing_host.email)
        self.assertNotIn(twin, HostPickForm(event=self.event).fields["people"].queryset)

    def test_picking_people_adds_copied_host_snapshots(self):
        response = self.client.post(reverse("events:team_add", args=[self.event.pk]),
                                    {"people": [self.nadia.pk, self.tarik.pk]},
                                    HTTP_HX_REQUEST="true")
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn('id="team-section"', html)  # the swap target survives
        self.assertIn("Nadia Islam", html)        # the roster shows the new hosts

        nadia_host = self.event.team_members.get(email="nadia@example.com")
        self.assertEqual(nadia_host.person, self.nadia)  # snapshot link kept
        self.assertEqual(nadia_host.role, "Sales Lead")  # details copied, not referenced
        self.assertTrue(AuditLogEntry.objects.filter(
            entity_type=AuditLogEntry.ENTITY_TEAM_MEMBER,
            action=AuditLogEntry.ACTION_CREATE, entity_id=nadia_host.pk).exists())

    def test_posting_an_already_hosting_email_is_rejected_not_duplicated(self):
        # A person whose email already hosts this event is outside the offered
        # queryset, so the pick comes back as a validation error inside the
        # section — never as a duplicate host.
        twin = Person.objects.create(name="Ripon Twin", email=self.existing_host.email)
        response = self.client.post(reverse("events:team_add", args=[self.event.pk]),
                                    {"people": [twin.pk]}, HTTP_HX_REQUEST="true")
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn('id="team-section"', html)
        self.assertIn("errorlist", html)
        self.assertEqual(self.event.team_members.filter(email=twin.email).count(), 1)

    # ----- sync & delete semantics -------------------------------------------
    def test_editing_a_person_can_resync_their_active_hosts(self):
        host = TeamMember.create_from_person(self.event, self.nadia)
        self.client.post(reverse("events_people:edit", args=[self.nadia.pk]),
                         self.person_payload(name="Nadia Islam-Chowdhury",
                                             role="Head of Sales", sync_hosts="on"))
        host.refresh_from_db()
        self.assertEqual(host.name, "Nadia Islam-Chowdhury")
        self.assertEqual(host.role, "Head of Sales")

    def test_editing_without_sync_leaves_hosts_untouched(self):
        host = TeamMember.create_from_person(self.event, self.nadia)
        self.client.post(reverse("events_people:edit", args=[self.nadia.pk]),
                         self.person_payload(role="Head of Sales"))  # no sync_hosts
        host.refresh_from_db()
        self.assertEqual(host.role, "Sales Lead")  # the snapshot keeps its own details

    def test_removed_hosts_are_left_alone_by_the_sync(self):
        removed = TeamMember.create_from_person(self.event, self.tarik)
        removed.is_active = False
        removed.save(update_fields=["is_active"])
        self.client.post(reverse("events_people:edit", args=[self.tarik.pk]),
                         self.person_payload(name="Tarik Hasan", email="tarik@example.com",
                                             role="Head of Consulting", sync_hosts="on"))
        removed.refresh_from_db()
        self.assertEqual(removed.role, "Travel Consultant")  # history untouched

    def test_deleting_a_person_keeps_their_host_snapshots(self):
        host = TeamMember.create_from_person(self.event, self.nadia)
        self.client.post(reverse("events_people:delete", args=[self.nadia.pk]))
        host.refresh_from_db()
        self.assertIsNone(host.person)              # link dropped…
        self.assertEqual(host.name, "Nadia Islam")  # …but the published details stay


# ---------------------------------------------------------------------------
# Slot builder (FR-3, day-based scheduling)
# ---------------------------------------------------------------------------


class SlotBuilderTest(EventTypeSetupMixin, TestCase):
    """The builder stamps one day pattern onto the event's own days."""

    def _build(self, event, **overrides):
        data = {
            "host": "", "days": [self.day.isoformat()],
            "start_time": "09:00", "end_time": "10:00",
            "duration_minutes": "30", "capacity": "1", "gap_minutes": "0",
        }
        data.update(overrides)
        return self.client.post(reverse("events:slot_build", args=[event.pk]), data,
                                HTTP_HX_REQUEST="true")

    def test_blank_host_builds_for_every_active_host(self):
        event, host, slot = self.make_event(Event.TYPE_OFFLINE)
        slot.delete()
        second = TeamMember.objects.create(event=event, name="Nino K",
                                           email="nino@example.com")
        self._build(event)
        self.assertEqual(event.slots.count(), 4)  # 09:00 + 09:30, for both hosts
        self.assertEqual(host.slots.count(), 2)
        self.assertEqual(second.slots.count(), 2)

    def test_one_host_only_builds_for_that_host(self):
        event, host, slot = self.make_event(Event.TYPE_OFFLINE)
        slot.delete()
        second = TeamMember.objects.create(event=event, name="Nino K",
                                           email="nino@example.com")
        self._build(event, host=host.pk)
        self.assertEqual(host.slots.count(), 2)
        self.assertEqual(second.slots.count(), 0)

    def test_the_host_shortcut_preselects_the_host(self):
        event, host, _ = self.make_event(Event.TYPE_OFFLINE)
        html = self.client.get(
            reverse("events:slot_build", args=[event.pk]) + f"?host={host.pk}",
            HTTP_HX_REQUEST="true").content.decode()
        self.assertIn(f'value="{host.pk}" selected', html)

    def test_several_break_windows_are_all_kept_clear(self):
        event, host, slot = self.make_event(Event.TYPE_OFFLINE)
        slot.delete()
        self._build(event, end_time="13:00", duration_minutes="60",
                    break_start_1="10:00", break_end_1="10:30",
                    break_start_2="11:30", break_end_2="12:00",
                    mark_breaks="on")
        starts = sorted(s.start_time.strftime("%H:%M")
                        for s in event.slots.exclude(status=Slot.STATUS_BREAK))
        self.assertEqual(starts, ["09:00", "10:30", "12:00"])
        breaks = sorted(s.start_time.strftime("%H:%M")
                        for s in event.slots.filter(status=Slot.STATUS_BREAK))
        self.assertEqual(breaks, ["10:00", "11:30"])

    def test_overlapping_breaks_are_rejected(self):
        event, _, _ = self.make_event(Event.TYPE_OFFLINE)
        form = SlotBuilderForm({
            "host": "", "days": [self.day.isoformat()],
            "start_time": "09:00", "end_time": "17:00",
            "duration_minutes": "30", "capacity": "1",
            "break_start_1": "10:00", "break_end_1": "11:00",
            "break_start_2": "10:30", "break_end_2": "11:30",
        }, event=event)
        self.assertFalse(form.is_valid())
        self.assertIn("overlap", str(form.non_field_errors()))

    def test_invalid_post_keeps_the_section_and_swap_target(self):
        event, host, _ = self.make_event(Event.TYPE_OFFLINE)
        response = self._build(event, start_time="14:15", end_time="12:32")
        html = response.content.decode()
        self.assertIn('id="slot-section"', html)  # the HTMX swap target survives
        self.assertIn("14:15", html)              # the message names both times
        self.assertIn("12:32", html)
        self.assertIn("errorlist", html)


# ---------------------------------------------------------------------------
# Photo upload (people directory + host snapshots)
# ---------------------------------------------------------------------------

# A real 1x1 transparent PNG — ImageField/Pillow verify the bytes are an image.
PHOTO_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
    b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


class PhotoUploadTest(EventTypeSetupMixin, TestCase):
    """An uploaded photo rides along everywhere the photo link used to."""

    def test_upload_creates_a_person_photo_and_media_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            with override_settings(MEDIA_ROOT=tmp):
                photo = SimpleUploadedFile("face.png", PHOTO_PNG, content_type="image/png")
                response = self.client.post(reverse("events_people:create"), {
                    "name": "Anik Chowdhury", "email": "anik@example.com", "role": "",
                    "photo_url": "", "photo": photo, "linked_user": "", "is_active": "on",
                })
                self.assertRedirects(response, reverse("events_people:list"))
                person = Person.objects.get(email="anik@example.com")
                self.assertTrue(person.photo.name.startswith("people/"))
                self.assertTrue(person.photo_display_url.startswith("/media/people/"))
                self.assertTrue((Path(tmp) / person.photo.name).exists())

    def test_an_upload_wins_over_a_pasted_link(self):
        person = Person.objects.create(name="Anik", email="anik@example.com",
                                       photo_url="https://example.com/face.png")
        self.assertEqual(person.photo_display_url, "https://example.com/face.png")
        with tempfile.TemporaryDirectory() as tmp:
            with override_settings(MEDIA_ROOT=tmp):
                person.photo.save("face.png", ContentFile(PHOTO_PNG), save=True)
                person.refresh_from_db()
                self.assertTrue(person.photo_display_url.startswith("/media/people/"))

    def test_picking_a_person_copies_the_upload_onto_the_host_snapshot(self):
        person = Person.objects.create(name="Anik", email="anik@example.com")
        event, _, _ = self.make_event(Event.TYPE_OFFLINE)
        with tempfile.TemporaryDirectory() as tmp:
            with override_settings(MEDIA_ROOT=tmp):
                person.photo.save("face.png", ContentFile(PHOTO_PNG), save=True)
                self.client.post(reverse("events:team_add", args=[event.pk]),
                                 {"people": [person.pk]}, HTTP_HX_REQUEST="true")
                host = event.team_members.get(email="anik@example.com")
                self.assertEqual(host.photo.name, person.photo.name)  # shares the file
                self.assertTrue(host.photo_display_url.startswith("/media/people/"))

    def test_syncing_a_person_refreshes_their_host_photos(self):
        person = Person.objects.create(name="Anik", email="anik@example.com")
        event, _, _ = self.make_event(Event.TYPE_OFFLINE)
        host = TeamMember.create_from_person(event, person)
        with tempfile.TemporaryDirectory() as tmp:
            with override_settings(MEDIA_ROOT=tmp):
                person.photo.save("face.png", ContentFile(PHOTO_PNG), save=True)
                self.client.post(reverse("events_people:edit", args=[person.pk]), {
                    "name": "Anik", "email": "anik@example.com", "role": "",
                    "photo_url": "", "photo": "", "linked_user": "",
                    "is_active": "on", "sync_hosts": "on",
                })
                host.refresh_from_db()
                self.assertEqual(host.photo.name, person.photo.name)

    def test_oversized_uploads_are_rejected(self):
        buf = io.BytesIO()
        Image.new("RGB", (3000, 900), "red").save(buf, format="BMP")  # ~7.8 MB
        big = SimpleUploadedFile("big.bmp", buf.getvalue(), content_type="image/bmp")
        with tempfile.TemporaryDirectory() as tmp:
            with override_settings(MEDIA_ROOT=tmp):
                response = self.client.post(reverse("events_people:create"), {
                    "name": "Big Photo", "email": "big@example.com", "role": "",
                    "photo_url": "", "photo": big, "linked_user": "", "is_active": "on",
                })
        self.assertEqual(response.status_code, 200)  # the form comes back with the error
        self.assertContains(response, "under 5 MB")
        self.assertFalse(Person.objects.filter(email="big@example.com").exists())

    def test_public_roster_uses_the_uploaded_photo(self):
        event, host, _ = self.make_event(Event.TYPE_OFFLINE)
        with tempfile.TemporaryDirectory() as tmp:
            with override_settings(MEDIA_ROOT=tmp):
                host.photo.save("face.png", ContentFile(PHOTO_PNG), save=True)
                html = self.client.get(
                    reverse("bookings_public:public_event", args=[event.public_slug])
                ).content.decode()
                self.assertIn(host.photo.url, html)
