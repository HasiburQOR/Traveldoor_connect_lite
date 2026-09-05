"""
End-to-end smoke test for TravelDoor Connect.

Run inside the project environment:
    python manage.py shell -c "exec(open('scripts/smoke_test.py', encoding='utf-8').read())"

Covers: event creation -> team -> bulk slots -> publish -> public booking ->
confirmation email log -> manage link (reschedule + cancel) -> admin views ->
host removal attention flag -> admin user creation -> scheduler tick ->
event delete cascade.
"""
import datetime
import secrets

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import Client
from django.urls import reverse

from audit.models import AuditLogEntry
from bookings.models import Booking
from events.models import Event, Person, Slot, TeamMember
from notifications.models import HostNotification, NotificationLog

step = 0


def check(cond, msg):
    global step
    step += 1
    assert cond, f"FAIL step {step}: {msg}"
    print(f"PASS {step:02d}: {msg}")


# The script provisions its own throwaway superuser rather than depending on a
# hardcoded developer password — that account's password can be changed at any
# time, which would break this script for everyone.
User = get_user_model()
SMOKE_USER = "smoke-admin"
SMOKE_PASSWORD = secrets.token_urlsafe(18)
smoke_admin, _ = User.objects.get_or_create(
    username=SMOKE_USER, defaults={"email": "smoke-admin@example.invalid"})
smoke_admin.is_staff = smoke_admin.is_superuser = smoke_admin.is_active = True
smoke_admin.set_password(SMOKE_PASSWORD)
smoke_admin.save()

client = Client()
check(client.login(username=SMOKE_USER, password=SMOKE_PASSWORD), "admin can log in")

d1 = datetime.date.today() + datetime.timedelta(days=3)

# --- 1. Event CRUD -----------------------------------------------------------
r = client.post(reverse("events:create") + "?type=offline", {
    "event_type": "offline",
    "name": "Roadshow 2026", "description": "Smoke-test event",
    "start_date": d1.isoformat(), "duration_days": "2",
    "venue": "Grand Hall", "hall_name": "Hall A", "table_name": "Table 1",
    "reminder_hours_csv": "24,1", "inapp_lead_minutes": "30",
    "same_day_summary": "on", "require_phone": "on", "require_company": "",
})
event = Event.objects.get(name="Roadshow 2026")
check(r.status_code == 302 and event.status == "draft", "event created (draft)")
check(AuditLogEntry.objects.filter(entity_type="Event", action="create").exists(),
      "event creation audited")
check(event.event_type == "offline", "event is strictly offline")
r = client.get(reverse("events:create"))
check(r.status_code == 200 and b"What kind of event" in r.content,
      "event creation starts with the type chooser")
r = client.get(reverse("events:update", args=[event.pk]))
check(b'name="venue"' in r.content and b'name="default_meeting_link"' not in r.content,
      "offline edit form shows venue fields and no link field")

# --- 2. Team (people directory -> host picker) --------------------------------
for name, email, role in [("Alice Host", "alice@example.com", "Sales Lead"),
                          ("Bob Host", "bob@example.com", "Engineer")]:
    client.post(reverse("events_people:create"), {
        "name": name, "email": email, "role": role,
        "photo_url": "", "photo": "", "linked_user": "", "is_active": "on",
    })
client.post(reverse("events:team_add", args=[event.pk]), {
    "people": list(Person.objects.filter(email__in=["alice@example.com",
                                                    "bob@example.com"]).values_list("pk", flat=True)),
})
check(TeamMember.objects.filter(event=event).count() == 2, "two hosts added")

# --- 3. Build the schedule (offline, event venue defaults) --------------------
client.post(reverse("events:slot_build", args=[event.pk]), {
    "host": "", "days": [d1.isoformat()],
    "start_time": "09:00", "end_time": "10:30",
    "duration_minutes": "30", "capacity": "1",
})
slots = list(event.slots.order_by("start_time"))
check(len(slots) == 6, f"builder created 6 slots across 2 hosts (got {len(slots)})")
check(all(s.mode == "offline" for s in slots), "generated slots inherit the event type")
check(all(not s.meeting_link for s in slots), "offline slots carry no meeting link")
check(event.event_type_locked, "event type locks once slots exist")
r = client.get(reverse("events:update", args=[event.pk]))
check(b"Event type is locked" in r.content, "edit form explains why the type is locked")

