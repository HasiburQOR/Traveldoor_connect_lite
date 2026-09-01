"""
Runtime-editable settings (SRS FR-8 admin configuration).

Everything here was previously environment-only, which meant changing the
sending mailbox required a redeploy. These values live in the database so an
admin can change them from the Settings screen, and they *override* the
matching environment variable when filled in. Leaving a field blank falls back
to the env var, so existing deployments keep working untouched.

The SMTP password is encrypted at rest: it is a live credential for the
company's mailbox, and a database dump or a stray backup should not hand it
over. The key is derived from SECRET_KEY, so rotating SECRET_KEY invalidates
the stored password — that is handled gracefully (it reads back as unset and
the admin is asked to re-enter it) rather than crashing.
"""
import base64
import hashlib

from django.conf import settings
from django.db import models


def _fernet():
    from cryptography.fernet import Fernet

    key = base64.urlsafe_b64encode(hashlib.sha256(settings.SECRET_KEY.encode()).digest())
    return Fernet(key)


class SiteSettings(models.Model):
    """Single row of admin-editable configuration."""

    singleton_id = models.PositiveSmallIntegerField(primary_key=True, default=1)

    # ----- branding ---------------------------------------------------------
    company_name = models.CharField(
        max_length=100, blank=True, default="",
        help_text="Shown in emails and in the footer. Blank uses COMPANY_NAME.",
    )
    public_base_url = models.URLField(
        blank=True, default="",
        help_text="The address visitors use, e.g. https://book.traveldoor.ge. "
                  "Every link in every email is built from this.",
    )

    # ----- outgoing mail ----------------------------------------------------
    email_host = models.CharField(
        max_length=200, blank=True, default="",
        help_text="SMTP server, e.g. smtp.gmail.com. Blank keeps using the "
                  "environment configuration.",
    )
    email_port = models.PositiveIntegerField(
        default=587, help_text="587 for STARTTLS, 465 for SSL.",
    )
    email_host_user = models.CharField(
        max_length=200, blank=True, default="", help_text="Usually the full mailbox address.",
    )
    email_host_password_encrypted = models.TextField(blank=True, default="")
    email_use_tls = models.BooleanField(default=True, help_text="STARTTLS — use with port 587.")
    email_use_ssl = models.BooleanField(default=False, help_text="Implicit SSL — use with port 465.")

    from_email = models.EmailField(
        blank=True, default="",
        help_text="The address every email is sent from, e.g. bookings@traveldoor.ge.",
    )
    from_name = models.CharField(
        max_length=100, blank=True, default="",
        help_text="Display name shown beside the from address.",
    )

    # ----- booking defaults -------------------------------------------------
    default_reminder_hours = models.CharField(
        max_length=50, blank=True, default="",
        help_text="Hours before a meeting to email a reminder, comma separated (e.g. 24,1). "
                  "Used by events that do not set their own.",
    )

    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="+",
    )

    class Meta:
        verbose_name = "Site settings"
        verbose_name_plural = "Site settings"

    def __str__(self):
        return "Site settings"

    # ----- password at rest -------------------------------------------------
    @property
    def email_host_password(self):
        """Decrypted SMTP password, or "" when unset/undecryptable."""
        if not self.email_host_password_encrypted:
            return ""
        try:
            return _fernet().decrypt(self.email_host_password_encrypted.encode()).decode()
        except Exception:
            # Most likely SECRET_KEY changed since it was saved.
            return ""

    @email_host_password.setter
    def email_host_password(self, raw):
        self.email_host_password_encrypted = (
            _fernet().encrypt(raw.encode()).decode() if raw else ""
        )

    @property
    def has_email_password(self):
        return bool(self.email_host_password_encrypted)

    @property
    def smtp_configured(self):
        """True when this row alone is enough to send mail."""
        return bool(self.email_host)

    # ----- access -----------------------------------------------------------
    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj
