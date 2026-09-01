"""Admin account management forms (FR-8)."""
from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import UserCreationForm

User = get_user_model()


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
    """Edit an admin account (keep passwords hashed — reset via Django admin)."""

    class Meta:
        model = User
        fields = ["email", "first_name", "last_name", "is_active", "is_superuser"]
        widgets = {"email": forms.EmailInput}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["is_superuser"].label = "Full access (can manage admin accounts)"
        self.fields["is_superuser"].help_text = ""
