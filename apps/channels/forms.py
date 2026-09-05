from django import forms

from apps.crm.models import Pipeline, Stage, Tag

from .models import WhatsAppAccount, WhatsAppTemplate


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

        phone_number_id = self.cleaned_data["phone_number_id"]

        # Update the existing account for this number if one already
        # exists, instead of always creating a new row. Without this,
        # every resubmit (e.g. pasting a fresh token after the old one
        # expired) created a duplicate WhatsAppAccount, and any lead
        # that had already messaged through the old row stayed stuck
        # on it via resolve_account_for_lead()'s "reuse this lead's
        # existing account" priority.
        account = WhatsAppAccount.objects.filter(
            organization=organization,
            phone_number_id=phone_number_id,
        ).first()

        if account is None:
            account = super().save(commit=False)
        else:
            account.waba_id = self.cleaned_data["waba_id"]
            account.display_phone_number = self.cleaned_data["display_phone_number"]
            account.access_token = self.cleaned_data["access_token"]

        account.organization = organization
        account.connection_type = WhatsAppAccount.ConnectionType.API
        account.status = WhatsAppAccount.Status.CONNECTED
        # Reconnecting an old disconnected number must make it visible/usable
        # again. Previously status became CONNECTED while is_active stayed False.
        account.is_active = True

        if commit:
            account.save()

        return account


class WhatsAppHostedRequestForm(forms.Form):
    """
    "coexisted Account" path -- the organization asks SHVYA to
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
        # NOTE: always creates a new row now -- an organization can
        # have several WhatsApp numbers (WhatsAppAccount.organization
        # is a ForeignKey, not OneToOne), so update_or_create by
        # organization alone would have either crashed
        # (MultipleObjectsReturned) or silently overwritten an
        # unrelated existing account.
        account = WhatsAppAccount.objects.create(
            organization=organization,
            connection_type=WhatsAppAccount.ConnectionType.coexisted,
            status=WhatsAppAccount.Status.PENDING,
            display_phone_number=self.cleaned_data.get(
                "display_phone_number", ""
            ),
        )

        return account


class BulkCampaignForm(forms.Form):
    """
    Compose screen for a bulk WhatsApp send. Pipeline/stage/tag/
    account querysets are scoped to the requesting organization by
    the view before this form is instantiated.
    """

    name = forms.CharField(
        max_length=150,
        help_text="Internal name for this campaign, not shown to leads.",
    )

    account = forms.ModelChoiceField(
        queryset=WhatsAppAccount.objects.none(),
        help_text="Which connected WhatsApp number to send this campaign from.",
    )

    pipeline = forms.ModelChoiceField(
        queryset=Pipeline.objects.none(),
    )

    stage = forms.ModelChoiceField(
        queryset=Stage.objects.none(),
        required=False,
        help_text="Leave blank to target every stage in the pipeline.",
    )

    tag = forms.ModelChoiceField(
        queryset=Tag.objects.none(),
        required=False,
        help_text="Optional: narrow further to leads with this tag.",
    )

    body = forms.CharField(
        widget=forms.Textarea(attrs={"rows": 4}),
        help_text=(
            "Sent as free text to leads who messaged in the last 24h. "
            "Leads outside that window are skipped unless a template is set."
        ),
    )

    template_name = forms.CharField(
        max_length=100,
        required=False,
        help_text="Meta-approved template name, for leads outside the 24h window.",
    )

    def __init__(self, *args, organization=None, **kwargs):
        super().__init__(*args, **kwargs)

        self.fields["account"].queryset = WhatsAppAccount.objects.filter(
            organization=organization,
            is_active=True,
            status=WhatsAppAccount.Status.CONNECTED,
        )

        self.fields["pipeline"].queryset = Pipeline.objects.filter(
            organization=organization,
            is_active=True,
        )

        self.fields["stage"].queryset = Stage.objects.filter(
            pipeline__organization=organization,
            is_active=True,
        )

        self.fields["tag"].queryset = Tag.objects.filter(
            organization=organization,
        )

    def clean(self):
        cleaned_data = super().clean()

        stage = cleaned_data.get("stage")
        pipeline = cleaned_data.get("pipeline")

        if stage and pipeline and stage.pipeline_id != pipeline.id:
            raise forms.ValidationError(
                "Selected stage does not belong to the selected pipeline."
            )

        return cleaned_data


BUTTON_CHOICES = [
    ("visit_website", "Visit Website"),
    ("call_phone", "Call Phone"),
    ("copy_offer", "Copy Offer"),
    ("text_back", "Text Back"),
    ("request_contact_info", "Request Contact Info"),
]


class WhatsAppTemplateForm(forms.ModelForm):
    """
    Template builder -- matches the Kraya "New Message Template"
    screen: name/category/business, standard vs carousel format,
    message body with {{variable}} placeholders, attachment type,
    footer, and a set of quick-action buttons.
    """

    buttons = forms.MultipleChoiceField(
        choices=BUTTON_CHOICES,
        required=False,
        widget=forms.CheckboxSelectMultiple,
    )

    class Meta:
        model = WhatsAppTemplate
        fields = [
            "account",
            "name",
            "category",
            "template_format",
            "body",
            "attachment_type",
            "footer",
        ]
        widgets = {
            "body": forms.Textarea(attrs={"rows": 6, "maxlength": 1024}),
        }

    def __init__(self, *args, organization=None, **kwargs):
        super().__init__(*args, **kwargs)

        self.fields["account"].queryset = WhatsAppAccount.objects.filter(
            organization=organization,
        )

    def clean_buttons(self):
        # Stored as a JSON list of {"type": <choice>} dicts on the
        # model, matching WhatsAppTemplate.buttons -- keeps the
        # door open for per-button config (a URL for visit_website,
        # a number for call_phone) without a schema change later.
        return [{"type": value} for value in self.cleaned_data["buttons"]]

    def save(self, organization, created_by, commit=True):

        template = super().save(commit=False)

        template.organization = organization
        template.created_by = created_by
        template.buttons = self.cleaned_data.get("buttons", [])

        if commit:
            template.save()

        return template
