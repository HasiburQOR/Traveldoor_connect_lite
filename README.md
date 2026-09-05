# TravelDoor Connect

Single-company, desktop-first B2B event & meeting scheduling platform built to
**SRS v1.1 / BRD v1.0**. Django 6.1, Python 3.12, SQLite (dev) / PostgreSQL
(prod), server-rendered templates with htmx (vendored — no CDN, no build step).

## Features

- **Events (FR-1)** — draft → live → closed lifecycle, public booking links
  (`/b/<slug>/`), deletion with cascade cancellation notices.
- **Team & slots (FR-2, FR-4)** — hosts per event, bulk slot generation,
  online/offline meeting types, host removal flags affected bookings for admin
  attention instead of auto-refund.
- **Public booking (FR-3)** — no visitor login; tokenised manage links
  (reschedule/cancel); cancelled slots reopen immediately; double-booking
  rejected at capacity.
- **Emails (FR-5, BRD 8)** — company SMTP account, txt+html templates, every
  send logged in `NotificationLog`; retries capped (NFR-3).
- **In-app notifications (FR-6)** — notifications list + FLAGS for
  needs-attention bookings.
- **Admin panel (FR-7)** — bookings list/detail/audit trail, book on behalf of
  a visitor, user management with generated one-time passwords.
- **Background scheduler** — `runscheduler` management command: reminders,
  in-app notification lead times, email retries.
- **Audit log (FR-9)** — every admin action recorded (generic entity refs so
  history survives deletions).

## Quick start

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows
pip install -r requirements.txt
copy .env.example .env            # optional — defaults are dev-safe
python manage.py migrate
python manage.py createsuperuser  # staff login for /panel/
python manage.py runserver
```

Admin panel: `http://127.0.0.1:8000/panel/` · Health check: `/health/`

Background worker (separate terminal, optional in dev):

```bash
python manage.py runscheduler           # loop
python manage.py runscheduler --once    # single tick
```

Email delivery: in dev the default backend prints emails to the console —
see **[docs/EMAILS.md](docs/EMAILS.md)** for configuring real SMTP (Settings
screen or `.env`), verifying with the dashboard test-email button, and
debugging.

## Run with Docker (local test)

```bash
docker compose up --build     # web + scheduler + PostgreSQL -> http://127.0.0.1:8000
```

- Creates the `pgdata` volume, runs migrations and collects static files on boot.
- First run: `docker compose exec web python manage.py createsuperuser`
- Logs: `docker compose logs -f web` · Stop & wipe: `docker compose down -v`
- Values like `DJANGO_DEBUG` / `DJANGO_SECRET_KEY` are picked up from your
  `.env` if present (compose reads it for the `${VAR:-default}` placeholders).

Quick throwaway smoke run without a database (SQLite inside the container):

```bash
docker build -t traveldoor .
docker run --rm -p 8000:8000 traveldoor
```

The image is role-switchable via its entrypoint: `web` (default), `scheduler`,
or any command, e.g. `docker run --rm traveldoor python manage.py shell`.

## Deploy on Dokploy

Dokploy runs the same image via `docker-compose.dokploy.yml` (web + scheduler;
PostgreSQL is provided by Dokploy itself, not the compose file).

1. **Database** — in your Dokploy project create a **PostgreSQL** database and
   attach it to this app (injects `DATABASE_URL`), or set `DATABASE_URL`
   manually in the app's environment.
2. **Application** — create the app from your Git repository, choose the
   **Docker Compose** template and point it at `docker-compose.dokploy.yml`.
3. **Environment variables** (Dokploy UI):

   | Variable | Value |
   |---|---|
   | `DJANGO_SECRET_KEY` | random string, e.g. `openssl rand -base64 32` |
   | `DJANGO_DEBUG` | `false` |
   | `DJANGO_ALLOWED_HOSTS` | your domain, e.g. `bookings.example.com` |
   | `PUBLIC_BASE_URL` | `https://bookings.example.com` (used in email links) |
   | `EMAIL_BACKEND` | `smtp` (+ `EMAIL_HOST`, `EMAIL_PORT`, `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD`) |

4. **Domain** — attach your domain to the `web` service on **port 8000**.
5. **First deploy** — migrations run automatically on boot; then create an
   admin login from the Dokploy console (Exec / SSH):
   `docker exec -it <web-container> python manage.py createsuperuser`

Notes: static files are served by WhiteNoise from inside the container (no
nginx sidecar); the reminder/retry loop runs as its own `scheduler` container;
TLS, `X-Forwarded-Proto` and secure cookies are handled automatically when
`DJANGO_DEBUG=false`.

## Configuration

All settings come from environment variables or a root `.env` file (real env
vars win). See [`.env.example`](.env.example) for the full list — notably:

