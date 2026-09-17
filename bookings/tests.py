"""
Tests for the public schedule filter bar (month / weekday / time-of-day /
exact date) and the expired-slot auto-close job.
"""
import datetime

from django.test import TestCase, RequestFactory
from django.utils import timezone

from events.models import Event, Slot, TeamMember
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
