# Working with emails in TravelDoor Connect

Everything outbound — confirmations to bookers (visitors), reminders,
cancellations, admin-account welcome mails, password-reset mails — flows
through one small pipeline in the `notifications` app. This guide covers how
it works, how to turn real sending on, how to verify it, and how to debug it.

## TL;DR — does email sending work?

* **The pipeline is complete and tested.** All six visitor/host emails plus the
  admin welcome email render and send through Django's mail stack; the email
  test suite passes (15/15, verified in this checkout).
* **A fresh dev checkout does not deliver real mail.** With no SMTP configured
  the app uses the *console backend*: every email is printed to the
  `runserver` terminal instead of being sent. That is the intended dev
  default (`traveldoor/settings.py`).
* **To send real mail**, configure SMTP once — either on the admin
  **Settings** screen (`/settings/`, no redeploy needed) or via `.env`
  (`EMAIL_BACKEND=smtp` + host/user/password) — then press **Send test email**
  on the dashboard to confirm.

## Step-by-step: turn on real email sending

### Option A — the Settings screen (recommended)

1. Log in as a **superuser** (regular staff can't open this screen) whose
   account has an email address — the test-email button sends to *your*
   address. Start the dev server: `python manage.py runserver`.
2. Open **`/settings/`** and fill in:
   * **Public base URL** — the address visitors use, e.g.
     `https://book.yourcompany.com` (every link inside every email is built
     from it; leaving the `127.0.0.1` default makes manage-links useless).
   * **From name / From address** — e.g. `TravelDoor Connect` /
     `bookings@yourcompany.com`. The from address is required once an SMTP
     server is set.
   * **SMTP server** — e.g. `smtp.gmail.com`.
   * **Port + encryption** — 587 with **STARTTLS** (the default) or 465 with
     **SSL**. Choose one; the form rejects both ticked.
   * **SMTP username** — usually the full mailbox address.
   * **SMTP password** — for Gmail / Microsoft 365 this must be an *app
     password*, not the normal login password. The field is write-only:
     after saving it shows "a password is saved (encrypted)" and leaving it
     blank keeps the stored one; tick **Remove the saved password** to clear.
3. **Save** — changes take effect immediately (config is read from the
   database on every send; no restart needed).
4. Go to the dashboard (`/`) and press **Send test email**. Success means the
   mail arrived in your inbox; failure shows the raw SMTP error (usually the
   whole diagnosis).
5. Reminder emails additionally need the background worker running:
   `python manage.py runscheduler` (the Docker `scheduler` service does this
   in production).

### Option B — a `.env` file

1. Copy `.env.example` → `.env` in the project root.
2. Edit the email block:

   ```ini
   EMAIL_BACKEND=smtp
   EMAIL_HOST=smtp.yourprovider.com
   EMAIL_PORT=587
   EMAIL_HOST_USER=bookings@yourcompany.com
   EMAIL_HOST_PASSWORD=********
   EMAIL_USE_TLS=true
   DEFAULT_FROM_EMAIL=TravelDoor Connect <bookings@yourcompany.com>
   PUBLIC_BASE_URL=https://book.yourcompany.com
   ```

3. Restart `runserver` — `.env` is read once at startup.
   Caveats: the env route supports **STARTTLS only** (there is no
   `EMAIL_USE_SSL` setting) — implicit SSL / port 465 must be configured in
   the Settings screen. And if an SMTP server is ever saved in the UI, the
   **UI wins** over `.env`.

### Provider cheat-sheet

| Provider | SMTP server | Port / encryption | Password to use |
|---|---|---|---|
| Gmail / Google Workspace | `smtp.gmail.com` | 587 STARTTLS | App password (needs 2FA on the account), not the login password |
| Microsoft 365 / Outlook | `smtp.office365.com` | 587 STARTTLS | App password; the From address must equal the authenticated mailbox |
| Hosting / cPanel mailbox | `mail.yourdomain.com` | 465 SSL (Settings screen) or 587 STARTTLS | Mailbox password |
| Brevo / Mailgun / SendGrid / Postmark | e.g. `smtp-relay.brevo.com:587` | 587 STARTTLS | The SMTP/API key they issue |

Transactional providers give the best deliverability for booking
confirmations; either way, send from a domain you control so SPF/DKIM pass.

## The pipeline

```
trigger (view or scheduler)
  └─ notifications/emails.py  send_*()
       └─ _deliver()
            ├─ render templates/emails/<name>.txt + <name>.html
            ├─ connection = core/runtime.py email_connection()
            │     DB SiteSettings SMTP  →  if set, use it (overrides env)
            │     otherwise             →  Django EMAIL_BACKEND (console in dev)
            ├─ EmailMultiAlternatives.send()
            └─ NotificationLog row: sent/failed + error text
                 └─ failed rows retried by `runscheduler`
                    up to settings.EMAIL_MAX_RETRIES (3)
```

| File | Role |
|---|---|
| `notifications/emails.py` | All `send_*` functions, `_deliver()`, `retry_failed_emails()`, `email_health()`, `send_test_email()` |
| `notifications/jobs.py` | Scheduler tick: due reminders, in-app notifications, email retries |
| `notifications/management/commands/runscheduler.py` | Background worker loop (`--once` for a single tick) |
| `notifications/models.py` | `NotificationLog` (every outbound attempt), `HostNotification`, `SchedulerHeartbeat` |
| `notifications/calendar_links.py` | Google/Outlook "add to calendar" URLs used in templates |
| `core/models.py` | `SiteSettings` — admin-editable SMTP/branding (password encrypted at rest) |
| `core/runtime.py` | Resolves effective config: **database first, environment second** |
| `templates/emails/` | One `.txt` + `.html` pair per email, shared `base_email.html` + `_where_rows.*` partials |
| `traveldoor/settings.py` | `EMAIL_*` env fallbacks, `DEFAULT_FROM_EMAIL`, `EMAIL_MAX_RETRIES` |

## Every email the system sends

| Email | Function (in `notifications/emails.py`) | Recipient | Sent when |
|---|---|---|---|
| Confirmation | `send_booking_confirmation` | `booking.visitor_email` | Visitor books on the public page; admin books on their behalf; admin re-sends from booking detail |
| Reschedule notice | `send_booking_confirmation(is_reschedule=True)` | visitor | Visitor reschedules via manage link; admin moves a booking |
| Reminder | `send_reminder` (via `jobs.process_due_reminders`) | visitor | `runscheduler` tick, N hours before the meeting (per event `reminder_hours_csv`, site default `24,1`) |
| Cancellation (visitor) | `send_cancellation_to_visitor` | visitor | Admin cancels; slot deleted; **event deleted** (before rows go) |
| Cancellation (host) | `send_cancellation_to_host` | `slot.host.email` | Visitor cancels or reschedules via manage link; admin cancels/moves a booking |
| Attention | `send_attention_email` | visitor | Their host was removed/deactivated while bookings were pending |
| Admin welcome | `send_admin_account_email` | new admin user | An admin account is created (one-time password included) |
| Test | `send_test_email` | your own address | Dashboard **Send test email** button or shell (below) |

Every send is logged in `NotificationLog` (type, recipient, subject,
status, error, retry count) — the booking detail screen shows the history.

## Configuration — database wins, environment is the fallback

**Layer 1 — Settings screen** (`/settings/`, superusers only, changes apply
immediately, no redeploy): company name, public base URL, from address/name,
SMTP host / port / username / password, STARTTLS vs SSL, default reminder
hours. The SMTP password is encrypted at rest (Fernet key derived from
`SECRET_KEY` — rotating `SECRET_KEY` invalidates it; it just reads back as
unset and the form asks for it again). Setting an SMTP server here is enough
to send mail, regardless of environment variables.

**Layer 2 — environment / `.env`** (used whenever the matching Settings field
is blank). Copy `.env.example` → `.env` at the project root:

```ini
EMAIL_BACKEND=smtp          # the switch — unset means console backend (dev)
EMAIL_HOST=smtp.yourprovider.com
EMAIL_PORT=587              # 587 = STARTTLS, 465 = implicit SSL
EMAIL_HOST_USER=bookings@yourcompany.com
EMAIL_HOST_PASSWORD=********
EMAIL_USE_TLS=true          # with 587; for 465 use SSL instead of STARTTLS
DEFAULT_FROM_EMAIL=TravelDoor Connect <bookings@yourcompany.com>
PUBLIC_BASE_URL=https://book.yourcompany.com   # every link inside emails is built from this
```

Real environment variables always win over `.env` values.

## Verifying that sending works

1. **Dashboard `/`** — shows an email-health card: "Email is configured and
   sending" vs specific warnings (console backend, links pointing at
   localhost, failed sends). It also shows when the scheduler last ran.