| Variable | Default | Purpose |
|---|---|---|
| `DJANGO_DEBUG` | `true` | turn **off** in production |
| `DJANGO_SECRET_KEY` | dev-only key | **set in production** |
| `DJANGO_ALLOWED_HOSTS` | `localhost,127.0.0.1,testserver` | comma separated |
| `PUBLIC_BASE_URL` | `http://127.0.0.1:8000` | absolute origin used in email links |
| `EMAIL_BACKEND` | console | set to `smtp` + `EMAIL_*` to send real mail |
| `TIME_ZONE` | `UTC` | e.g. `Europe/Amsterdam` |

## Unit tests

```bash
python manage.py test
```

Two suites, 86 tests:

`core/tests.py` walks **every** admin and visitor screen for both event types.
It renders with `string_if_invalid` set to a marker, so a missing or misspelled
context variable fails the build instead of rendering an invisible blank. It
also asserts the no-JS fallback, that a rejected inline form comes back inside
its own htmx swap target, and the "at most one gold action per screen" rule.

`events/tests.py` locks down the event-type rules — that an offline event never
exposes a meeting-link field or link anywhere downstream, that an online event
never exposes Venue / Hall / Table, and that the type becomes immutable once the
event has slots.

## Event type

An event is **strictly one type**, chosen once on the "What kind of event is
this?" screen and immutable as soon as it has a slot. There are no per-slot
overrides — `Slot.mode` is a denormalised mirror of `Event.event_type`, kept in
sync by `Slot.save()`.

| | Online event | Offline event |
|---|---|---|
| Event fields | provider + optional shared link (blank = per-slot auto rooms) | venue, hall, table |
| Slot fields | provider + link (blank = shared link, else a private auto room) | venue/hall/table (blank = inherit) |
| Visitor page | teal "Video call" pill | grey "In person" pill + venue/hall/table |
| Emails | join link, "this is a video call" | full address + "bring this email to the venue" |
| In-app alert | button that opens the meeting link | venue/hall/table, no link |

The fields for the other type are **removed from the form**, not hidden — see
`ONLINE_FIELDS` / `OFFLINE_FIELDS` in `events/forms.py`. `EventForm.save()` and
`Slot.save()` also blank the other type's columns, so stale data cannot leak
into a template or an email.

### Manual QA

1. `/panel/new/` → pick **Offline**. The form has Venue / Hall / Table and no
   link field. Add a host and a slot: the slot form has no link field either.
   Publish, open the public link, pick a time — the stub shows In person with
   venue/hall/table. Book it; the confirmation email (printed to the console)
   carries the address and the "bring this email" note, and no URL.
2. Repeat with **Online**. Every screen shows the provider and link; Venue,
   Hall and Table appear nowhere, and the confirmation email carries the join
   link instead of an address.
3. Re-open either event's edit form once it has slots — the type is replaced by
   a locked notice explaining why.

## Email delivery

**The quickest route: Settings → Outgoing email** (top nav, superusers only).
Fill in the SMTP server, port, username, password and the address to send from,
save, then press **Send test email**. Values saved here override the
environment, take effect immediately with no redeploy, and the SMTP password is
encrypted at rest. Leave the server blank to keep using environment variables
instead — nothing about an existing deployment changes.

Typical providers:

| Provider | Host | Port | Encryption | Password |
|---|---|---|---|---|
| Gmail / Google Workspace | `smtp.gmail.com` | 587 | STARTTLS | An **app password** — a normal account password is rejected |
| Microsoft 365 | `smtp.office365.com` | 587 | STARTTLS | App password (or SMTP AUTH enabled) |
| Most hosts (cPanel etc.) | `mail.yourdomain.com` | 465 | SSL | The mailbox password |

Set **Company → Public base URL** at the same time (e.g.
`https://book.traveldoor.ge`). Every link in every email is built from it, so
while it points at localhost visitors get dead links.

Reminder emails additionally need the scheduler running — `python manage.py
runscheduler`, its own service in `docker-compose.yml`.

### Environment alternative



Confirmation, reschedule, reminder and cancellation emails are all implemented
and already carry the meeting join link (online) or the full address (offline),
plus the visitor's personal manage link and **Add to calendar** links for Google
and Outlook. Two defaults stop them reaching anyone, and both are the usual
cause of "the emails don't work":

| Setting | Default | Effect if left alone |
|---|---|---|
| `EMAIL_BACKEND` | console | Mail is printed to the server log and **never sent**. Set `EMAIL_BACKEND=smtp` plus the `EMAIL_*` values. |
| `PUBLIC_BASE_URL` | `http://127.0.0.1:8000` | Every manage/join link in every email points at localhost and is dead for visitors. Set it to the address visitors use. |

Reminders additionally need the scheduler running — `python manage.py
runscheduler`, which is its own service in `docker-compose.yml`. Without it,
reminder emails and pre-meeting alerts silently never fire.

The dashboard surfaces all three: it warns when mail is not being sent, when
email links point at localhost, and when the scheduler has never run. **Send
test email** mails the logged-in admin and shows the raw SMTP error on failure,
so a misconfiguration is diagnosed in one click. Each booking page lists its
reminders as *sent / scheduled / not sent*, and offers **Re-send confirmation**.

## Breaks

