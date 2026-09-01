"""
"Add to calendar" links for confirmation and reschedule emails.

Links rather than an attached .ics file: a URL renders and works in every mail
client, where an attachment is frequently stripped, hidden behind a download,
or simply ignored on mobile.

Both providers take the event in UTC. The location follows the event type — a
join link for online events, the venue address for offline ones — so these
links never leak the wrong type's details, exactly like the emails themselves.
"""
from urllib.parse import urlencode

from core.runtime import public_base_url

GOOGLE_BASE = "https://calendar.google.com/calendar/render"
OUTLOOK_BASE = "https://outlook.live.com/calendar/0/deeplink/compose"


def _utc(value):
    """Aware datetime -> UTC, so both providers read the same instant."""
    import datetime

    return value.astimezone(datetime.timezone.utc)


def _parts(booking):
    """Title, description and location for one booking."""
    slot = booking.slot
    event = booking.event
    host = slot.host

    title = f"{event.name} — meeting with {host.name}"

    lines = [f"Your meeting with {host.name} for {event.name}."]
    if event.is_online:
        link = slot.effective_meeting_link
        provider = slot.effective_provider_display or "Video call"
        location = link or provider
        if link:
            lines.append(f"Join ({provider}): {link}")
    else:
        location = ", ".join(
            p for p in (slot.effective_venue, slot.effective_hall, slot.effective_table) if p
        )
        if location:
            lines.append(f"Where: {location}")
        lines.append("Please bring your confirmation email to be checked in.")

    manage_url = f"{public_base_url()}{booking.get_manage_url()}"
    lines.append(f"Change or cancel: {manage_url}")
    return title, "\n".join(lines), location


def google_calendar_url(booking):
    """Prefilled Google Calendar event."""
    title, description, location = _parts(booking)
    start = _utc(booking.slot.start_datetime).strftime("%Y%m%dT%H%M%SZ")
    end = _utc(booking.slot.end_datetime).strftime("%Y%m%dT%H%M%SZ")
    return GOOGLE_BASE + "?" + urlencode({
        "action": "TEMPLATE",
        "text": title,
        "dates": f"{start}/{end}",
        "details": description,
        "location": location,
    })


def outlook_calendar_url(booking):
    """Prefilled Outlook (outlook.com / Microsoft 365) event."""
    title, description, location = _parts(booking)
    return OUTLOOK_BASE + "?" + urlencode({
        "path": "/calendar/action/compose",
        "rru": "addevent",
        "subject": title,
        "startdt": _utc(booking.slot.start_datetime).isoformat().replace("+00:00", "Z"),
        "enddt": _utc(booking.slot.end_datetime).isoformat().replace("+00:00", "Z"),
        "body": description,
        "location": location,
    })