2. **Send test email** — button on the dashboard (needs an email address on
   your admin account). On failure it displays the raw SMTP error, which is
   usually the whole diagnosis (bad password, wrong port, …).
3. **From a shell** (works in dev too — the console backend prints the mail):

   ```bash
   python manage.py shell -c "from notifications.emails import send_test_email; print(send_test_email('you@example.com'))"
   ```

4. **Full end-to-end in dev without SMTP**: book a slot on a live event's
   public page (`/b/<slug>/…`) and watch the `runserver` terminal — the
   complete confirmation email (text + HTML) is printed there.
5. **Check the log**:

   ```bash
   python manage.py shell -c "from notifications.models import NotificationLog; print(list(NotificationLog.objects.values('type','status','recipient_email','error')[:20]))"
   ```

## Reminders need the scheduler

Confirmations and cancellations send immediately from the request.
**Reminders and failed-delivery retries only happen while the background
worker runs**:

```bash
python manage.py runscheduler           # loop, every SCHEDULER_INTERVAL_SECONDS (30)
python manage.py runscheduler --once    # single tick (useful in dev)
```

In Docker/Dokploy the `scheduler` service runs this for you. If the dashboard
says the background jobs never ran, reminders are silently not sending —
start the worker. Reminders are deduplicated per booking via the log's `meta`
field (`reminder-<hours>`), so re-running a tick never double-sends.

