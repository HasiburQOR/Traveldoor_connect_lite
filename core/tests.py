"""
Full-surface walk of both sides of the app.

Every admin screen and every visitor screen is requested for BOTH event types,
and each response is checked for three things:

  1. it returns the status we expect (no 500s, no accidental redirects);
  2. it contains no unresolved template variable — the suite renders with
     ``string_if_invalid`` set to a marker, so a typo'd or missing context
     variable becomes a hard failure instead of a silent blank;
  3. it shows the fields belonging to its event type and none belonging to
     the other one.

This is the regression net for the redesign: templates were rewritten wholesale,
and a rewritten template that renders blank is otherwise invisible.
"""
import copy
import datetime

from django.conf import settings
from django.conf import settings as dj_settings
from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse

from bookings.models import Booking
from bookings.services import create_booking
from events.models import Event, Slot, TeamMember
from core.models import SiteSettings
from notifications.models import HostNotification, SchedulerHeartbeat

MARKER = "XXINVALIDXX"
LINK = "https://meet.google.com/abc-defg-hij"

# Render with a loud marker for anything that fails to resolve.
_TEMPLATES = copy.deepcopy(settings.TEMPLATES)
_TEMPLATES[0].setdefault("OPTIONS", {})["string_if_invalid"] = MARKER


