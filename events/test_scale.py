"""
Long-running schedules — a one- or two-year event must stay usable.

The builder writes tens of thousands of slots in one request, so nothing on
the admin or public side may load, count or render them one at a time. These
tests build a real two-year schedule and pin the query counts and page sizes
of every screen that reads it.
"""
import datetime

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from bookings.models import Booking
from bookings.services import cancel_booking, create_booking
from events.models import Event, Slot, TeamMember
from events.views import SLOTS_PER_PAGE

DAYS = 730
SLOTS_PER_DAY = 16  # 09:00–17:00 in 30-minute slots


class LongScheduleMixin:
    def setUp(self):
        super().setUp()
        self.admin = get_user_model().objects.create_user(
            username="admin", password="pw-for-tests-123", is_staff=True, is_superuser=True
        )
        self.client.force_login(self.admin)
        self.today = datetime.date.today()
        self.event = Event.objects.create(
            name="Two Year Desk", start_date=self.today, duration_days=DAYS,
            status=Event.STATUS_LIVE, public_slug="two-year-desk",
            event_type=Event.TYPE_ONLINE,
        )
        self.hosts = [
            TeamMember.objects.create(event=self.event, name=name, email=f"{name}@example.com")
            for name in ("asha", "bela")
        ]

    def build(self, **overrides):
        data = {
            "host": "", "days": [d.isoformat() for d in self.event.event_dates()],
            "start_time": "09:00", "end_time": "17:00",
            "duration_minutes": "30", "capacity": "1", "gap_minutes": "0",
        }
        data.update(overrides)
        return self.client.post(reverse("events:slot_build", args=[self.event.pk]), data,
                                HTTP_HX_REQUEST="true")

    def book(self, slot):
        return create_booking(slot=slot, visitor_name="Anika Rahman",
                              visitor_email="anika@example.com")

    def queries(self, url, **extra):
        with CaptureQueriesContext(connection) as captured:
            response = self.client.get(url, **extra)
        self.assertEqual(response.status_code, 200)
        return response, len(captured)


class TwoYearScheduleTest(LongScheduleMixin, TestCase):
    def setUp(self):
        super().setUp()
        with CaptureQueriesContext(connection) as captured:
            self.build_response = self.build()
        self.build_queries = len(captured)
        self.total = DAYS * SLOTS_PER_DAY * len(self.hosts)

    def test_builder_writes_two_years_in_bulk(self):
        self.assertEqual(self.build_response.status_code, 200)
        self.assertEqual(self.event.slots.count(), self.total)
        # Bulk batches, not a SELECT + INSERT per slot.
        self.assertLess(self.build_queries, 150)
        slot = self.event.slots.first()
        self.assertEqual(slot.mode, Slot.MODE_ONLINE)  # save() rules still applied
        self.assertTrue(slot.meeting_link.startswith("https://meet.jit.si/"))
        self.assertEqual(self.event.slots.values("meeting_link").distinct().count(), self.total)

    def test_building_again_skips_what_exists(self):
        self.build()
        self.assertEqual(self.event.slots.count(), self.total)

    def test_event_detail_shows_one_month_page_by_page(self):
        url = reverse("events:detail", args=[self.event.pk])
        response, count = self.queries(url)
        self.assertLess(count, 40)
        self.assertEqual(len(response.context["slots"]), SLOTS_PER_PAGE)
        self.assertLessEqual(len(response.context["day_chips"]), 31)
        self.assertEqual(response.context["summary"]["slots"], self.total)
        self.assertEqual(response.context["summary"]["seats_left"], self.total)
        self.assertLess(len(response.content), 600_000)
        # Every month of the two years is reachable from the month picker.
        self.assertGreaterEqual(len(response.context["month_options"]), 24)

    def test_month_and_page_narrow_the_slot_table(self):
        later = self.today + datetime.timedelta(days=400)
        url = reverse("events:detail", args=[self.event.pk])
        response, _ = self.queries(url, data={"month": f"{later:%Y-%m}", "page": "2"})
        slots = response.context["slots"]
        self.assertEqual({(s.date.year, s.date.month) for s in slots}, {(later.year, later.month)})
        self.assertEqual(response.context["slot_page"].number, 2)
        response, _ = self.queries(url, data={"date": later.isoformat(), "tod": "morning"})
        self.assertEqual(len(response.context["slots"]), 6 * len(self.hosts))  # 09:00–11:30

    def test_htmx_swap_keeps_the_month_and_its_chips(self):
        later = self.today + datetime.timedelta(days=400)
        slot = self.event.slots.filter(date=later).first()
        page_url = f"http://testserver/events/{self.event.pk}/?month={later:%Y-%m}"
        response = self.client.post(
            reverse("events:slot_close", args=[self.event.pk, slot.pk]),
            HTTP_HX_REQUEST="true", HTTP_HX_CURRENT_URL=page_url,
        )
        self.assertEqual(response.context["selected_month"], f"{later:%Y-%m}")
        self.assertTrue(response.context["day_chips"])
        self.assertContains(response, "Skip day")

    def test_event_list_counts_without_multiplying_rows(self):
        self.book(self.event.slots.first())
        response, count = self.queries(reverse("events:list"))
        self.assertLess(count, 15)
        event = response.context["events"][0]
        self.assertEqual((event.host_count, event.slot_count, event.booking_count),
                         (len(self.hosts), self.total, 1))

    def test_public_pages_stay_bounded(self):
        _, count = self.queries(reverse("bookings_public:public_event", args=["two-year-desk"]))
        self.assertLess(count, 10)
        url = reverse("bookings_public:public_host", args=["two-year-desk", self.hosts[0].pk])
        response, count = self.queries(url)
        self.assertLess(count, 12)
        slots = response.context["slots"]
        self.assertLessEqual(len(slots), 31 * SLOTS_PER_DAY)  # one month, not two years
        self.assertTrue(all(s.host_id == self.hosts[0].pk for s in slots))
        self.assertEqual(slots, sorted(slots, key=lambda s: (s.date, s.start_time)))
        self.assertGreaterEqual(len(response.context["month_options"]), 24)

    def test_public_month_filter_reaches_next_year(self):
        later = self.today + datetime.timedelta(days=500)
        url = reverse("bookings_public:public_host", args=["two-year-desk", self.hosts[0].pk])
        response, _ = self.queries(url, data={"month": f"{later:%Y-%m}"})
        slots = response.context["slots"]
        self.assertTrue(slots)
        self.assertEqual({(s.date.year, s.date.month) for s in slots}, {(later.year, later.month)})
        self.assertTrue(all(s.bookable for s in slots))

    def test_reschedule_picker_lists_the_soonest_slots_only(self):
        from bookings.views import PICKER_SLOT_LIMIT

        booking = self.book(self.event.slots.filter(date__gt=self.today).first())
        response, count = self.queries(
            reverse("bookings_manage:manage_reschedule", args=[booking.manage_token]))
        self.assertLess(count, 12)
        self.assertLessEqual(len(response.context["options"]), PICKER_SLOT_LIMIT)

    def test_clone_copies_two_years_in_bulk_with_its_own_rooms(self):
        with CaptureQueriesContext(connection) as captured:
            self.client.post(reverse("events:clone", args=[self.event.pk]))
        self.assertLess(len(captured), 200)
        clone = Event.objects.exclude(pk=self.event.pk).get()
        self.assertEqual(clone.slots.count(), self.total)
        source_rooms = set(self.event.slots.values_list("meeting_link", flat=True))
        clone_rooms = set(clone.slots.values_list("meeting_link", flat=True))
        self.assertEqual(len(clone_rooms), self.total)
        self.assertFalse(source_rooms & clone_rooms)


