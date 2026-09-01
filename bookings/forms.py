"""Visitor booking form (FR-4.4) and admin booking forms (FR-7.1)."""
from django import forms

from .models import Booking


class VisitorBookingForm(forms.Form):
    """Required fields are configurable per event (FR-4.4)."""

    visitor_name = forms.CharField(max_length=200, label="Your name")
    visitor_email = forms.EmailField(label="Your email")
    visitor_phone = forms.CharField(max_length=40, required=False, label="Phone number")
    company = forms.CharField(max_length=200, required=False, label="Company")
    notes = forms.CharField(
        required=False, widget=forms.Textarea(attrs={"rows": 2}), label="Anything you'd like to discuss (optional)"
    )

    def __init__(self, *args, event=None, **kwargs):
        super().__init__(*args, **kwargs)
        if event is not None:
            self.fields["visitor_phone"].required = event.require_phone
            self.fields["company"].required = event.require_company


class AdminBookingForm(forms.ModelForm):
    """Admin creating/editing a booking on a visitor's behalf (BR-12, FR-7.1)."""

    class Meta:
        model = Booking
        fields = ["visitor_name", "visitor_email", "visitor_phone", "company", "notes", "status"]
        widgets = {"notes": forms.Textarea(attrs={"rows": 2})}
