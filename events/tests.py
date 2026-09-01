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

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase
from django.urls import reverse

from bookings.models import Booking
from bookings.services import create_booking
from events.forms import EventForm, SlotForm
from events.forms_bulk import BulkSlotForm
from events.models import Event, Slot, TeamMember
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

    def test_bulk_generated_slots_inherit_the_event_type(self):
        event, host, _ = self.make_event(Event.TYPE_ONLINE)
        self.client.post(reverse("events:slot_bulk", args=[event.pk]), {
            "date_from": self.day.isoformat(), "date_to": self.day.isoformat(),
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

    def _bulk(self, event, **overrides):
        data = {
            "date_from": self.day.isoformat(), "date_to": self.day.isoformat(),
            "start_time": "09:00", "end_time": "12:00",
            "duration_minutes": "60", "capacity": "1", "gap_minutes": "0",
        }
        data.update(overrides)
        return self.client.post(reverse("events:slot_bulk", args=[event.pk]), data,
                                HTTP_HX_REQUEST="true")

    def test_bulk_skips_slots_that_hit_the_break(self):
        event, host, slot = self.make_event(Event.TYPE_OFFLINE)
        slot.delete()
        self._bulk(event, break_start="10:00", break_end="11:00", mark_break="")
        starts = sorted(s.start_time.strftime("%H:%M")
                        for s in event.slots.exclude(status=Slot.STATUS_BREAK))
        # 09:00 runs to 10:00; 10:00 and the 10:00-11:00 window are skipped.
        self.assertEqual(starts, ["09:00", "11:00"])

    def test_a_slot_overlapping_the_break_is_dropped_not_shifted(self):
        event, host, slot = self.make_event(Event.TYPE_OFFLINE)
        slot.delete()
        # 30-min slots, break 10:15-10:45 -> 10:00 overlaps and must go.
        self._bulk(event, duration_minutes="30", end_time="11:30",
                   break_start="10:15", break_end="10:45", mark_break="")
        starts = sorted(s.start_time.strftime("%H:%M")
                        for s in event.slots.exclude(status=Slot.STATUS_BREAK))
        self.assertNotIn("10:00", starts)
        self.assertIn("09:30", starts)
        self.assertIn("10:45", starts)

    def test_break_can_be_shown_in_the_schedule(self):
        event, host, slot = self.make_event(Event.TYPE_OFFLINE)
        slot.delete()
        self._bulk(event, break_start="10:00", break_end="11:00", mark_break="on")
        breaks = event.slots.filter(status=Slot.STATUS_BREAK)
        self.assertEqual(breaks.count(), 1)
        self.assertEqual(breaks.first().duration_minutes, 60)

    def test_gap_between_slots_is_respected(self):
        event, host, slot = self.make_event(Event.TYPE_OFFLINE)
        slot.delete()
        self._bulk(event, duration_minutes="30", gap_minutes="15", end_time="11:00")
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
        form = BulkSlotForm({
            "date_from": self.day.isoformat(), "date_to": self.day.isoformat(),
            "start_time": "09:00", "end_time": "12:00",
            "duration_minutes": "30", "capacity": "1",
            "break_start": "13:00", "break_end": "14:00",
        }, event=event)
        self.assertFalse(form.is_valid())
        self.assertIn("break_start", form.errors)

    def test_half_a_break_is_rejected(self):
        event, _, _ = self.make_event(Event.TYPE_OFFLINE)
        form = BulkSlotForm({
            "date_from": self.day.isoformat(), "date_to": self.day.isoformat(),
            "start_time": "09:00", "end_time": "12:00",
            "duration_minutes": "30", "capacity": "1", "break_start": "10:00",
        }, event=event)
        self.assertFalse(form.is_valid())
        self.assertIn("break_end", form.errors)


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
