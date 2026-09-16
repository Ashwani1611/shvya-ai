from datetime import timedelta

from django import forms
from django.utils import timezone


BOOKING_TIME_CHOICES = [
    ("10:00", "10:00 AM IST"),
    ("11:00", "11:00 AM IST"),
    ("12:00", "12:00 PM IST"),
    ("14:00", "2:00 PM IST"),
    ("15:00", "3:00 PM IST"),
    ("16:00", "4:00 PM IST"),
    ("17:00", "5:00 PM IST"),
]


class MarketingBookingForm(forms.Form):
    name = forms.CharField(max_length=100)
    email = forms.EmailField(max_length=200)
    phone = forms.CharField(max_length=32)
    company = forms.CharField(max_length=150, required=False)
    preferred_date = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}))
    preferred_time = forms.ChoiceField(choices=BOOKING_TIME_CHOICES)
    goal = forms.CharField(max_length=1200, widget=forms.Textarea(attrs={"rows": 5}))
    interest = forms.CharField(max_length=140, required=False, widget=forms.HiddenInput)
    website = forms.CharField(required=False, widget=forms.HiddenInput)
    consent = forms.BooleanField(
        required=True,
        label="I agree that Shvya AI can contact me about this sales session request.",
    )

    def clean_website(self):
        value = self.cleaned_data.get("website")
        if value:
            raise forms.ValidationError("Invalid submission.")
        return value

    def clean_preferred_date(self):
        value = self.cleaned_data["preferred_date"]
        today = timezone.localdate()
        if value < today:
            raise forms.ValidationError("Choose today or a future date.")
        if value > today + timedelta(days=90):
            raise forms.ValidationError("Choose a date within the next 90 days.")
        return value