# --- 4. Publish --------------------------------------------------------------
client.post(reverse("events:publish", args=[event.pk]))
event.refresh_from_db()
check(event.status == "live" and bool(event.public_slug), "event published with public slug")
slots = list(event.slots.order_by("start_time").select_related("event", "host"))  # refresh post-publish
check(all(s.is_publicly_bookable() for s in slots), "all offline slots publicly bookable once live")

# --- 5. Public pages ---------------------------------------------------------
slots = list(event.slots.order_by("start_time").select_related("event", "host"))  # refresh post-publish
r = client.get(reverse("bookings_public:public_event", args=[event.public_slug]))
check(r.status_code == 200 and b"Alice Host" in r.content, "public roster page renders")

host_a = TeamMember.objects.get(event=event, name="Alice Host")
r = client.get(reverse("bookings_public:public_host", args=[event.public_slug, host_a.pk]))
check(r.status_code == 200 and b'class="slot"' in r.content, "host page lists open times")
check(b"All hosts" in r.content, "host page keeps the back link")

alice_slots = [s for s in slots if s.host_id == host_a.pk]
slot1, slot2 = alice_slots[0], alice_slots[1]
r = client.get(reverse("bookings_public:public_book", args=[event.public_slug, slot1.pk]),
               HTTP_HX_REQUEST="true")
check(r.status_code == 200 and b"Confirm booking" in r.content, "booking form partial renders")
check(b"In person" in r.content and b"Table 1" in r.content,
      "offline booking stub shows venue details, not a meeting mode pill")
check(b"meet.google.com" not in r.content and b"Video call" not in r.content,
      "offline booking stub has no video-link references")

# --- 6. Visitor books --------------------------------------------------------
r = client.post(reverse("bookings_public:public_book", args=[event.public_slug, slot1.pk]), {
    "visitor_name": "Vera Visitor", "visitor_email": "vera@example.com",
    "visitor_phone": "+44 7700 900123", "company": "Acme Ltd", "notes": "Hello!",
}, HTTP_HX_REQUEST="true")
booking = Booking.objects.get(visitor_email="vera@example.com")
check(booking.status == "confirmed", "booking confirmed")
check(b"You&#x27;re booked" in r.content or b"You're booked" in r.content,
      "success partial shown")
check(NotificationLog.objects.filter(booking=booking, type="confirmation", status="sent").exists(),
      "confirmation email sent + logged")
check(AuditLogEntry.objects.filter(entity_type="Booking", entity_id=booking.pk,
                                    actor__isnull=True).exists(),
      "visitor booking audited with no actor")
slot1.refresh_from_db()
check(slot1.status == "booked", "slot flipped to booked (capacity 1)")

# --- 7. Double-booking prevented --------------------------------------------
client.post(reverse("bookings_public:public_book", args=[event.public_slug, slot1.pk]), {
    "visitor_name": "Second Person", "visitor_email": "second@example.com",
    "visitor_phone": "123", "company": "", "notes": "",
}, HTTP_HX_REQUEST="true")
check(Booking.objects.filter(visitor_email="second@example.com").count() == 0,
      "second booking on full slot rejected")

# --- 8. Manage link: reschedule + cancel -------------------------------------
token = booking.manage_token
r = client.get(reverse("bookings_manage:manage", args=[token]))
check(r.status_code == 200 and b"Vera Visitor" in r.content, "manage link page renders")
client.post(reverse("bookings_manage:manage_reschedule", args=[token]), {"slot": slot2.pk})
booking.refresh_from_db()
new_booking = Booking.objects.get(visitor_email="vera@example.com", status="confirmed")
check(booking.status == "cancelled" and new_booking.slot_id == slot2.pk,
      "visitor rescheduled (old booking cancelled, fresh booking on new slot)")