@override_settings(TEMPLATES=_TEMPLATES)
class FullSurfaceWalkTest(TestCase):
    """Requests every page in the product and asserts it renders cleanly."""

    def setUp(self):
        User = get_user_model()
        self.admin = User.objects.create_user(
            username="walker", password="pw-for-tests-123", email="walker@example.com",
            is_staff=True, is_superuser=True,
        )
        self.day = datetime.date.today() + datetime.timedelta(days=2)
        self.online = self._build(Event.TYPE_ONLINE)
        self.offline = self._build(Event.TYPE_OFFLINE)

    # ----- fixtures ---------------------------------------------------------
    def _build(self, event_type):
        kw = dict(
            name=f"{event_type.title()} Event",
            description="Meet our specialists.",
            start_date=self.day - datetime.timedelta(days=1),
            duration_days=6,
            status=Event.STATUS_LIVE,
            public_slug=f"walk-{event_type}",
            event_type=event_type,
        )
        if event_type == Event.TYPE_ONLINE:
            kw.update(default_video_provider=Event.PROVIDER_GOOGLE_MEET, default_meeting_link=LINK)
        else:
            kw.update(venue="Radisson Blu", hall_name="Hall A", table_name="Table 7")
        event = Event.objects.create(**kw)
        host = TeamMember.objects.create(
            event=event, name="Hasibur Rahman", email=f"h-{event_type}@example.com",
            role="Sales Lead", linked_user=self.admin,
        )
        slots = [
            Slot.objects.create(event=event, host=host, date=self.day,
                                start_time=datetime.time(10, 0), duration_minutes=30),
            Slot.objects.create(event=event, host=host, date=self.day,
                                start_time=datetime.time(10, 30), duration_minutes=30),
        ]
        booking = create_booking(
            slot=slots[0], visitor_name="Anika Rahman", visitor_email="anika@example.com",
            visitor_phone="+995 555 000", company="Wanderlust", notes="Looking forward to it.",
        )
        HostNotification.objects.create(
            host=host, booking=booking, event=event,
            kind=HostNotification.KIND_UPCOMING,
            title=f"Meeting with {booking.visitor_name}",
            message="Starts in 20 min.",
            meeting_datetime=slots[0].start_datetime,
            join_url=slots[0].effective_meeting_link if event.is_online else "",
            venue_details="" if event.is_online else "Radisson Blu — Hall A — Table 7",
        )
        return {"event": event, "host": host, "slots": slots, "booking": booking}

    # ----- helpers ----------------------------------------------------------
    def get(self, url, expect=200, **kwargs):
        response = self.client.get(url, **kwargs)
        self.assertEqual(response.status_code, expect, f"GET {url}")
        if expect == 200:
            html = response.content.decode()
            self.assertNotIn(MARKER, html, f"unresolved template variable in {url}")
            return html
        return ""

    def assert_type_purity(self, html, event_type, where):
        """The page shows its own type's fields and none of the other's."""
        if event_type == Event.TYPE_ONLINE:
            for banned in ('name="venue"', 'name="hall_name"', 'name="table_name"',
                           "Radisson Blu", "Table 7"):
                self.assertNotIn(banned, html, f"{where} leaked offline data: {banned}")
        else:
            for banned in ('name="meeting_link"', 'name="video_provider"',
                           'name="default_meeting_link"', LINK, "meet.google.com"):
                self.assertNotIn(banned, html, f"{where} leaked online data: {banned}")

    # ----- visitor side -----------------------------------------------------
    def test_visitor_screens_render_for_both_types(self):
        for event_type in (Event.TYPE_ONLINE, Event.TYPE_OFFLINE):
            fx = self.online if event_type == Event.TYPE_ONLINE else self.offline
            event, host, slots = fx["event"], fx["host"], fx["slots"]
            slug = event.public_slug
            label = f"visitor/{event_type}"

            # Host roster
            html = self.get(reverse("bookings_public:public_event", args=[slug]))
            self.assertIn(host.name, html)
            self.assertIn("Video call" if event.is_online else "In person", html)

            # Booking page + the stub for an open slot
            html = self.get(reverse("bookings_public:public_host", args=[slug, host.pk]))
            self.assertIn("All hosts", html)
            self.assert_type_purity(html, event_type, f"{label} host page")

            stub = self.get(reverse("bookings_public:public_book", args=[slug, slots[1].pk]))
            self.assertIn("Confirm booking", stub)
            if event.is_online:
                self.assertIn("Google Meet", stub)
                self.assertNotIn("In person", stub)
            else:
                self.assertIn("In person", stub)
                for word in ("Radisson Blu", "Hall A", "Table 7"):
                    self.assertIn(word, stub)
                self.assertNotIn("Video call", stub)

            # Manage / reschedule / cancel
            booking = fx["booking"]
            html = self.get(reverse("bookings_manage:manage", args=[booking.manage_token]))
            self.assertIn(booking.visitor_name, html)
            self.assert_type_purity(html, event_type, f"{label} manage page")

            html = self.get(reverse("bookings_manage:manage_reschedule", args=[booking.manage_token]))
            self.assertIn("Change your time", html)

    def test_visitor_can_complete_a_booking_on_both_types(self):
        for event_type in (Event.TYPE_ONLINE, Event.TYPE_OFFLINE):
            fx = self.online if event_type == Event.TYPE_ONLINE else self.offline
            event, slots = fx["event"], fx["slots"]
            mail.outbox.clear()
            response = self.client.post(
                reverse("bookings_public:public_book", args=[event.public_slug, slots[1].pk]),
                {"visitor_name": "Nino K", "visitor_email": f"nino-{event_type}@example.com",
                 "visitor_phone": "", "company": "", "notes": ""},
                HTTP_HX_REQUEST="true",
            )
            self.assertEqual(response.status_code, 200)
            html = response.content.decode()
            self.assertNotIn(MARKER, html)
            self.assertIn("Confirmed", html)
            self.assertTrue(
                Booking.objects.filter(visitor_email=f"nino-{event_type}@example.com").exists()
            )
            self.assertEqual(len(mail.outbox), 2, "visitor + host confirmation emails sent")
            self.assertEqual(
                {m.to[0] for m in mail.outbox},
                {f"nino-{event_type}@example.com", fx["host"].email},
            )

    def test_booking_flow_degrades_without_htmx(self):
        """No JS: the booking URL must serve a full styled page, not a fragment."""
        for event_type in (Event.TYPE_ONLINE, Event.TYPE_OFFLINE):
            fx = self.online if event_type == Event.TYPE_ONLINE else self.offline
            event, slots = fx["event"], fx["slots"]
            url = reverse("bookings_public:public_book", args=[event.public_slug, slots[1].pk])

            # GET without the HX-Request header -> whole ticket page.
            html = self.get(url)
            self.assertIn("<html", html)
            self.assertIn("site.css", html)
            self.assertIn("Confirm booking", html)
            self.assertIn("All hosts", html, "back link must survive the fallback")

            # POST without htmx -> full confirmation page, still styled.
            response = self.client.post(url, {
                "visitor_name": "No JS", "visitor_email": f"nojs-{event_type}@example.com",
                "visitor_phone": "", "company": "", "notes": "",
            })
            self.assertEqual(response.status_code, 200)
            body = response.content.decode()
            self.assertNotIn(MARKER, body)
            self.assertIn("<html", body)
            self.assertIn("site.css", body)
            self.assertIn("Confirmed", body)
            self.assertTrue(
                Booking.objects.filter(visitor_email=f"nojs-{event_type}@example.com").exists())

    def test_slots_are_links_so_they_work_without_js(self):
        fx = self.offline
        html = self.get(reverse("bookings_public:public_host",
                                args=[fx["event"].public_slug, fx["host"].pk]))
        target = reverse("bookings_public:public_book",
                         args=[fx["event"].public_slug, fx["slots"][1].pk])
        self.assertIn(f'href="{target}"', html)

    def test_closed_event_shows_the_closed_state(self):
        event = self.online["event"]
        event.status = Event.STATUS_CLOSED
        event.save(update_fields=["status"])
        html = self.get(reverse("bookings_public:public_event", args=[event.public_slug]))
        self.assertIn("Bookings are closed", html)

    def test_visitor_pages_need_no_login(self):
        self.client.logout()
        event, host = self.online["event"], self.online["host"]
        self.get(reverse("bookings_public:public_event", args=[event.public_slug]))
        self.get(reverse("bookings_public:public_host", args=[event.public_slug, host.pk]))

    def test_public_events_directory_lists_only_bookable_events(self):
        """/b/ is the one link for the website — only live, unexpired events
        appear, each linking straight to its own booking page."""
        Event.objects.create(
            name="Draft Event", start_date=self.day, duration_days=3,
            status=Event.STATUS_DRAFT, event_type=Event.TYPE_ONLINE,
            public_slug="draft-walk",
        )
        Event.objects.create(
            name="Closed Event", start_date=self.day, duration_days=3,
            status=Event.STATUS_CLOSED, event_type=Event.TYPE_ONLINE,
            public_slug="closed-walk",
        )
        Event.objects.create(
            name="Expired Event", start_date=datetime.date.today() - datetime.timedelta(days=5),
            duration_days=1, status=Event.STATUS_LIVE, event_type=Event.TYPE_ONLINE,
            public_slug="expired-walk",
        )

        html = self.get(reverse("bookings_public:public_events"))
        for fx in (self.online, self.offline):
            event = fx["event"]
            self.assertIn(event.name, html)
            self.assertIn(
                f'href="{reverse("bookings_public:public_event", args=[event.public_slug])}"',
                html,
            )
        for hidden in ("Draft Event", "Closed Event", "Expired Event"):
            self.assertNotIn(hidden, html)

    def test_public_events_directory_empty_state_needs_no_login(self):
        self.client.logout()
        Event.objects.all().update(status=Event.STATUS_DRAFT)
        html = self.get(reverse("bookings_public:public_events"))
        self.assertIn("Nothing open for booking yet", html)

    # ----- admin side -------------------------------------------------------
    def test_every_admin_screen_renders_for_both_types(self):
        self.client.force_login(self.admin)
        for event_type in (Event.TYPE_ONLINE, Event.TYPE_OFFLINE):
            fx = self.online if event_type == Event.TYPE_ONLINE else self.offline
            event, host, slots, booking = fx["event"], fx["host"], fx["slots"], fx["booking"]
            label = f"admin/{event_type}"

            pages = {
                "dashboard": reverse("core:dashboard"),
                "event list": reverse("events:list"),
                "type chooser": reverse("events:create"),
                "create form": reverse("events:create") + f"?type={event_type}",
                "event detail": reverse("events:detail", args=[event.pk]),
                "event edit": reverse("events:update", args=[event.pk]),
                "delete confirm": reverse("events:delete_confirm", args=[event.pk]),
                "bookings list": reverse("bookings_admin:admin_list"),
                "bookings attention": reverse("bookings_admin:admin_list") + "?attention=1&status=confirmed&q=Anika",
                "booking detail": reverse("bookings_admin:admin_detail", args=[booking.pk]),
                "booking edit": reverse("bookings_admin:admin_update", args=[booking.pk]),
                "booking new": reverse("bookings_admin:admin_create") + f"?event={event.pk}",
                "booking reschedule": reverse("bookings_admin:admin_reschedule", args=[booking.pk]),
                "audit log": reverse("audit:list"),
                "audit filtered": reverse("audit:list") + "?action=create&entity=Event",
                "notifications": reverse("notifications:list"),
                "users": reverse("accounts:user_list"),
                "user new": reverse("accounts:user_create"),
                "user edit": reverse("accounts:user_edit", args=[self.admin.pk]),
                "password change": reverse("accounts:password_change"),
            }
            for name, url in pages.items():
                html = self.get(url)
                self.assertIn("TravelDoor Connect", html, f"{label} {name} missing admin chrome")

            # These endpoints are htmx fragments: bare partials by design, so
            # they carry no page chrome — but they must still render cleanly.
            fragments = {
                "team add form": reverse("events:team_add", args=[event.pk]),
                "team edit form": reverse("events:team_edit", args=[event.pk, host.pk]),
                "slot add form": reverse("events:slot_add", args=[event.pk]),
                "slot edit form": reverse("events:slot_edit", args=[event.pk, slots[0].pk]),
                "slot build form": reverse("events:slot_build", args=[event.pk]),
            }
            for name, url in fragments.items():
                html = self.get(url, HTTP_HX_REQUEST="true")
                self.assertIn("<form", html, f"{label} {name} is not a usable fragment")
                self.assertNotIn("<body", html, f"{label} {name} should be a fragment, not a page")

            # Type purity on every screen that carries meeting details.
            for name in ("create form", "event detail", "event edit", "booking detail"):
                self.assert_type_purity(self.get(pages[name]), event_type, f"{label} {name}")
            for name in ("slot add form", "slot edit form"):
                self.assert_type_purity(
                    self.get(fragments[name], HTTP_HX_REQUEST="true"), event_type, f"{label} {name}")

    def test_no_screen_shows_more_than_one_gold_action(self):
        """Design rule: at most one gold `.btn-primary` visible per screen."""
        self.client.force_login(self.admin)
        fx = self.offline
        urls = [
            reverse("core:dashboard"), reverse("events:list"),
            reverse("events:create"), reverse("events:create") + "?type=online",
            reverse("events:detail", args=[fx["event"].pk]),
            reverse("events:update", args=[fx["event"].pk]),
            reverse("events:delete_confirm", args=[fx["event"].pk]),
            reverse("bookings_admin:admin_list"),
            reverse("bookings_admin:admin_detail", args=[fx["booking"].pk]),
            reverse("bookings_admin:admin_reschedule", args=[fx["booking"].pk]),
            reverse("audit:list"), reverse("notifications:list"),
            reverse("accounts:user_list"), reverse("accounts:user_create"),
            reverse("accounts:password_change"),
        ]
        for url in urls:
            count = self.get(url).count("btn-primary")
            self.assertLessEqual(count, 1, f"{url} shows {count} gold actions")

        self.client.logout()
        for url in (reverse("accounts:login"),
                    reverse("bookings_public:public_event", args=[fx["event"].public_slug]),
                    reverse("bookings_public:public_host",
                            args=[fx["event"].public_slug, fx["host"].pk]),
                    reverse("bookings_manage:manage", args=[fx["booking"].manage_token]),
                    reverse("bookings_manage:manage_reschedule",
                            args=[fx["booking"].manage_token])):
            count = self.get(url).count("btn-primary")
            self.assertLessEqual(count, 1, f"{url} shows {count} gold actions")

    def test_admin_screens_require_login(self):
        self.client.logout()
        for url in (reverse("core:dashboard"), reverse("events:list"),
                    reverse("bookings_admin:admin_list"), reverse("audit:list"),
                    reverse("accounts:user_list"), reverse("notifications:list")):
            response = self.client.get(url)
            self.assertIn(response.status_code, (302, 403), f"{url} is not protected")

    def test_htmx_partials_swap_cleanly(self):
        """The fragments htmx swaps in must render standalone, not just inline."""
        self.client.force_login(self.admin)
        fx = self.offline
        event, host, slots, booking = fx["event"], fx["host"], fx["slots"], fx["booking"]

        # Team section swap
        response = self.client.post(
            reverse("events:team_reactivate", args=[event.pk, host.pk]), HTTP_HX_REQUEST="true")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(MARKER, response.content.decode())
        self.assertIn("team-section", response.content.decode())

        # Slot section swap
        response = self.client.post(
            reverse("events:slot_close", args=[event.pk, slots[1].pk]), HTTP_HX_REQUEST="true")
        self.assertEqual(response.status_code, 200)
        self.assertIn("slot-section", response.content.decode())

        # Single booking row swap (clear-flag)
        booking.needs_attention = True
        booking.save(update_fields=["needs_attention"])
        response = self.client.post(
            reverse("bookings_admin:admin_clear_flag", args=[booking.pk]), HTTP_HX_REQUEST="true")
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertNotIn(MARKER, html)
        self.assertIn(booking.visitor_name, html)

        # Notification list swap
        response = self.client.post(
            reverse("notifications:mark_all_read"), HTTP_HX_REQUEST="true")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(MARKER, response.content.decode())

    def test_invalid_inline_form_keeps_its_swap_target(self):
        """A rejected inline form must come back inside its section.

        These forms swap `#slot-section` / `#team-section` with outerHTML. If a
        validation error returns the bare form, the swap destroys the section —
        the table vanishes and the target id no longer exists, so every later
        submit silently does nothing. Regression guard for that.
        """
        self.client.force_login(self.admin)
        event, host = self.offline["event"], self.offline["host"]

        cases = [
            # end time before start time
            (reverse("events:slot_build", args=[event.pk]), {
                "host": "", "days": [self.day.isoformat()],
                "start_time": "14:15", "end_time": "12:32",
                "duration_minutes": "30", "capacity": "1"},
             "slot-section"),
            # missing host/date/time
            (reverse("events:slot_add", args=[event.pk]), {
                "host": "", "date": "", "start_time": "",
                "duration_minutes": "30", "capacity": "1"},
             "slot-section"),
            # missing name/email
            (reverse("events:team_add", args=[event.pk]), {"name": "", "email": ""},
             "team-section"),
        ]
        for url, payload, section_id in cases:
            response = self.client.post(url, payload, HTTP_HX_REQUEST="true")
            self.assertEqual(response.status_code, 200, url)
            html = response.content.decode()
            self.assertNotIn(MARKER, html, url)
            self.assertIn(f'id="{section_id}"', html,
                          f"{url} destroyed its own swap target on validation error")
            self.assertIn("errorlist", html, f"{url} lost the validation message")
            self.assertIn("<form", html, f"{url} lost the form the user was filling in")

    def test_builder_form_prefills_a_working_day(self):
        """Empty time inputs are how you get an end-before-start day."""
        self.client.force_login(self.admin)
        event = self.offline["event"]
        html = self.get(reverse("events:slot_build", args=[event.pk]), HTTP_HX_REQUEST="true")
        self.assertIn('value="09:00"', html)
        self.assertIn('value="17:00"', html)
        # The host dropdown defaults to the whole team and lists every host.
        self.assertIn("Every active host", html)
        self.assertIn("Hasibur Rahman", html)
        # Only the event's own days are offered — one checkbox per date, and
        # nothing beyond the event's window can even be submitted.
        for d in event.event_dates():
            self.assertIn(f'value="{d.isoformat()}"', html)
        outside = (event.end_date + datetime.timedelta(days=1)).isoformat()
        self.assertNotIn(f'value="{outside}"', html)

    def test_builder_form_attaches_errors_to_the_offending_field(self):
        self.client.force_login(self.admin)
        event = self.offline["event"]
        response = self.client.post(reverse("events:slot_build", args=[event.pk]), {
            "host": "", "days": [event.start_date.isoformat()],
            "start_time": "14:15", "end_time": "12:32",
            "duration_minutes": "30", "capacity": "1",
        }, HTTP_HX_REQUEST="true")
        html = response.content.decode()
        # The message names both times so the fix is obvious, and sits on the
        # end_time field rather than in a detached banner.
        self.assertIn("12:32", html)
        self.assertIn("14:15", html)
        self.assertNotIn("errorlist nonfield", html)
        self.assertIn('id="slot-section"', html)

    def test_builder_form_rejects_a_day_the_event_doesnt_run(self):
        self.client.force_login(self.admin)
        event = self.offline["event"]
        outside = (event.end_date + datetime.timedelta(days=10)).isoformat()
        response = self.client.post(reverse("events:slot_build", args=[event.pk]), {
            "host": "", "days": [outside],
            "start_time": "09:00", "end_time": "17:00",
            "duration_minutes": "30", "capacity": "1",
        }, HTTP_HX_REQUEST="true")
        self.assertIn("Select a valid choice", response.content.decode())
        self.assertFalse(Slot.objects.filter(event=event, date__gte=outside).exists())

    def test_builder_form_explains_when_there_are_no_hosts(self):
        self.client.force_login(self.admin)
        event = Event.objects.create(
            name="Hostless", start_date=self.day, duration_days=2,
            event_type=Event.TYPE_ONLINE, default_video_provider=Event.PROVIDER_ZOOM,
            default_meeting_link=LINK,
        )
        html = self.get(reverse("events:slot_build", args=[event.pk]), HTTP_HX_REQUEST="true")
        self.assertIn("Add a host first", html)
        self.assertNotIn("Build schedule", html)

    def test_builder_still_works(self):
        self.client.force_login(self.admin)
        event = self.offline["event"]
        before = event.slots.count()
        self.client.post(reverse("events:slot_build", args=[event.pk]), {
            "host": "", "days": [event.start_date.isoformat()],
            "start_time": "09:00", "end_time": "10:00",
            "duration_minutes": "30", "capacity": "1",
        }, HTTP_HX_REQUEST="true")
        self.assertEqual(event.slots.count(), before + 2, "two 30-min slots in a 1-hour day")

    def test_in_app_notification_component_matches_the_event_type(self):
        self.client.force_login(self.admin)
        html = self.get(reverse("notifications:list"))
        # Online meeting -> a clickable join control; offline -> venue, no link.
        self.assertIn("Join the video call", html)
        self.assertIn("Radisson Blu", html)
        self.assertEqual(html.count("Join the video call"), 1,
                         "only the online notification may offer a join link")


