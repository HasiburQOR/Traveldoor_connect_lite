"""Admin-editable settings form (FR-8 configuration)."""
from django import forms

from .models import SiteSettings


class SiteSettingsForm(forms.ModelForm):
    """Edit the runtime configuration.

    The SMTP password is write-only: it is never rendered back into the page,
    and an empty box means "keep what is stored" rather than "clear it". The
    template shows whether a password is currently saved, and there is an
    explicit checkbox to remove one.
    """

    email_host_password = forms.CharField(
        label="SMTP password",
        required=False,
        widget=forms.PasswordInput(render_value=False, attrs={"autocomplete": "new-password"}),
        help_text="Leave blank to keep the saved password. For Gmail or Microsoft 365 "
                  "this must be an app password, not your normal login password.",
    )
    clear_email_password = forms.BooleanField(
        label="Remove the saved password", required=False,
    )

    class Meta:
        model = SiteSettings
        fields = [
            "company_name", "public_base_url",
            "from_name", "from_email",
            "email_host", "email_port", "email_host_user",
            "email_use_tls", "email_use_ssl",
            "default_reminder_hours",
        ]
        labels = {
            "company_name": "Company name",
            "public_base_url": "Public base URL",
            "from_name": "From name",
            "from_email": "From address",
            "email_host": "SMTP server",
            "email_port": "Port",
            "email_host_user": "SMTP username",
            "email_use_tls": "Use STARTTLS",
            "email_use_ssl": "Use SSL",
            "default_reminder_hours": "Send reminders (hours before)",
        }
        widgets = {
            "public_base_url": forms.URLInput(attrs={"placeholder": "https://book.example.com"}),
            "email_host": forms.TextInput(attrs={"placeholder": "smtp.gmail.com"}),
            "from_email": forms.EmailInput(attrs={"placeholder": "bookings@example.com"}),
            "email_host_user": forms.TextInput(attrs={"placeholder": "bookings@example.com"}),
            "default_reminder_hours": forms.TextInput(attrs={"placeholder": "24,1"}),
        }

    def clean_default_reminder_hours(self):
        raw = (self.cleaned_data.get("default_reminder_hours") or "").strip()
        if not raw:
            return raw
        for part in raw.split(","):
            part = part.strip()
            if not part.isdigit() or int(part) < 1:
                raise forms.ValidationError(
                    "Enter positive whole hours, comma separated — e.g. 24,1"
                )
        return raw

    def clean(self):
        cleaned = super().clean()
        host = cleaned.get("email_host")
        use_tls = cleaned.get("email_use_tls")
        use_ssl = cleaned.get("email_use_ssl")

        if use_tls and use_ssl:
            self.add_error(
                "email_use_ssl",
                "Choose one: STARTTLS (port 587) or SSL (port 465), not both.",
            )

        if host:
            # A server with no sender address would send from the env default,
            # which is rarely what someone filling this screen in intends.
            if not cleaned.get("from_email"):
                self.add_error("from_email", "Required once an SMTP server is set.")
            if not cleaned.get("email_host_user") and not self.instance.has_email_password:
                self.add_error("email_host_user", "Required — most SMTP servers need a login.")
            password_now = cleaned.get("email_host_password")
            if not password_now and not self.instance.has_email_password:
                self.add_error("email_host_password", "Required — no password is saved yet.")
        return cleaned

    def save(self, commit=True):
        site = super().save(commit=False)
        if self.cleaned_data.get("clear_email_password"):
            site.email_host_password = ""
        elif self.cleaned_data.get("email_host_password"):
            site.email_host_password = self.cleaned_data["email_host_password"]
        if commit:
            site.save()
        return site
