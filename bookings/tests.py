"""
Tests for the public schedule filter bar (month / weekday / time-of-day /
exact date) and the expired-slot auto-close job.
"""
import datetime

from django.core import mail
from django.test import TestCase, RequestFactory
from django.urls import reverse
from django.utils import timezone

from events.models import Event, Slot, TeamMember
from bookings.models import Booking
from notifications.jobs import close_expired_slots


def _make_event_and_host():
    event = Event.objects.create(
        name="365-Day Roadshow",
        event_type=Event.TYPE_OFFLINE,
        start_date=datetime.date.today() + datetime.timedelta(days=30),
        duration_days=365,
    )
    host = TeamMember.objects.create(event=event, name="Host", email="host@example.com")
    return event, host


class SlotFilterTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.event, self.host = _make_event_and_host()
        base = datetime.date.today() + datetime.timedelta(days=60)
        # Mon–Wed style dates; use fixed dates for determinism.
        self.s_jun_morning = Slot.objects.create(
            event=self.event, host=self.host,
            date=datetime.date(base.year, 6, 1), start_time=datetime.time(9, 0))
        self.s_jun_evening = Slot.objects.create(
            event=self.event, host=self.host,
            date=datetime.date(base.year, 6, 1), start_time=datetime.time(18, 0))
        self.s_sep_afternoon = Slot.objects.create(
            event=self.event, host=self.host,
            date=datetime.date(base.year, 9, 15), start_time=datetime.time(14, 0))
        self.slots = [self.s_jun_morning, self.s_jun_evening, self.s_sep_afternoon]

    def test_time_of_day_buckets(self):
        self.assertEqual(self.s_jun_morning.time_of_day, "morning")
        self.assertEqual(self.s_sep_afternoon.time_of_day, "afternoon")
        self.assertEqual(self.s_jun_evening.time_of_day, "evening")

    def test_month_filter(self):
        from bookings.views import _filter_slots, _slot_filters
        req = self.factory.get("/", {"month": f"{self.s_sep_afternoon.date.year}-09"})
        flt = _slot_filters(req)
        self.assertEqual(_filter_slots(self.slots, flt), [self.s_sep_afternoon])

    def test_time_of_day_filter(self):
        from bookings.views import _filter_slots, _slot_filters
        req = self.factory.get("/", {"tod": "morning,evening"})
        flt = _slot_filters(req)
        self.assertEqual(_filter_slots(self.slots, flt),
                         [self.s_jun_morning, self.s_jun_evening])

    def test_weekday_filter(self):
        from bookings.views import _filter_slots, _slot_filters
        dow = self.s_sep_afternoon.date.weekday()
        req = self.factory.get("/", {"dow": str(dow)})
        flt = _slot_filters(req)
        self.assertEqual(_filter_slots(self.slots, flt), [self.s_sep_afternoon])

    def test_invalid_filters_are_ignored(self):
        from bookings.views import _filter_slots, _slot_filters
        req = self.factory.get("/", {"month": "nope", "tod": "night", "dow": "9"})
        flt = _slot_filters(req)
        self.assertEqual(_filter_slots(self.slots, flt), self.slots)


class ExpiredSlotAutoCloseTests(TestCase):
    def test_past_available_slot_is_closed_by_job(self):
        event, host = _make_event_and_host()
        past = Slot.objects.create(
            event=event, host=host,
            date=datetime.date.today() - datetime.timedelta(days=1),
            start_time=datetime.time(9, 0), duration_minutes=30)
        future = Slot.objects.create(
            event=event, host=host,
            date=datetime.date.today() + datetime.timedelta(days=1),
            start_time=datetime.time(9, 0))
        closed = close_expired_slots()
        self.assertEqual(closed, 1)
        past.refresh_from_db()
        future.refresh_from_db()
        self.assertEqual(past.status, Slot.STATUS_CLOSED)
        self.assertEqual(past.is_expired, True)
        self.assertEqual(future.status, Slot.STATUS_AVAILABLE)