slot1.refresh_from_db(); slot2.refresh_from_db()
check(slot1.status == "available" and slot2.status == "booked", "slots updated after reschedule")
check(NotificationLog.objects.filter(booking=new_booking, type="reschedule").exists(),
      "reschedule email logged")
check(NotificationLog.objects.filter(booking=booking, type="cancellation",
                                     recipient_email="alice@example.com").exists(),
      "old host notified of release")

client.post(reverse("bookings_manage:manage_cancel", args=[new_booking.manage_token]))
new_booking.refresh_from_db()
check(new_booking.status == "cancelled", "visitor cancelled via manage link")
slot2.refresh_from_db()
check(slot2.status == "available", "cancelled slot reopens immediately (FR-3.3)")

# --- 9. Attention flow (FR-2.3) ----------------------------------------------
client.post(reverse("bookings_public:public_book", args=[event.public_slug, slot2.pk]), {
    "visitor_name": "Vera Visitor", "visitor_email": "vera2@example.com",
    "visitor_phone": "+44 7700 900124", "company": "", "notes": "",
}, HTTP_HX_REQUEST="true")
booking2 = Booking.objects.get(visitor_email="vera2@example.com")
client.post(reverse("events:team_remove", args=[event.pk, host_a.pk]))
booking2.refresh_from_db()
check(booking2.needs_attention is True, "booking flagged needs_attention on host removal")
check(HostNotification.objects.filter(host=host_a, kind=HostNotification.KIND_FLAGS).exists(),
      "in-app FLAGS notification created")
check(NotificationLog.objects.filter(booking=booking2, type="attention").exists(),
      "attention email logged")

# --- 10. Admin views render ---------------------------------------------------
r = client.get(reverse("bookings_admin:admin_list") + "?attention=1")
check(r.status_code == 200, "admin booking list (attention filter) renders")
r = client.get(reverse("bookings_admin:admin_detail", args=[booking2.pk]))
check(r.status_code == 200 and b"History" in r.content, "booking detail + history render")
check(b"Join link" not in r.content, "offline booking detail shows no join link")
r = client.post(reverse("bookings_admin:admin_clear_flag", args=[booking2.pk]),
                HTTP_HX_REQUEST="true")
booking2.refresh_from_db()
check(booking2.needs_attention is False and r.status_code == 200, "attention flag cleared (htmx)")

r = client.get(reverse("bookings_admin:admin_reschedule", args=[booking2.pk]))
check(r.status_code == 200, "admin reschedule page renders")

# --- 11. Other admin pages ----------------------------------------------------
for name, url in [
    ("dashboard", reverse("core:dashboard")),
    ("event list", reverse("events:list")),
    ("event detail", reverse("events:detail", args=[event.pk])),
    ("audit", reverse("audit:list") + "?action=update"),
    ("notifications", reverse("notifications:list")),
    ("user list", reverse("accounts:user_list")),
    ("new user", reverse("accounts:user_create")),
    ("new booking", reverse("bookings_admin:admin_create")),
    ("health", reverse("core:health")),
]:
    r = client.get(url)
    check(r.status_code == 200, f"{name} page renders ({r.status_code})")

# --- 12. Create admin user + welcome email (FR-8.2) ---------------------------
User = get_user_model()
client.post(reverse("accounts:user_create"), {
    "username": "ops1", "email": "ops1@example.com", "first_name": "Ops", "last_name": "One",
    "is_superuser": "", "password1": "Tr4v3lD00r!Connect", "password2": "Tr4v3lD00r!Connect",
    "send_credentials_email": "on",
})
check(User.objects.filter(username="ops1", is_staff=True).exists(), "admin user created (staff)")
check(NotificationLog.objects.filter(type="admin_account", recipient_email="ops1@example.com").exists(),
      "welcome/credentials email logged")
check(client.login(username="ops1", password="Tr4v3lD00r!Connect"), "new admin can log in")

