"""
Day-by-day scheduling and auto-generated meeting links (tests).

Covers:

  * online slots with no link (and no event default) get their own private
    auto-generated Jitsi room, unique per (event, host, date, time);
  * a manually pasted Meet/Zoom link always overrides and is never rewritten;
  * concurrent meetings may not share a manual link, and two offline slots
    may not occupy the same venue+hall+table at overlapping times;
  * skipping a day hides it from visitors, blocks new slot creation, cancels
    its confirmed bookings with notice — and re-opening restores it;
  * the slot builder never offers skipped days.
"""
import datetime

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core import mail
from django.test import TestCase
from django.urls import reverse

from bookings.models import Booking
from bookings.services import create_booking
from events.forms import EventForm, SlotForm
from events.forms_bulk import SlotBuilderForm
from events.models import Event, EventDay, Slot, TeamMember
from notifications.models import NotificationLog


class DayScheduleMixin:
    def setUp(self):
        User = get_user_model()
        self.admin = User.objects.create_user(
            username="admin", password="pw-for-tests-123", is_staff=True, is_superuser=True
        )
        self.client.force_login(self.admin)
        self.day = datetime.date.today() + datetime.timedelta(days=2)
        self.event = Event.objects.create(
            name="Link Roadshow", start_date=self.day, duration_days=3,
            status=Event.STATUS_LIVE, public_slug="link-roadshow",
            event_type=Event.TYPE_ONLINE,
        )
        self.host = TeamMember.objects.create(
            event=self.event, name="Ripon Shikdar", email="ripon@example.com"
        )
        self.host2 = TeamMember.objects.create(
            event=self.event, name="Nadia Karim", email="nadia@example.com"
        )

    def make_slot(self, host=None, day=None, time=(10, 30), minutes=30, **kwargs):
        return Slot.objects.create(
            event=self.event, host=host or self.host,
            date=day or self.day,
            start_time=datetime.time(*time), duration_minutes=minutes, **kwargs
        )

    def book(self, slot):
        return create_booking(
            slot=slot, visitor_name="Anika Rahman", visitor_email="anika@example.com"
        )


class AutoLinkGenerationTest(DayScheduleMixin, TestCase):
    def test_slot_without_any_link_gets_private_room(self):
        slot = self.make_slot()
        self.assertTrue(slot.meeting_link.startswith("https://meet.jit.si/tdc-link-roadshow-"))
        self.assertEqual(slot.video_provider, Event.PROVIDER_JITSI)
        self.assertTrue(slot.is_publicly_bookable())

    def test_auto_links_are_unique_per_slot(self):
        a = self.make_slot(time=(9, 0))
        b = self.make_slot(time=(10, 0))
        c = self.make_slot(host=self.host2, time=(9, 0))
        links = {a.meeting_link, b.meeting_link, c.meeting_link}
        self.assertEqual(len(links), 3)

    def test_same_slot_regenerates_the_same_room(self):
        slot = self.make_slot()
        first = slot.meeting_link
        slot.full_clean()
        slot.save()
        self.assertEqual(slot.meeting_link, first)

    def test_manual_link_is_never_overwritten(self):
        manual = "https://meet.google.com/abc-defg-hij"
        slot = self.make_slot(meeting_link=manual, video_provider=Event.PROVIDER_GOOGLE_MEET)
        self.assertEqual(slot.meeting_link, manual)
        self.assertEqual(slot.effective_meeting_link, manual)

    def test_event_default_link_still_inherited(self):
        self.event.default_video_provider = Event.PROVIDER_GOOGLE_MEET
        self.event.default_meeting_link = "https://zoom.us/j/123"
        self.event.save()
        slot = self.make_slot()
        self.assertEqual(slot.meeting_link, "")  # nothing stored — inherits
        self.assertEqual(slot.effective_meeting_link, "https://zoom.us/j/123")

    def test_event_form_accepts_a_blank_link(self):
        # Auto-rooms are the default: the event-level link is optional now.
        form = EventForm(
            data={
                "name": "No Link Webinars", "description": "",
                "start_date": self.day.isoformat(), "duration_days": "2",
                "default_video_provider": Event.PROVIDER_ZOOM,
                "default_meeting_link": "",
                "reminder_hours_csv": "", "inapp_lead_minutes": "30",
            },
            event_type=Event.TYPE_ONLINE,
        )
        self.assertTrue(form.is_valid(), form.errors)
        event = form.save()
        self.assertEqual(event.default_meeting_link, "")

    def test_slot_form_gives_blank_slots_their_own_room(self):
        form = SlotForm(
            data={
                "host": self.host.pk, "date": self.day.isoformat(),
                "start_time": "10:30", "duration_minutes": "30", "capacity": "1",
                "video_provider": "", "meeting_link": "",
            },
            event=self.event,
        )
        self.assertTrue(form.is_valid(), form.errors)
        slot = form.save()
        self.assertTrue(slot.meeting_link.startswith("https://meet.jit.si/tdc-link-roadshow-"))
        self.assertEqual(slot.video_provider, Event.PROVIDER_JITSI)
        self.assertTrue(slot.is_publicly_bookable())

    def test_slot_form_still_uses_a_shared_event_link(self):
        self.event.default_video_provider = Event.PROVIDER_ZOOM
        self.event.default_meeting_link = "https://zoom.us/j/42"
        self.event.save()
        form = SlotForm(
            data={
                "host": self.host.pk, "date": self.day.isoformat(),
                "start_time": "11:00", "duration_minutes": "30", "capacity": "1",
                "video_provider": "", "meeting_link": "",
            },
            event=self.event,
        )
        self.assertTrue(form.is_valid(), form.errors)
        slot = form.save()
        self.assertEqual(slot.meeting_link, "")  # nothing stored — inherits
        self.assertEqual(slot.effective_meeting_link, "https://zoom.us/j/42")

    def test_offline_slot_never_gets_a_link(self):
        self.event.event_type = Event.TYPE_OFFLINE
        self.event.venue, self.event.hall_name, self.event.table_name = "Grand Hall", "Hall A", "Table 1"
        self.event.save()
        slot = self.make_slot()
        self.assertEqual(slot.meeting_link, "")
        self.assertEqual(slot.mode, Slot.MODE_OFFLINE)

    def test_auto_link_flows_to_confirmation_email(self):
        slot = self.make_slot()
        booking = self.book(slot)
        from notifications.emails import send_booking_confirmation
        send_booking_confirmation(booking)
        self.assertEqual(len(mail.outbox), 1)
        body = mail.outbox[0].body
        self.assertIn(slot.meeting_link, body)