@override_settings(TEMPLATES=_TEMPLATES)
class EmailTemplateWalkTest(TestCase):
    """Every email template renders for both event types with no stray marker."""

    def setUp(self):
        User = get_user_model()
        self.admin = User.objects.create_user(
            username="mailer", password="pw-for-tests-123", email="mailer@example.com",
            is_staff=True,
        )
        self.day = datetime.date.today() + datetime.timedelta(days=2)

    def _booking(self, event_type):
        kw = dict(name="Mail Event", start_date=self.day, duration_days=3,
                  status=Event.STATUS_LIVE, public_slug=f"mail-{event_type}",
                  event_type=event_type)
        if event_type == Event.TYPE_ONLINE:
            kw.update(default_video_provider=Event.PROVIDER_ZOOM, default_meeting_link=LINK)
        else:
            kw.update(venue="Radisson Blu", hall_name="Hall A", table_name="Table 7")
        event = Event.objects.create(**kw)
        host = TeamMember.objects.create(event=event, name="Nino K", email="nino@example.com")
        slot = Slot.objects.create(event=event, host=host, date=self.day,
                                   start_time=datetime.time(9, 30))
        return create_booking(slot=slot, visitor_name="Anika Rahman",
                              visitor_email="anika@example.com", visitor_phone="",
                              company="Wanderlust", notes="")

    def test_all_transactional_emails_render_for_both_types(self):
        from notifications.emails import (
            send_admin_account_email, send_attention_email, send_booking_confirmation,
            send_cancellation_to_host, send_cancellation_to_visitor, send_reminder,
        )

        for event_type in (Event.TYPE_ONLINE, Event.TYPE_OFFLINE):
            booking = self._booking(event_type)
            mail.outbox.clear()
            send_booking_confirmation(booking)
            send_booking_confirmation(booking, is_reschedule=True)
            send_reminder(booking, 24)
            send_cancellation_to_visitor(booking, reason="Testing.")
            send_cancellation_to_host(booking, reason="Testing.")
            send_attention_email(booking, reason="Testing.")
            self.assertEqual(len(mail.outbox), 6, f"{event_type}: all six emails sent")

            for message in mail.outbox:
                bodies = [message.body] + [b for b, _ in message.alternatives]
                for body in bodies:
                    self.assertNotIn(MARKER, body,
                                     f"{event_type}: unresolved variable in {message.subject}")
                    if event_type == Event.TYPE_ONLINE:
                        self.assertNotIn("Radisson Blu", body)
                        self.assertNotIn("Table 7", body)
                    else:
                        self.assertNotIn(LINK, body)
                        self.assertNotIn("meet.google.com", body)

        mail.outbox.clear()
        send_admin_account_email(self.admin, "one-time-password")
        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        for body in [message.body] + [b for b, _ in message.alternatives]:
            self.assertNotIn(MARKER, body)
            self.assertIn("one-time-password", body)