# --- 12b. Online event: the mirror-image branch --------------------------------
client.login(username=SMOKE_USER, password=SMOKE_PASSWORD)
Event.objects.filter(name="Webinar Week").delete()  # leftover from a prior run
r = client.post(reverse("events:create") + "?type=online", {
    "event_type": "online",
    "name": "Webinar Week", "description": "",
    "start_date": d1.isoformat(), "duration_days": "1",
    "default_video_provider": "google_meet",
    "default_meeting_link": "https://meet.google.com/abc-defg-hij",
    "reminder_hours_csv": "", "inapp_lead_minutes": "30",
})
online = Event.objects.get(name="Webinar Week")
check(online.event_type == "online" and online.venue == "",
      "online event created with no venue data")
r = client.get(reverse("events:update", args=[online.pk]))
check(b'name="venue"' not in r.content and b'name="default_meeting_link"' in r.content,
      "online edit form shows the link field and no venue fields")
client.post(reverse("events_people:create"), {
    "name": "Cara Host", "email": "cara@example.com", "role": "",
    "photo_url": "", "photo": "", "linked_user": "", "is_active": "on",
})
client.post(reverse("events:team_add", args=[online.pk]), {
    "people": [Person.objects.get(email="cara@example.com").pk],
})
cara = TeamMember.objects.get(event=online, name="Cara Host")
r = client.get(reverse("events:slot_add", args=[online.pk]))
check(b'name="venue"' not in r.content and b'name="meeting_link"' in r.content,
      "online slot form shows the link field and no venue fields")
client.post(reverse("events:slot_add", args=[online.pk]), {
    "host": cara.pk, "date": d1.isoformat(), "start_time": "11:00",
    "duration_minutes": "30", "capacity": "1", "video_provider": "", "meeting_link": "",
})
online_slot = Slot.objects.get(event=online, host=cara)
check(online_slot.mode == "online" and online_slot.venue == "",
      "online slot inherits the type and carries no venue")
check(online_slot.effective_meeting_link == "https://meet.google.com/abc-defg-hij",
      "online slot falls back to the event default link")
client.post(reverse("events:publish", args=[online.pk]))
online.refresh_from_db()
r = client.get(reverse("bookings_public:public_book", args=[online.public_slug, online_slot.pk]))
check(b"Google Meet" in r.content and b"In person" not in r.content,
      "online booking stub shows the video pill, not venue details")

# --- 13. Scheduler tick --------------------------------------------------------
call_command("runscheduler", once=True)
check(True, "runscheduler --once completed")

# --- 14. Event delete cascade (FR-1.2) -----------------------------------------
r = client.get(reverse("events:delete_confirm", args=[event.pk]))
check(r.status_code == 200 and b"vera2@example.com" in r.content,
      "delete confirmation lists affected bookings")
client.post(reverse("events:delete", args=[event.pk]))
check(not Event.objects.filter(pk=event.pk).exists(), "event deleted")
check(not Slot.objects.filter(event_id=event.pk).exists()
      and not TeamMember.objects.filter(event_id=event.pk).exists(),
      "slots + team members torn down with event")
check(not Booking.objects.filter(pk=booking2.pk).exists(), "booking rows removed with event")
check(NotificationLog.objects.filter(type="cancellation",
                                     recipient_email="vera2@example.com").exists(),
      "cancellation email sent + logged (survives as NULL-booking log)")
check(AuditLogEntry.objects.filter(entity_type="Event", entity_id=event.pk,
                                   action="delete").exists(),
      "event deletion audited")

# Tear down the online-branch event too, so the script is re-runnable.
client.post(reverse("events:delete", args=[online.pk]))
check(not Event.objects.filter(pk=online.pk).exists(), "online event torn down")

# Remove the throwaway account so the script leaves nothing behind.
smoke_admin.delete()
check(not User.objects.filter(username=SMOKE_USER).exists(), "smoke-admin removed")

print(f"\nALL {step} SMOKE CHECKS PASSED")