class ConflictGuardsTest(DayScheduleMixin, TestCase):
    def test_offline_same_table_overlap_rejected(self):
        self.event.event_type = Event.TYPE_OFFLINE
        self.event.venue, self.event.hall_name, self.event.table_name = "Grand Hall", "Hall A", "Table 1"
        self.event.save()
        self.make_slot(host=self.host, time=(10, 0), minutes=60)
        clash = self.make_slot(host=self.host2, time=(10, 30), minutes=30)
        with self.assertRaises(ValidationError) as ctx:
            clash.full_clean()
        self.assertIn("table_name", ctx.exception.message_dict)

    def test_offline_different_tables_ok(self):
        self.event.event_type = Event.TYPE_OFFLINE
        self.event.venue, self.event.hall_name, self.event.table_name = "Grand Hall", "Hall A", ""
        self.event.save()
        self.make_slot(host=self.host, time=(10, 0), minutes=60, table_name="Table 1")
        ok = self.make_slot(host=self.host2, time=(10, 30), minutes=30, table_name="Table 2")
        ok.full_clean()  # no exception

    def test_manual_link_overlap_rejected(self):
        manual = "https://meet.google.com/abc-defg-hij"
        self.make_slot(host=self.host, time=(10, 0), minutes=60,
                       meeting_link=manual, video_provider=Event.PROVIDER_GOOGLE_MEET)
        clash = self.make_slot(host=self.host2, time=(10, 30),
                               meeting_link=manual, video_provider=Event.PROVIDER_GOOGLE_MEET)
        with self.assertRaises(ValidationError) as ctx:
            clash.full_clean()
        self.assertIn("meeting_link", ctx.exception.message_dict)

    def test_distinct_manual_links_at_same_time_ok(self):
        self.make_slot(host=self.host, time=(10, 0), minutes=60,
                       meeting_link="https://meet.google.com/one-one-one",
                       video_provider=Event.PROVIDER_GOOGLE_MEET)
        ok = self.make_slot(host=self.host2, time=(10, 30),
                            meeting_link="https://meet.google.com/two-two-two",
                            video_provider=Event.PROVIDER_GOOGLE_MEET)
        ok.full_clean()  # no exception

    def test_auto_links_never_conflict(self):
        self.make_slot(host=self.host, time=(10, 0))
        ok = self.make_slot(host=self.host2, time=(10, 0))
        ok.full_clean()  # auto rooms are private by construction