## Templates

* Pairs live in `templates/emails/`: `confirmation`, `reschedule`, `reminder`,
  `cancellation_visitor`, `cancellation_host`, `attention`, `admin_welcome`
  — each as `.txt` and `.html`, plus `base_email.html` (layout/branding) and
  `_where_rows.*` (the where/how-to-join block shared by all booking mails).
* Common context variables: `booking`, `slot`, `host`, `event`, `when`,
  `duration`, `manage_url`, `base_url`, `company_name`, `current_year`, and
  the online/offline branch keys `is_online`, `join_link`, `provider`,
  `venue`, `hall_name`, `table_name`, `google_calendar_url`,
  `outlook_calendar_url`. Per-email extras: `reason`/`actor_label`
  (cancellations), `hours_before` (reminder), `raw_password`/`login_path`
  (admin welcome).
* Both bodies are always rendered (multipart alternative). Tests
  (`core.tests.EmailTemplateWalkTest`) render every template for online and
  offline events and assert there are no unresolved `{{ }}` markers and no
  venue/link leakage across event types — extend that test when adding
  variables or templates.
* Quick preview hack: with the console backend, the smoke test
  (`scripts/smoke_test.py`) prints every email it triggers.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Emails appear in the `runserver` terminal but visitors get nothing | Console backend (dev default) — nothing is sent | Configure SMTP (Settings screen or `.env` with `EMAIL_BACKEND=smtp`) |
| `Test email failed: (535, … authentication)` | Wrong SMTP user/password; Gmail and similar require an app password, not the login password | Re-enter credentials in `/settings/` |
| Test email hangs then times out | Wrong port/TLS combination (e.g. SSL port with STARTTLS) | 587 → STARTTLS on, SSL off; 465 → SSL on, STARTTLS off |
| Links inside emails point at `http://127.0.0.1:8000` | `PUBLIC_BASE_URL` / Settings public base URL still the dev default | Set the real public address (Settings screen or `PUBLIC_BASE_URL`) |
| Reminders never arrive; dashboard says background jobs never ran | `runscheduler` not running | Start the worker (`--once` to test; Docker `scheduler` service in prod) |
| A saved SMTP password seems gone | `SECRET_KEY` was rotated — the stored password can no longer be decrypted | Re-enter the password in `/settings/` (by design, not a bug) |
| NotificationLog rows stuck at `failed` | Retries exhausted (`EMAIL_MAX_RETRIES = 3`) or scheduler stopped | Fix the SMTP error shown on the booking detail screen; failed rows retry automatically once the worker runs |
| Log says `sent` but the visitor reports nothing | Delivered to spam / sender domain lacks SPF/DKIM | Check spam; align `from_email` with the authenticated mailbox's domain |

## Running the email tests

```bash
python manage.py test core.tests.EmailTemplateWalkTest core.tests.SiteSettingsTest
```

(15 tests, all passing in this checkout.) The full suite plus the end-to-end
smoke test additionally exercise the booking → confirmation → cancellation
lifecycle with the in-memory mail backend.

