# Deploying to Dokploy

The repo is Dokploy-ready: `docker-compose.dokploy.yml` deploys two services
from the same image — `web` (migrate → collectstatic → gunicorn :8000) and
`scheduler` (the reminder/retry loop). The database is a Dokploy-managed
PostgreSQL attached to the app (that injects `DATABASE_URL`). TLS and the
reverse proxy are handled by Dokploy's Traefik; static files by WhiteNoise —
no nginx needed.

Example below uses `booking.binomargroup.com`.

## 1. One-time preparation

1. **DNS**: point `booking.binomargroup.com` at your Dokploy server — an `A`
   record to the server IP (or a `CNAME` to it if you already have a domain
   record there). If a wildcard `*.binomargroup.com` already points at the
   server, skip this.
2. **Push the code** Dokploy will build from Git:

   ```bash
   git add -A && git commit -m "Prepare for Dokploy deploy" && git push origin main
   ```

3. **Generate a secret key** (any machine): run
   `python -c "import secrets; print(secrets.token_urlsafe(48))"` and copy
   the output. Keep it — it encrypts the SMTP password saved in Settings.

## 2. In Dokploy

1. **Project** → *Create Project* → e.g. `binomar`.
2. **Database**: *Databases → PostgreSQL → Create* (project scope). Defaults
   are fine for testing. Wait for it to be healthy.
3. **App**: *Create → Docker Compose*. Source type **Git**, pick the
   `Traveldoor_connect_lite` repository, and set the compose file path to
   `docker-compose.dokploy.yml` (or paste its contents into the compose
   editor — then no repo path is needed).
4. **Attach the database** to this compose resource (Databases → … → attach).
   Dokploy injects `DATABASE_URL` automatically; if you prefer, copy the
   Postgres internal connection string into the env below instead.
5. **Environment variables** (Dokploy → Environment):

   ```ini
   DJANGO_SECRET_KEY=<the random string from step 1.3>
   DJANGO_DEBUG=false
   DJANGO_ALLOWED_HOSTS=booking.binomargroup.com
   PUBLIC_BASE_URL=https://booking.binomargroup.com
   ```

   Email SMTP is intentionally left out: configure it after first login on
   the Settings screen so the password is encrypted at rest and survives
   redeploys (database config always wins over environment).

   How Dokploy applies these: the compose **Environment** editor writes them
   to a `.env` file next to the compose file, and `docker-compose.dokploy.yml`
   pulls each one in via `${VAR:-default}` — the documented Dokploy pattern.
   If you also `Attach` the Postgres to the service, `DATABASE_URL` is
   injected for you; otherwise paste the database's **Internal Connection
   URL** (Connection tab) as `DATABASE_URL`.
6. **Domain**: *Domains → Add domain* → `booking.binomargroup.com`, service
   `web`, port `8000`. Issue the Let's Encrypt certificate and keep HTTPS
   redirect on. **Redeploy required**: for Docker Compose, domains are applied
   as Traefik labels — after adding/changing a domain you must click
   **Redeploy** for routing to take effect (latest Dokploy docs).
7. **Deploy**. First build takes a few minutes. The `web` container runs
   migrations and static collection automatically on every boot; the
   `scheduler` container starts once `web` is healthy.

## 3. First login

1. Create the admin account: Dokploy → the compose resource → `web` service →
   **Terminal**, then run `python manage.py createsuperuser`
   (username + a real email such as your Gmail — the test-email button sends
   there). Set a strong password.
2. Log in at `https://booking.binomargroup.com/accounts/login/`.
3. `/settings/`: set Company name, Public base URL
   (`https://booking.binomargroup.com`), From name/address, and the SMTP
   block (`smtp.gmail.com`, port 587, STARTTLS, username = your Gmail, an
   app password). Save → dashboard → **Send test email**.

## 4. Verify

* `https://booking.binomargroup.com/health/` returns `ok`.
* Book a test event with a real email → confirmation arrives, manage link
  starts with `https://booking.binomargroup.com/`, reschedule + cancel work.
* Dashboard "background jobs" line shows a recent run → scheduler alive
  (reminders will fire).

## 5. Updating / troubleshooting

* **Update**: `git push origin main` → Dokploy → Redeploy. Data (Postgres
  volume + Settings) persists.
* Bad gateway / "invalid host" → `DJANGO_ALLOWED_HOSTS` missing the domain,
  or the domain's service/port is not `web`/`8000`.
* `web` container "unhealthy" / scheduler "dependency failed to start" → the
  image healthcheck probes `http://127.0.0.1:8000/health/`; the app always
  allows loopback for this, so a failure means gunicorn never started — open
  the service **Logs** (service `web`) and look for migrate/DB errors above
  the gunicorn banner.
* `could not translate host name "<db-host>"` → the compose services and the
  database must share **dokploy-network**; `docker-compose.dokploy.yml` joins
  it for both services, so this only happens if that block was removed. Note
  Dokploy's *attach database* dropdown lists Applications only — with a
  Compose resource, set `DATABASE_URL` manually (the file handles networking).
* Environment values are **raw** — no `<`/`>` placeholder brackets, no
  quotes. A `DATABASE_URL` that starts with `<` parses to an empty host and
  crashes `migrate` on boot.
* CSRF error on booking → `PUBLIC_BASE_URL` must match the browser address
  exactly (`https://`, no trailing slash).
* Redirect loop over http → intended: `DJANGO_DEBUG=false` forces HTTPS; use
  the https URL.
* Reminders not sending → check the `scheduler` container logs.
* Saved SMTP password "lost" after redeploy → `DJANGO_SECRET_KEY` changed;
  re-enter the password.
* Free Gmail accounts relay ~500 recipients/day — fine for testing.