@override_settings(TEMPLATES=_TEMPLATES)
class AdminListTest(TestCase):
    """Company column, filters, pagination and CSV export on the booking list."""

    def setUp(self):
        User = get_user_model()
        self.admin = User.objects.create_user(
            username="lister", password="pw-for-tests-123", email="lister@example.com",
            is_staff=True, is_superuser=True,
        )
        self.client.force_login(self.admin)
        self.day = datetime.date.today() + datetime.timedelta(days=2)
        self.event = Event.objects.create(
            name="List Event", start_date=self.day, duration_days=3,
            status=Event.STATUS_LIVE, public_slug="list-event",
            event_type=Event.TYPE_OFFLINE, venue="Radisson Blu",
            hall_name="Hall A", table_name="Table 7",
        )
        self.other = Event.objects.create(
            name="Other Event", start_date=self.day, duration_days=3,
            event_type=Event.TYPE_ONLINE, default_video_provider=Event.PROVIDER_ZOOM,
            default_meeting_link=LINK,
        )
        self.host = TeamMember.objects.create(
            event=self.event, name="Hasibur Rahman", email="h@example.com")
        self.booking = self._book(0, "Anika Rahman", "anika@example.com", "Wanderlust Travels")

    def _book(self, minute_offset, name, email, company):
        start = (datetime.datetime.combine(self.day, datetime.time(9, 0))
                 + datetime.timedelta(minutes=minute_offset)).time()
        slot = Slot.objects.create(
            event=self.event, host=self.host, date=self.day, start_time=start)
        return create_booking(slot=slot, visitor_name=name, visitor_email=email,
                              visitor_phone="", company=company, notes="")

    def test_company_is_shown_in_the_list(self):
        html = self.client.get(reverse("bookings_admin:admin_list")).content.decode()
        self.assertNotIn(MARKER, html)
        self.assertIn("<th>Company</th>", html)
        self.assertIn("Wanderlust Travels", html)

    def test_filtering_by_event(self):
        url = reverse("bookings_admin:admin_list")
        html = self.client.get(url, {"event": self.event.pk}).content.decode()
        self.assertIn("Anika Rahman", html)
        html = self.client.get(url, {"event": self.other.pk}).content.decode()
        self.assertNotIn("Anika Rahman", html)

    def test_search_matches_name_email_or_company(self):
        url = reverse("bookings_admin:admin_list")
        for term in ("Anika", "anika@example.com", "Wanderlust"):
            html = self.client.get(url, {"q": term}).content.decode()
            self.assertIn("Anika Rahman", html, term)
        html = self.client.get(url, {"q": "nobody"}).content.decode()
        self.assertIn("No bookings match", html)

    def test_pagination_splits_long_lists_and_keeps_filters(self):
        for i in range(1, 30):
            self._book(i * 30, "Visitor %d" % i, "v%d@example.com" % i, "Acme")
        url = reverse("bookings_admin:admin_list")
        page1 = self.client.get(url).content.decode()
        self.assertIn("of 30 bookings", page1)
        self.assertIn("page=2", page1)
        page2 = self.client.get(url, {"page": 2}).content.decode()
        self.assertNotIn(MARKER, page2)
        self.assertIn("Page 2 of 2", page2)
        filtered = self.client.get(url, {"q": "Acme", "page": 2}).content.decode()
        self.assertIn("q=Acme", filtered)

    def test_out_of_range_page_does_not_error(self):
        response = self.client.get(reverse("bookings_admin:admin_list"), {"page": "99"})
        self.assertEqual(response.status_code, 200)
        response = self.client.get(reverse("bookings_admin:admin_list"), {"page": "abc"})
        self.assertEqual(response.status_code, 200)

    def test_csv_export_matches_the_current_filters(self):
        self._book(90, "Bob Other", "bob@example.com", "Globex")
        url = reverse("bookings_admin:admin_export")
        response = self.client.get(url)
        self.assertEqual(response["Content-Type"], "text/csv")
        self.assertIn("attachment; filename=", response["Content-Disposition"])
        body = response.content.decode()
        self.assertIn("Wanderlust Travels", body)
        self.assertIn("Globex", body)
        body = self.client.get(url, {"q": "Globex"}).content.decode()
        self.assertIn("Globex", body)
        self.assertNotIn("Wanderlust Travels", body)

    def test_export_is_audited_because_it_contains_personal_data(self):
        from audit.models import AuditLogEntry

        self.client.get(reverse("bookings_admin:admin_export"))
        self.assertTrue(
            AuditLogEntry.objects.filter(entity_repr="Booking CSV export",
                                         actor=self.admin).exists())

    def test_event_list_shows_counts_and_management_actions(self):
        html = self.client.get(reverse("events:list")).content.decode()
        self.assertNotIn(MARKER, html)
        self.assertIn("1 host", html)
        self.assertIn("Radisson Blu", html)
        self.assertIn(reverse("events:delete_confirm", args=[self.event.pk]), html)
        self.assertIn(reverse("events:clone", args=[self.event.pk]), html)

    def test_event_detail_shows_a_summary_strip(self):
        html = self.client.get(reverse("events:detail", args=[self.event.pk])).content.decode()
        self.assertNotIn(MARKER, html)
        for label in ("Active hosts", "Bookable slots", "Confirmed bookings", "Days left"):
            self.assertIn(label, html)