class SlotStateRegressionTest(LongScheduleMixin, TestCase):
    """Bugs found while making long schedules workable."""

    def make_slot(self, **kwargs):
        defaults = dict(event=self.event, host=self.hosts[0],
                        date=self.today + datetime.timedelta(days=3),
                        start_time=datetime.time(10, 0))
        defaults.update(kwargs)
        return Slot.objects.create(**defaults)

    def test_reopening_an_empty_closed_slot_makes_it_available(self):
        slot = self.make_slot()
        self.client.post(reverse("events:slot_close", args=[self.event.pk, slot.pk]))
        self.client.post(reverse("events:slot_reopen", args=[self.event.pk, slot.pk]))
        slot.refresh_from_db()
        self.assertEqual(slot.status, Slot.STATUS_AVAILABLE)

    def test_reopening_a_full_closed_slot_stays_booked(self):
        slot = self.make_slot()
        self.book(slot)
        self.client.post(reverse("events:slot_close", args=[self.event.pk, slot.pk]))
        self.client.post(reverse("events:slot_reopen", args=[self.event.pk, slot.pk]))
        slot.refresh_from_db()
        self.assertEqual(slot.status, Slot.STATUS_BOOKED)

    def test_cancelling_a_booking_never_reopens_a_closed_slot(self):
        slot = self.make_slot()
        booking = self.book(slot)
        self.client.post(reverse("events:slot_close", args=[self.event.pk, slot.pk]))
        cancel_booking(booking)
        slot.refresh_from_db()
        self.assertEqual(slot.status, Slot.STATUS_CLOSED)

    def test_booked_column_ignores_cancelled_bookings(self):
        slot = self.make_slot()
        cancel_booking(self.book(slot))
        response = self.client.get(reverse("events:detail", args=[self.event.pk]),
                                   {"date": slot.date.isoformat()})
        self.assertEqual(response.context["slots"][0].confirmed_count, 0)
        self.assertContains(response, "0/1")
        self.assertEqual(Booking.objects.filter(slot=slot).count(), 1)

    def test_book_for_a_visitor_survives_a_bad_event_parameter(self):
        url = reverse("bookings_admin:admin_create")
        self.assertEqual(self.client.get(url, {"event": "abc"}).status_code, 200)
        self.assertEqual(self.client.get(url, {"event": "999999"}).status_code, 200)

    def test_event_page_offers_book_for_a_visitor(self):
        slot = self.make_slot()
        response = self.client.get(reverse("events:detail", args=[self.event.pk]))
        self.assertContains(response, f"slot={slot.pk}")

    def test_deleting_a_booked_slot_notifies_without_orphan_logs(self):
        from django.core import mail
        from notifications.models import NotificationLog

        slot = self.make_slot()
        self.book(slot)
        response = self.client.post(reverse("events:slot_delete", args=[self.event.pk, slot.pk]))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(len(mail.outbox), 1)
        self.assertFalse(Slot.objects.filter(pk=slot.pk).exists())
        self.assertFalse(NotificationLog.objects.filter(booking__isnull=False)
                         .exclude(booking__in=Booking.objects.all()).exists())

    def test_builder_refuses_an_absurdly_large_run(self):
        response = self.build(start_time="00:00", end_time="23:55", duration_minutes="5")
        self.assertContains(response, "Build fewer days")
        self.assertEqual(self.event.slots.count(), 0)
