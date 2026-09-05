"""Admin account management forms (FR-8) plus the forgot-password form."""
import logging

from django import forms
from django.conf import settings as dj_settings
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import (
    AuthenticationForm, PasswordResetForm, UserCreationForm,
)
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone

from core.runtime import (
    company_name, email_connection, from_email as from_address,
    public_base_url, site_settings,
)
from notifications.models import NotificationLog

logger = logging.getLogger(__name__)

User = get_user_model()


class LoginForm(AuthenticationForm):
    """Sign-in form that accepts the account's username OR its email address.

    The back office is small and people remember their email — typing it into
    a "username" box must not lock them out. If the entry looks like an email
    and matches an account, it is swapped for that account's username before
    authentication; otherwise the normal "incorrect credentials" error shows
    (no hint about which emails exist).
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["username"].label = "Username or email"

    def clean(self):
        entered = self.cleaned_data.get("username", "")
        if "@" in entered:
            user = (
                User.objects.filter(email__iexact=entered).order_by("pk").first()
            )
            if user is not None:
                self.cleaned_data["username"] = user.username
        return super().clean()


class AdminUserCreationForm(UserCreationForm):
    """Create additional admin accounts (FR-8.3) with strength rules (NFR-7)."""

    email = forms.EmailField(required=True)
    is_superuser = forms.BooleanField(
        required=False, initial=False, label="Full access (can manage admin accounts)"
    )
    send_credentials_email = forms.BooleanField(
        required=False, initial=True, label="Email login details to the new admin"
    )

    class Meta:
        model = User
        fields = ["username", "email", "first_name", "last_name", "is_superuser",
                  "password1", "password2", "send_credentials_email"]

    def save(self, commit=True):
        user = super().save(commit=False)
        user.email = self.cleaned_data["email"]
        user.is_staff = True  # every created account is a back-office admin
        user.is_superuser = self.cleaned_data.get("is_superuser", False)
        if commit:
            user.save()
        return user


class AdminUserEditForm(forms.ModelForm):
    """Edit an admin account (passwords stay hashed — use the forgot-password
    link on the sign-in page to set a new one)."""

    class Meta:
        model = User
        fields = ["email", "first_name", "last_name", "is_active", "is_superuser"]
        widgets = {"email": forms.EmailInput}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["is_superuser"].label = "Full access (can manage admin accounts)"
        self.fields["is_superuser"].help_text = ""


class BrandedPasswordResetForm(PasswordResetForm):
    """Forgot-password form whose email rides the app's branded pipeline.

    Same rules as every other outbound message (FR-5): rendered from the
    templates/emails set, sent from the Settings-screen sender through the
    Settings-screen SMTP connection when one is configured (console backend in
    development, in-memory outbox under test), and recorded in NotificationLog
    with its delivery status. Links are built from PUBLIC_BASE_URL so they keep
    working behind the reverse proxy.
    """

    def send_mail(self, subject_template_name, email_template_name, context,
                  from_email, to_email, html_email_template_name=None):
        site = site_settings()
        reset_path = reverse(
            "accounts:password_reset_confirm",
            kwargs={"uidb64": context["uid"], "token": context["token"]},
        )
        context = {
            **context,
            "company_name": company_name(site),
            "current_year": timezone.now().year,
            "base_url": public_base_url(site),
            "login_path": reverse("accounts:login"),
            "reset_path": reset_path,
            "reset_timeout_hours": max(1, dj_settings.PASSWORD_RESET_TIMEOUT // 3600),
        }
        subject = "".join(render_to_string(subject_template_name, context).splitlines())
        text_body = render_to_string(email_template_name, context)
        html_body = render_to_string(
            html_email_template_name or "emails/password_reset.html", context
        )
        message = EmailMultiAlternatives(
            subject=subject, body=text_body, to=[to_email],
            from_email=from_address(site), connection=email_connection(site),
        )
        message.attach_alternative(html_body, "text/html")
        error, ok = "", False
        try:
            message.send(fail_silently=False)
            ok = True
        except Exception as exc:  # SMTP misconfiguration / outage — logged like any other email
            error = str(exc)
            logger.error("Password reset email to %s failed: %s", to_email, exc)
        return NotificationLog.objects.create(
            type=NotificationLog.TYPE_PASSWORD_RESET,
            channel=NotificationLog.CHANNEL_EMAIL,
            recipient_email=to_email,
            subject=subject,
            status=NotificationLog.STATUS_SENT if ok else NotificationLog.STATUS_FAILED,
            error=error,
            sent_at=timezone.now() if ok else None,
        )