@override_settings(TEMPLATES=_TEMPLATES)
class EmailDiagnosticsTest(TestCase):
    """Delivery problems must be visible, not silent."""

    def setUp(self):
        User = get_user_model()
        self.admin = User.objects.create_user(
            username="ops", password="pw-for-tests-123", email="ops@example.com",
            is_staff=True, is_superuser=True,
        )
        self.client.force_login(self.admin)

    def test_dashboard_warns_when_mail_is_not_actually_sent(self):
        html = self.client.get(reverse("core:dashboard")).content.decode()
        self.assertNotIn(MARKER, html)
        # The test runner uses the locmem backend, i.e. not SMTP.
        self.assertIn("Emails are not being sent", html)
        self.assertIn("EMAIL_BACKEND=smtp", html)

    @override_settings(PUBLIC_BASE_URL="http://127.0.0.1:8000")
    def test_dashboard_warns_when_email_links_point_at_localhost(self):
        html = self.client.get(reverse("core:dashboard")).content.decode()
        self.assertIn("PUBLIC_BASE_URL", html)

    def test_dashboard_warns_when_the_scheduler_has_never_run(self):
        html = self.client.get(reverse("core:dashboard")).content.decode()
        self.assertIn("Background jobs have never run", html)

    def test_heartbeat_clears_the_scheduler_warning(self):
        from notifications.jobs import run_all_jobs

        run_all_jobs()
        heartbeat = SchedulerHeartbeat.current()
        self.assertIsNotNone(heartbeat.last_run_at)
        html = self.client.get(reverse("core:dashboard")).content.decode()
        self.assertNotIn("Background jobs have never run", html)
        self.assertIn("Background jobs last ran", html)

    def test_send_test_email_reports_back(self):
        response = self.client.post(reverse("core:test_email"), follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("ops@example.com", mail.outbox[0].to)
        self.assertIn("Test email", response.content.decode())

    def test_send_test_email_needs_an_address(self):
        self.admin.email = ""
        self.admin.save()
        response = self.client.post(reverse("core:test_email"), follow=True)
        self.assertIn("Add an email address", response.content.decode())
        self.assertEqual(len(mail.outbox), 0)


@override_settings(TEMPLATES=_TEMPLATES)
class ReminderVisibilityTest(TestCase):
    """An admin can see whether a visitor's reminder actually went out."""

    def setUp(self):
        User = get_user_model()
        self.admin = User.objects.create_user(
            username="reminder", password="pw-for-tests-123", is_staff=True, is_superuser=True)
        self.client.force_login(self.admin)
        day = datetime.date.today() + datetime.timedelta(days=2)
        self.event = Event.objects.create(
            name="Reminder Event", start_date=day, duration_days=2,
            status=Event.STATUS_LIVE, event_type=Event.TYPE_ONLINE,
            default_video_provider=Event.PROVIDER_ZOOM, default_meeting_link=LINK,
            reminder_hours_csv="24,1",
        )
        host = TeamMember.objects.create(event=self.event, name="Host", email="h@example.com")
        slot = Slot.objects.create(event=self.event, host=host, date=day,
                                   start_time=datetime.time(10, 0))
        self.booking = create_booking(slot=slot, visitor_name="Anika", visitor_email="a@example.com",
                                      visitor_phone="", company="", notes="")

    def test_scheduled_reminders_are_listed(self):
        html = self.client.get(
            reverse("bookings_admin:admin_detail", args=[self.booking.pk])).content.decode()
        self.assertNotIn(MARKER, html)
        self.assertIn("24h before", html)
        self.assertIn("1h before", html)
        self.assertIn("Scheduled", html)

    def test_a_sent_reminder_is_shown_as_sent(self):
        from notifications.emails import send_reminder

        send_reminder(self.booking, 24)
        html = self.client.get(
            reverse("bookings_admin:admin_detail", args=[self.booking.pk])).content.decode()
        self.assertIn("Sent", html)

    def test_resend_confirmation_sends_and_audits(self):
        from audit.models import AuditLogEntry

        mail.outbox.clear()
        self.client.post(reverse("bookings_admin:admin_resend", args=[self.booking.pk]))
        self.assertEqual(len(mail.outbox), 2, "visitor + host confirmation re-sent")
        self.assertEqual({m.to[0] for m in mail.outbox},
                         {"a@example.com", "h@example.com"})
        self.assertTrue(AuditLogEntry.objects.filter(
            entity_id=self.booking.pk, details__icontains="re-sent").exists())


@override_settings(TEMPLATES=_TEMPLATES)
class SiteSettingsTest(TestCase):
    """Admin-editable configuration: mailbox, branding, and who may change it."""

    def setUp(self):
        User = get_user_model()
        self.owner = User.objects.create_user(
            username="owner", password="pw-for-tests-123", email="owner@example.com",
            is_staff=True, is_superuser=True)
        self.staff = User.objects.create_user(
            username="staffonly", password="pw-for-tests-123", email="s@example.com",
            is_staff=True, is_superuser=False)

    def _post(self, **overrides):
        data = {
            "company_name": "TravelDoor",
            "public_base_url": "https://book.traveldoor.ge",
            "from_name": "TravelDoor Bookings",
            "from_email": "bookings@traveldoor.ge",
            "email_host": "smtp.gmail.com",
            "email_port": "587",
            "email_host_user": "bookings@traveldoor.ge",
            "email_host_password": "app-password-123",
            "email_use_tls": "on",
            "default_reminder_hours": "24,1",
        }
        data.update(overrides)
        return self.client.post(reverse("core:settings"), data)

    # ----- access ---------------------------------------------------------
    def test_only_superusers_may_open_settings(self):
        self.client.force_login(self.staff)
        response = self.client.get(reverse("core:settings"))
        self.assertIn(response.status_code, (302, 403))
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(reverse("core:settings")).status_code, 200)

    def test_settings_link_only_shows_for_superusers(self):
        self.client.force_login(self.staff)
        self.assertNotIn(reverse("core:settings"),
                         self.client.get(reverse("core:dashboard")).content.decode())
        self.client.force_login(self.owner)
        self.assertIn(reverse("core:settings"),
                      self.client.get(reverse("core:dashboard")).content.decode())

    def test_page_renders_cleanly(self):
        self.client.force_login(self.owner)
        html = self.client.get(reverse("core:settings")).content.decode()
        self.assertNotIn(MARKER, html)
        for section in ("Company", "Outgoing email", "Booking defaults"):
            self.assertIn(section, html)

    # ----- saving ---------------------------------------------------------
    def test_saving_switches_the_sending_mailbox(self):
        self.client.force_login(self.owner)
        self._post()
        site = SiteSettings.load()
        self.assertEqual(site.email_host, "smtp.gmail.com")
        self.assertEqual(site.from_email, "bookings@traveldoor.ge")
        self.assertEqual(site.updated_by, self.owner)

        from core.runtime import from_email, email_is_sending, public_base_url

        self.assertEqual(from_email(), "TravelDoor Bookings <bookings@traveldoor.ge>")
        self.assertTrue(email_is_sending())
        self.assertEqual(public_base_url(), "https://book.traveldoor.ge")

    def test_saved_settings_are_used_for_outgoing_mail(self):
        self.client.force_login(self.owner)
        self._post()
        day = datetime.date.today() + datetime.timedelta(days=2)
        event = Event.objects.create(
            name="Mail Event", start_date=day, duration_days=2, status=Event.STATUS_LIVE,
            event_type=Event.TYPE_ONLINE, default_video_provider=Event.PROVIDER_ZOOM,
            default_meeting_link=LINK)
        host = TeamMember.objects.create(event=event, name="Host", email="h@example.com")
        slot = Slot.objects.create(event=event, host=host, date=day,
                                   start_time=datetime.time(10, 0))
        booking = create_booking(slot=slot, visitor_name="A", visitor_email="a@example.com",
                                 visitor_phone="", company="", notes="")
        mail.outbox.clear()
        from notifications.emails import send_booking_confirmation

        send_booking_confirmation(booking)
        self.assertEqual(mail.outbox[0].from_email,
                         "TravelDoor Bookings <bookings@traveldoor.ge>")
        # Links in the email use the configured public URL, not localhost.
        self.assertIn("https://book.traveldoor.ge", mail.outbox[0].body)

    # ----- password handling ----------------------------------------------
    def test_password_is_encrypted_at_rest_and_never_rendered(self):
        self.client.force_login(self.owner)
        self._post()
        site = SiteSettings.load()
        self.assertEqual(site.email_host_password, "app-password-123")
        # Stored form is not the plaintext.
        self.assertNotIn("app-password-123", site.email_host_password_encrypted)
        # And it never comes back to the browser.
        html = self.client.get(reverse("core:settings")).content.decode()
        self.assertNotIn("app-password-123", html)
        self.assertIn("A password is saved", html)

    def test_blank_password_keeps_the_saved_one(self):
        self.client.force_login(self.owner)
        self._post()
        self._post(email_host_password="", company_name="Renamed")
        site = SiteSettings.load()
        self.assertEqual(site.company_name, "Renamed")
        self.assertEqual(site.email_host_password, "app-password-123")

    def test_password_can_be_removed_explicitly(self):
        self.client.force_login(self.owner)
        self._post()
        self._post(email_host_password="", clear_email_password="on")
        self.assertEqual(SiteSettings.load().email_host_password, "")

    def test_unreadable_password_reads_as_unset_rather_than_crashing(self):
        """A rotated SECRET_KEY must not take the app down."""
        site = SiteSettings.load()
        site.email_host_password_encrypted = "not-a-valid-fernet-token"
        site.save()
        self.assertEqual(SiteSettings.load().email_host_password, "")

    # ----- validation ------------------------------------------------------
    def test_smtp_server_requires_a_sender_and_a_password(self):
        self.client.force_login(self.owner)
        response = self._post(from_email="", email_host_password="")
        form = response.context["form"]
        self.assertIn("from_email", form.errors)
        self.assertIn("email_host_password", form.errors)

    def test_tls_and_ssl_are_mutually_exclusive(self):
        self.client.force_login(self.owner)
        response = self._post(email_use_ssl="on")
        self.assertIn("email_use_ssl", response.context["form"].errors)

    def test_reminder_hours_must_be_whole_positive_hours(self):
        self.client.force_login(self.owner)
        response = self._post(default_reminder_hours="24,soon")
        self.assertIn("default_reminder_hours", response.context["form"].errors)

    def test_blank_settings_fall_back_to_the_environment(self):
        """An untouched install keeps behaving exactly as before."""
        from core.runtime import company_name, email_is_sending, from_email

        self.assertEqual(company_name(), dj_settings.COMPANY_NAME)
        self.assertEqual(from_email(), dj_settings.DEFAULT_FROM_EMAIL)
        # Test runner uses locmem, so nothing is really sending.
        self.assertFalse(email_is_sending())

    def test_event_reminder_default_follows_settings(self):
        self.client.force_login(self.owner)
        self._post(default_reminder_hours="48,2")
        day = datetime.date.today() + datetime.timedelta(days=3)
        event = Event.objects.create(
            name="Defaults", start_date=day, duration_days=1,
            event_type=Event.TYPE_ONLINE, default_video_provider=Event.PROVIDER_ZOOM,
            default_meeting_link=LINK)
        self.assertEqual(event.reminder_hours(), [48, 2])