class BookForAVisitorTests(TestCase):
    """The admin "Book for a visitor" form, reached from any entry point."""

    def setUp(self):
        from django.contrib.auth import get_user_model

        admin = get_user_model().objects.create_superuser("admin", "admin@example.com", "x")
        self.client.force_login(admin)
        self.url = reverse("bookings_admin:admin_create")
        self.day = timezone.localdate() + datetime.timedelta(days=5)
        self.event, self.slot = self._live_event("Expo")
        self.visitor = {"visitor_name": "Ada", "visitor_email": "ada@example.com"}

    def _live_event(self, name):
        event = Event.objects.create(
            name=name, event_type=Event.TYPE_OFFLINE, status=Event.STATUS_LIVE,
            start_date=self.day, duration_days=10, venue="Hall",
        )
        host = TeamMember.objects.create(event=event, name=f"{name} Host", email="h@example.com")
        slot = Slot.objects.create(event=event, host=host, date=self.day, start_time=datetime.time(10, 0))
        self.assertTrue(slot.is_publicly_bookable())
        return event, slot

    def _book(self, **data):
        return self.client.post(self.url, {**self.visitor, **data})

    def test_with_several_events_the_form_asks_for_an_event_first(self):
        self._live_event("Summit")
        response = self.client.get(self.url)
        self.assertContains(response, "Choose an event first")
        self.assertContains(response, "Expo")
        self.assertContains(response, "Summit")

    def test_with_one_event_its_slots_are_ready_to_pick(self):
        response = self.client.get(self.url)
        self.assertEqual(response.context["slots"], [self.slot])
        self.assertContains(response, f'<option value="{self.slot.pk}"')

    def test_picking_an_event_swaps_in_only_its_slots(self):
        other, other_slot = self._live_event("Summit")
        response = self.client.get(self.url, {"event": other.pk}, headers={"HX-Request": "true"})
        self.assertNotContains(response, "<html")
        self.assertContains(response, f'<option value="{other_slot.pk}"')
        self.assertNotContains(response, f'<option value="{self.slot.pk}"')

    def test_event_with_no_open_slots_says_so(self):
        self.slot.delete()
        response = self.client.get(self.url, {"event": self.event.pk}, headers={"HX-Request": "true"})
        self.assertContains(response, "No open slots on this event")

    def test_no_open_events_says_so_instead_of_a_dead_form(self):
        Event.objects.update(status=Event.STATUS_DRAFT)
        response = self.client.get(self.url)
        self.assertContains(response, "No events are open for booking")
        self.assertNotContains(response, "Save booking")

    def test_event_page_link_preselects_its_slot(self):
        response = self.client.get(self.url, {"event": self.event.pk, "slot": self.slot.pk})
        self.assertContains(response, f'<option value="{self.slot.pk}" selected>')
        self.assertContains(response, f'<option value="{self.event.pk}" selected>')

    def test_booking_is_confirmed_and_emails_the_visitor(self):
        response = self._book(event=self.event.pk, slot=self.slot.pk)
        booking = Booking.objects.get()
        self.assertRedirects(response, reverse("bookings_admin:admin_detail", args=[booking.pk]))
        self.assertEqual(booking.status, Booking.STATUS_CONFIRMED)
        self.slot.refresh_from_db()
        self.assertEqual(self.slot.status, Slot.STATUS_BOOKED)
        self.assertIn("ada@example.com", [m.to[0] for m in mail.outbox])

    def test_a_requested_status_cannot_override_confirmed(self):
        self._book(event=self.event.pk, slot=self.slot.pk, status="cancelled")
        self.assertEqual(Booking.objects.get().status, Booking.STATUS_CONFIRMED)

    def test_saving_without_a_slot_explains_why_and_keeps_the_input(self):
        response = self._book(event=self.event.pk)
        self.assertContains(response, "Choose an open slot for this booking.")
        self.assertContains(response, "ada@example.com")
        self.assertFalse(Booking.objects.exists())

    def test_invalid_details_keep_the_chosen_event_and_slot(self):
        response = self.client.post(self.url, {"event": self.event.pk, "slot": self.slot.pk})
        self.assertContains(response, f'<option value="{self.slot.pk}" selected>')
        self.assertFalse(Booking.objects.exists())

    def test_slot_from_another_event_is_refused(self):
        other, other_slot = self._live_event("Summit")
        self._book(event=self.event.pk, slot=other_slot.pk)
        self.assertFalse(Booking.objects.exists())

    def test_a_taken_slot_is_refused_and_no_longer_offered(self):
        self._book(event=self.event.pk, slot=self.slot.pk)
        response = self._book(event=self.event.pk, slot=self.slot.pk)
        self.assertContains(response, "not bookable")
        self.assertEqual(Booking.objects.count(), 1)
        self.assertEqual(response.context["slots"], [])

    def test_bad_slot_values_never_crash(self):
        for bad in ("abc", "²", "99999999999999999999"):
            self.assertEqual(self.client.get(self.url, {"slot": bad}).status_code, 200)
            self.assertEqual(self._book(event=self.event.pk, slot=bad).status_code, 200)
        self.assertFalse(Booking.objects.exists())

    def test_editing_details_leaves_the_status_alone(self):
        self._book(event=self.event.pk, slot=self.slot.pk)
        booking = Booking.objects.get()
        response = self.client.post(
            reverse("bookings_admin:admin_update", args=[booking.pk]),
            {"visitor_name": "Ada L", "visitor_email": "ada@example.com", "status": "cancelled"},
        )
        self.assertEqual(response.status_code, 302)
        booking.refresh_from_db()
        self.assertEqual(booking.visitor_name, "Ada L")
        self.assertEqual(booking.status, Booking.STATUS_CONFIRMED)

    def test_rescheduling_without_a_valid_slot_is_an_error_not_a_crash(self):
        self._book(event=self.event.pk, slot=self.slot.pk)
        booking = Booking.objects.get()
        admin_url = reverse("bookings_admin:admin_reschedule", args=[booking.pk])
        manage_url = reverse("bookings_manage:manage_reschedule", args=[booking.manage_token])
        for url in (admin_url, manage_url):
            for data in ({}, {"slot": "abc"}):
                self.assertEqual(self.client.post(url, data).status_code, 302)
        booking.refresh_from_db()
        self.assertEqual(booking.status, Booking.STATUS_CONFIRMED)