class HostEmailTest(DayScheduleMixin, TestCase):
    """The host gets an email with the meeting link whenever a visitor books."""

    def test_public_booking_emails_the_host_with_the_auto_link(self):
        slot = self.make_slot()
        from django.test import Client

        guest = Client()
        response = guest.post(
            reverse("bookings_public:public_book", args=[self.event.public_slug, slot.pk]),
            data={
                "visitor_name": "Anika Rahman",
                "visitor_email": "anika@example.com",
                "visitor_phone": "",
                "company": "",
                "notes": "",
            },
        )
        self.assertEqual(response.status_code, 200)
        booking = Booking.objects.get(slot=slot)
        host_mail = next(m for m in mail.outbox if m.to == [self.host.email])
        self.assertIn("New booking", host_mail.subject)
        for body in [host_mail.body] + [b for b, _ in host_mail.alternatives]:
            self.assertIn(slot.effective_meeting_link, body)
            self.assertIn("anika@example.com", body)
        self.assertTrue(
            NotificationLog.objects.filter(
                booking=booking, recipient_email=self.host.email,
                type=NotificationLog.TYPE_CONFIRMATION,
            ).exists()
        )

    def test_resend_hits_both_parties(self):
        slot = self.make_slot()
        booking = self.book(slot)
        mail.outbox.clear()
        self.client.post(reverse("bookings_admin:admin_resend", args=[booking.pk]))
        recipients = sorted(m.to[0] for m in mail.outbox)
        self.assertEqual(recipients, sorted(["anika@example.com", self.host.email]))

    def test_offline_host_email_shows_venue_not_link(self):
        self.event.event_type = Event.TYPE_OFFLINE
        self.event.venue, self.event.hall_name = "Radisson Blu", "Hall A"
        self.event.default_meeting_link = ""
        self.event.save()
        slot = self.make_slot()
        slot.refresh_from_db()
        booking = self.book(slot)
        mail.outbox.clear()
        from notifications.emails import send_booking_confirmation_to_host

        send_booking_confirmation_to_host(booking)
        host_mail = mail.outbox[0]
        for body in [host_mail.body] + [b for b, _ in host_mail.alternatives]:
            self.assertIn("Radisson Blu", body)
            self.assertNotIn("meet.jit.si", body)


class SkipDayTest(DayScheduleMixin, TestCase):
    def test_skip_hides_slots_and_blocks_creation(self):
        slot = self.make_slot()
        url = reverse("events:day_toggle_skip", args=[self.event.pk, self.day.isoformat()])
        response = self.client.post(url)
        self.assertEqual(response.status_code, 302)
        slot.refresh_from_db()
        self.assertFalse(slot.is_publicly_bookable())
        self.assertEqual(Slot.public_for_event(self.event), [])
        # New slot creation on the skipped day is rejected.
        blocked = self.make_slot(time=(15, 0))
        with self.assertRaises(ValidationError) as ctx:
            blocked.full_clean()
        self.assertIn("date", ctx.exception.message_dict)

    def test_skip_cancels_bookings_with_notice(self):
        slot = self.make_slot()
        booking = self.book(slot)
        self.client.post(reverse("events:day_toggle_skip", args=[self.event.pk, self.day.isoformat()]))
        booking.refresh_from_db()
        self.assertEqual(booking.status, Booking.STATUS_CANCELLED)
        self.assertTrue(NotificationLog.objects.filter(
            booking=booking, type=NotificationLog.TYPE_CANCELLATION).exists())
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("not holding meetings", mail.outbox[0].body)

    def test_unskip_restores_the_day(self):
        slot = self.make_slot()
        url = reverse("events:day_toggle_skip", args=[self.event.pk, self.day.isoformat()])
        self.client.post(url)
        self.client.post(url)  # toggle back
        slot.refresh_from_db()
        self.assertIn(slot, Slot.public_for_event(self.event))

    def test_builder_excludes_skipped_days(self):
        other_day = self.day + datetime.timedelta(days=1)
        EventDay.objects.create(event=self.event, date=other_day, skipped=True)
        form = SlotBuilderForm(event=self.event)
        self.assertNotIn(other_day, form.dates)
        self.assertIn(self.day, form.dates)

    def test_event_detail_shows_day_chips_and_skip_state(self):
        self.make_slot()
        self.client.post(reverse("events:day_toggle_skip", args=[self.event.pk, self.day.isoformat()]))
        response = self.client.get(reverse("events:detail", args=[self.event.pk]))
        self.assertContains(response, "Skipped")
        self.assertContains(response, "Skip day")