Protected time a host is not available for: lunch, travel, a reset between
meetings. A break is a `Slot` with `status="break"` — it occupies the host's
day, is never bookable, and never reaches a visitor.

* **Bulk generator** — set *Break starts* / *Break ends* and any slot that would
  overlap the window is dropped (a meeting running into lunch is no more usable
  than one held during it); generation resumes at the end of the break. Tick
  *Show the break in the schedule* to also create one non-bookable Break entry
  per host per day, so the gap is explained rather than mysterious.
* **Gap between slots** — minutes of breathing room after each meeting; 0 keeps
  them back-to-back.
* **Single slot** — tick *This is a break, not a bookable meeting*. A break
  needs neither a meeting link nor a venue, so those requirements are waived.
  Unticking it hands the time back to visitors.

The live counter under the form walks the day exactly the way the server does,
so the number shown is the number created.

## Admin lists

* **Bookings** — searchable by name/email/company, filterable by status and
  event, paginated 25 per page, and exportable to CSV. The export reuses the
  exact filtered queryset, so what you see is what you get; because it contains
  visitor personal data, every export is written to the audit log.
* **Events** — each row carries hosts / slots / bookings counts plus the venue
  or provider, so events are distinguishable without opening them, and can be
  opened, edited, duplicated or deleted from the list. **Duplicate** copies the
  settings, active hosts and slot pattern (breaks included) as a fresh Draft —
  never the bookings.
* **Slots** are grouped by day rather than paginated: the table lives inside an
  htmx-swapped section, so page state would be lost on every edit or delete.

## Design system

All colour, type and spacing tokens live in `static/css/site.css` under
`:root`. No template contains a hardcoded hex value; the only exception is
`templates/emails/`, because email clients support neither CSS custom
properties nor external stylesheets (the values there are copies of the same
tokens, noted in `base_email.html`).

* **Fonts** — Fraunces for display moments only (page titles, host and event
  names, headline numbers); Inter for all body and UI text.
* **Buttons** — at most one gold `.btn-primary` per screen. Everything else is
  `.btn-secondary` (navy outline), `.btn-ghost` or `.btn-danger`.
* **Status pills** — one mapping everywhere: teal for online/confirmed/live,
  gold for draft/pending/attention, grey for offline/closed, red for
  cancelled/failed.
* **Forms** — every form on both sides renders through
  `templates/partials/form_fields.html`, so labels and inputs are identical.
* **Admin vs visitor** — admin screens use the persistent top nav and bordered
  tables; the visitor side uses the "ticket" motif (header block, perforated
  divider, two-column body) and carries no chrome beyond the back link and the
  footer credit. The ticket is never used for admin data tables.

## Smoke test

End-to-end script covering the whole booking lifecycle (66 checks): event
create → team → bulk slots → publish → public booking → double-book rejection →
manage-link reschedule/cancel → attention flag → admin pages → admin user
welcome email → online-event branch → scheduler tick → event delete cascade.

```bash
# 1. wipe the dev database (order matters — PROTECT FKs)
python manage.py shell -c "from django.contrib.auth import get_user_model; from events.models import Event, Slot, TeamMember; from bookings.models import Booking; from notifications.models import NotificationLog, HostNotification; from audit.models import AuditLogEntry; get_user_model().objects.filter(username='ops1').delete(); NotificationLog.objects.all().delete(); HostNotification.objects.all().delete(); Booking.objects.all().delete(); Slot.objects.all().delete(); TeamMember.objects.all().delete(); Event.objects.all().delete(); AuditLogEntry.objects.all().delete()"

# 2. run (provisions its own throwaway superuser; no fixed password needed)
python manage.py shell -c "exec(open('scripts/_run_smoke.py', encoding='utf-8').read())"
```

The runner writes tracebacks to `smoke_traceback.txt` (PowerShell swallows
them otherwise). Expected output ends with `ALL 66 SMOKE CHECKS PASSED`.

> With the console email backend (default) the run also prints every email
> that would have been sent — useful as a template preview.

## Project layout

```
traveldoor/          project package (settings, urls)
accounts/            admin users, login, user management (FR-8)
events/              events, team members, slots, publish lifecycle
bookings/            bookings, public booking flow, tokenised manage links
notifications/       email layer, in-app notifications, runscheduler
audit/               generic audit trail
core/                dashboard, health check
templates/           all templates (emails/ for txt+html message bodies)
static/              site.css + vendored htmx.min.js
scripts/             smoke_test.py + _run_smoke.py runner
```

## Design notes

- **Audit entries are generic** (`entity_type` + `entity_id`, no FK) so the
  trail survives row deletion; `NotificationLog.booking` is `SET_NULL` for the
  same reason.
- **Event deletion (FR-1.2)** cancels + emails affected visitors *before*
  tearing down rows (the log FK would fail afterwards), then deletes
  bookings → slots → team members explicitly (`Slot.host` is `PROTECT`, so a
  plain `event.delete()` cascade raises `ProtectedError`).
- **Reschedule creates a new Booking** with a fresh manage token and cancels
  the old one — both parties are notified of the change/release.
# Traveldoor_connect_lite
