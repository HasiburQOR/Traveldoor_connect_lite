"""
Effective configuration: database first, environment second.

Every caller reads config through here rather than touching
``django.conf.settings`` directly, so an admin editing the Settings screen
changes behaviour immediately, while a deployment that only sets environment
variables keeps working exactly as before.
"""
from django.conf import settings as dj_settings
from django.core.mail import get_connection

from .models import SiteSettings


def site_settings():
    return SiteSettings.load()


def company_name(site=None):
    site = site or site_settings()
    return site.company_name or dj_settings.COMPANY_NAME


def public_base_url(site=None):
    site = site or site_settings()
    return (site.public_base_url or dj_settings.PUBLIC_BASE_URL).rstrip("/")


def from_email(site=None):
    """The address (and display name) every outbound email is sent from."""
    site = site or site_settings()
    if not site.from_email:
        return dj_settings.DEFAULT_FROM_EMAIL
    if site.from_name:
        return f"{site.from_name} <{site.from_email}>"
    return site.from_email


def default_reminder_hours(site=None):
    site = site or site_settings()
    return site.default_reminder_hours or dj_settings.DEFAULT_REMINDER_HOURS


#: Django's test runner swaps EMAIL_BACKEND to this. When it is active the
#: harness owns outgoing mail, so we must never open a real socket behind its
#: back — otherwise a saved SMTP host would make the suite talk to the internet.
LOCMEM_BACKEND = "django.core.mail.backends.locmem.EmailBackend"


def email_connection(site=None):
    """An SMTP connection built from the Settings screen, or None.

    None means "use whatever EMAIL_BACKEND is configured" — the console backend
    in development, the in-memory outbox under test.
    """
    if getattr(dj_settings, "EMAIL_BACKEND", "") == LOCMEM_BACKEND:
        return None
    site = site or site_settings()
    if not site.smtp_configured:
        return None
    return get_connection(
        backend="django.core.mail.backends.smtp.EmailBackend",
        host=site.email_host,
        port=site.email_port,
        username=site.email_host_user,
        password=site.email_host_password,
        use_tls=site.email_use_tls,
        use_ssl=site.email_use_ssl,
        timeout=int(getattr(dj_settings, "EMAIL_TIMEOUT", 20) or 20),
    )


def email_is_sending(site=None):
    """Is outgoing mail *configured* to reach real inboxes?

    This describes the configuration, not the transport — the test backend
    short-circuits delivery in email_connection() without changing the answer
    here, so the health banner still reflects what an operator set up.
    """
    site = site or site_settings()
    if site.smtp_configured:
        return True
    return "smtp" in getattr(dj_settings, "EMAIL_BACKEND", "").lower()
