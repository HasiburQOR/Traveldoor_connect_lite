"""
Django settings for the TravelDoor Connect project.

TravelDoor Connect — single-company, desktop-first B2B event & meeting
scheduling platform (see SRS v1.1 section 1.2 for the decided stack).

Environment configuration is read from a `.env` file at the project root
when present (no external dependency required), and real environment
variables always win over `.env` values.
"""
from pathlib import Path
import os

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_env_file(path):
    """Tiny .env loader so no third-party package is required (SRS 1.2)."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


_load_env_file(BASE_DIR / ".env")


def env_bool(name, default=False):
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


# -----------------------------------------------------------------------------
# Core
# -----------------------------------------------------------------------------
SECRET_KEY = os.environ.get(
    "DJANGO_SECRET_KEY",
    "django-insecure-dev-only-key-change-me-in-production-0123456789",
)

DEBUG = env_bool("DJANGO_DEBUG", True)

ALLOWED_HOSTS = [
    h.strip()
    for h in os.environ.get("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1,testserver").split(",")
    if h.strip()
]
# The container HEALTHCHECK (and compose `depends_on: service_healthy`) probes
# http://127.0.0.1:<port>/health/ directly, so loopback must always be allowed
# even when DJANGO_ALLOWED_HOSTS is overridden for deployment (Dokploy etc.).
for _loopback in ("127.0.0.1", "localhost"):
    if _loopback not in ALLOWED_HOSTS:
        ALLOWED_HOSTS.append(_loopback)

# Absolute origin used to build links inside emails (FR-5.1 manage links etc.).
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "http://127.0.0.1:8000").rstrip("/")

# Origins allowed to issue authenticated POSTs - required behind reverse proxies
# (Dokploy/Traefik). PUBLIC_BASE_URL is included automatically; add extra
# domains via DJANGO_CSRF_TRUSTED_ORIGINS="https://a.example,https://b.example".
CSRF_TRUSTED_ORIGINS = list(dict.fromkeys(
    [PUBLIC_BASE_URL]
    + [o.strip() for o in os.environ.get("DJANGO_CSRF_TRUSTED_ORIGINS", "").split(",") if o.strip()]
))

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # TravelDoor Connect apps
    "core",
    "accounts",
    "events",
    "bookings",
    "notifications",
    "audit",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",  # serves static files under gunicorn/Docker
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "traveldoor.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "traveldoor.wsgi.application"

# -----------------------------------------------------------------------------
# Database — SQLite for early dev, PostgreSQL in production (SRS 1.2).
# Set POSTGRES_DB (plus POSTGRES_USER / POSTGRES_PASSWORD / POSTGRES_HOST /
# POSTGRES_PORT) to switch to PostgreSQL.
# -----------------------------------------------------------------------------
def _db_from_url(url):
    """Parse DATABASE_URL (postgres://user:pass@host:port/name) without extra deps."""
    from urllib.parse import unquote, urlsplit

    parts = urlsplit(url)
    return {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": unquote(parts.path.lstrip("/")) or "traveldoor",
        "USER": unquote(parts.username or ""),
        "PASSWORD": unquote(parts.password or ""),
        "HOST": parts.hostname or "",
        "PORT": str(parts.port or ""),
        "CONN_MAX_AGE": 60,
    }


if os.environ.get("DATABASE_URL"):
    # Dokploy / PaaS style single URL (injected when a managed DB is attached).
    DATABASES = {"default": _db_from_url(os.environ["DATABASE_URL"])}
elif os.environ.get("POSTGRES_DB"):
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": os.environ["POSTGRES_DB"],
            "USER": os.environ.get("POSTGRES_USER", "postgres"),
            "PASSWORD": os.environ.get("POSTGRES_PASSWORD", ""),
            "HOST": os.environ.get("POSTGRES_HOST", "localhost"),
            "PORT": os.environ.get("POSTGRES_PORT", "5432"),
            "CONN_MAX_AGE": 60,
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
        }
    }

# -----------------------------------------------------------------------------
# Authentication (SRS FR-8) — Django's built-in auth system
# -----------------------------------------------------------------------------
AUTH_PASSWORD_VALIDATORS = [  # NFR-7: basic strength rules
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
     "OPTIONS": {"min_length": 8}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LOGIN_URL = "accounts:login"
LOGIN_REDIRECT_URL = "core:dashboard"
LOGOUT_REDIRECT_URL = "accounts:login"

# -----------------------------------------------------------------------------
# Email — company's own SMTP account (SRS 1.2 / BRD 8).
# In development the console backend prints emails instead of sending them;
# set EMAIL_BACKEND=smtp plus EMAIL_* values (or put them in .env) to go live.
# -----------------------------------------------------------------------------
if os.environ.get("EMAIL_BACKEND", "").lower() == "smtp":
    EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
    EMAIL_HOST = os.environ.get("EMAIL_HOST", "smtp.example.com")
    EMAIL_PORT = int(os.environ.get("EMAIL_PORT", "587"))
    EMAIL_HOST_USER = os.environ.get("EMAIL_HOST_USER", "")
    EMAIL_HOST_PASSWORD = os.environ.get("EMAIL_HOST_PASSWORD", "")
    EMAIL_USE_TLS = env_bool("EMAIL_USE_TLS", True)
    EMAIL_TIMEOUT = int(os.environ.get("EMAIL_TIMEOUT", "20"))
else:
    EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"

COMPANY_NAME = os.environ.get("COMPANY_NAME", "TravelDoor")
DEFAULT_FROM_EMAIL = os.environ.get(
    "DEFAULT_FROM_EMAIL",
    "TravelDoor Connect <bookings@traveldoor.example>",
)

# Number of delivery attempts before a notification email is abandoned (NFR-3).
EMAIL_MAX_RETRIES = 3

# -----------------------------------------------------------------------------
# Internationalisation
# -----------------------------------------------------------------------------
LANGUAGE_CODE = "en-us"
TIME_ZONE = os.environ.get("TIME_ZONE", "UTC")
USE_I18N = True
USE_TZ = True

# -----------------------------------------------------------------------------
# Static files
# -----------------------------------------------------------------------------
STATIC_URL = "static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"  # collectstatic target (Docker / WhiteNoise)

# Production static serving via WhiteNoise (plain `runserver` is unaffected).
if not DEBUG:
    STORAGES = {
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "whitenoise.storage.CompressedStaticFilesStorage"},
    }

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# -----------------------------------------------------------------------------
# TravelDoor Connect platform defaults
# (answers to SRS section 7 open questions — sensible defaults, configurable)
# -----------------------------------------------------------------------------
# Reminder emails are sent N hours before a meeting (FR-5.2). Comma separated.
DEFAULT_REMINDER_HOURS = "24,1"
# In-app notification lead time in minutes before a meeting (FR-6.3).
DEFAULT_INAPP_LEAD_MINUTES = 30
# Background scheduler tick, in seconds (management command `runscheduler`).
SCHEDULER_INTERVAL_SECONDS = int(os.environ.get("SCHEDULER_INTERVAL_SECONDS", "30"))

# -----------------------------------------------------------------------------
# Production hardening - active only when DJANGO_DEBUG=false (e.g. Dokploy's
# Traefik proxy terminates TLS and forwards X-Forwarded-Proto).
# -----------------------------------------------------------------------------
if not DEBUG:
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_SSL_REDIRECT = env_bool("DJANGO_SSL_REDIRECT", True)

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {
        "console": {"class": "logging.StreamHandler", "level": "INFO"},
    },
    "root": {"handlers": ["console"], "level": "INFO"},
}

