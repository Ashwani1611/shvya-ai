from django import forms

from .models import WhatsAppAccount


class WhatsAppConnectAPIForm(forms.ModelForm):
    """
    "Connect API" path -- the organization brings their own Meta
    WhatsApp Business Platform credentials.
    """

    access_token = forms.CharField(
        widget=forms.PasswordInput(
            render_value=False,
        ),
        help_text="Meta system-user access token. Stored encrypted, never shown again.",
    )

    class Meta:
        model = WhatsAppAccount
        fields = [
            "phone_number_id",
            "waba_id",
            "display_phone_number",
            "access_token",
        ]

    def clean_phone_number_id(self):
        phone_number_id = self.cleaned_data["phone_number_id"].strip()

        if not phone_number_id:
            raise forms.ValidationError(
                "phone_number_id is required to connect via API."
            )

        return phone_number_id

    def clean_access_token(self):
        access_token = self.cleaned_data["access_token"].strip()

        if not access_token:
            raise forms.ValidationError(
                "An access token is required to connect via API."
            )

        return access_token

    def save(self, organization, commit=True):

        account = super().save(commit=False)

        account.organization = organization
        account.connection_type = WhatsAppAccount.ConnectionType.API
        account.status = WhatsAppAccount.Status.CONNECTED

        if commit:
            account.save()

        return account


class WhatsAppHostedRequestForm(forms.Form):
    """
    "Hosted Account" path -- the organization asks SHVYA to
    provision and manage a WhatsApp number on their behalf.

    No Meta credentials are collected here. This just records
    the request; actual provisioning (embedded signup / tech
    provider flow) happens as a separate step and fills in
    WhatsAppAccount.phone_number_id / access_token once ready.
    """

    display_phone_number = forms.CharField(
        required=False,
        help_text="Existing business number to port in, if any. Leave blank to get a new number.",
    )

    notes = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text="Anything SHVYA's team should know before provisioning.",
    )

    def save(self, organization):

        account, _created = WhatsAppAccount.objects.update_or_create(
            organization=organization,
            defaults={
                "connection_type": WhatsAppAccount.ConnectionType.HOSTED,
                "status": WhatsAppAccount.Status.PENDING,
                "display_phone_number": self.cleaned_data.get(
                    "display_phone_number", ""
                ),
            },
        )

        return account