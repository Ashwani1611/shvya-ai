import re

from django import forms
from django.core.exceptions import ValidationError

from apps.crm.models import AttributeDefinition, Pipeline, Stage
from apps.integrations.models import MetaConversionMapping


USER_DATA_FIELDS = {
    "name": "Name", "phone": "Phone", "email": "Email",
    "lead_id": "Meta Lead ID", "external_id": "Shvya lead ID",
    "wa_ref_ctwa_clid": "WhatsApp ad click ID", "ip_address": "Customer IP address",
    "user_agent": "Customer browser", "fbp": "Browser ID (fbp)", "fbc": "Click ID (fbc)",
    "city": "City", "state": "State", "zip_code": "Postal code", "country": "Country",
    "gender": "Gender", "date_of_birth": "Date of birth",
}
DEFAULT_USER_DATA_FIELDS = ["name", "phone", "email", "lead_id", "external_id"]
CURRENCIES = ["INR", "USD", "EUR", "GBP"]
ACTION_SOURCES = {
    "system_generated": "CRM update", "phone_call": "Phone call", "chat": "Chat",
    "email": "Email", "physical_store": "In store", "website": "Website", "other": "Other",
}
# Customer identifiers belong in hashed user_data, never arbitrary custom_data.
PRIVATE_ATTRIBUTE = re.compile(
    r"token|secret|password|credential|prompt|health|medical|diagnos|symptom|pain|"
    r"email|phone|mobile|(^|_)(name|city|state|zip|postal|country|address|notes|remarks|birth|dob)(_|$)|"
    r"(first|last|full|customer|contact|lead)_name|date_of_birth|gender|ip_address|user_agent|"
    r"(^|_)(ai|internal|human_intervention|meta|wa_ref|fbp|fbc)(_|$)", re.I,
)
RESERVED_CUSTOM_KEYS = {
    "event_source", "lead_event_source", "value", "currency", "event_name",
    "event_time", "event_id", "action_source", "user_data", "custom_data",
}


def shareable_attributes(organization):
    return [
        attribute for attribute in AttributeDefinition.objects.filter(
            organization=organization, is_active=True,
        )
        if not PRIVATE_ATTRIBUTE.search(attribute.key.replace("-", "_"))
        and attribute.key not in RESERVED_CUSTOM_KEYS
    ]


class MetaConversionsSettingsForm(forms.Form):
    dataset_id = forms.RegexField(r"^[0-9]{5,30}$", max_length=30)
    access_token = forms.CharField(required=False, max_length=8000, strip=True)
    is_enabled = forms.BooleanField(required=False)
    test_mode = forms.BooleanField(required=False)
    test_event_code = forms.RegexField(r"^[A-Za-z0-9_-]+$", required=False, max_length=100)
    event_scope = forms.ChoiceField(choices=[
        ("meta_leads", "Meta Lead Ads only"), ("all_leads", "All lead sources"),
    ])
    user_data_fields = forms.MultipleChoiceField(choices=list(USER_DATA_FIELDS.items()))
    custom_attribute_keys = forms.MultipleChoiceField(required=False)

    def __init__(self, *args, configuration, **kwargs):
        super().__init__(*args, **kwargs)
        self.configuration = configuration
        self.fields["custom_attribute_keys"].choices = [
            (a.key, a.name) for a in shareable_attributes(configuration.organization)
        ]

    def clean(self):
        data = super().clean()
        token = data.get("access_token", "")
        if any(character.isspace() for character in token):
            self.add_error("access_token", "Paste the token without spaces or line breaks.")
        if not token and not self.configuration.has_access_token:
            self.add_error("access_token", "Enter the Conversions API access token.")
        if (self.configuration.dataset_id and data.get("dataset_id") != self.configuration.dataset_id
                and not token):
            self.add_error("access_token", "Enter a token for the new dataset.")
        if data.get("is_enabled") and data.get("test_mode") and not data.get("test_event_code"):
            self.add_error("test_event_code", "Enter a Test Events code or choose live delivery.")
        return data


class MetaConversionMappingForm(forms.ModelForm):
    event_name = forms.RegexField(r"^[A-Za-z][A-Za-z0-9_ ]{0,99}$", max_length=100)
    action_source = forms.ChoiceField(choices=list(ACTION_SOURCES.items()))
    currency = forms.ChoiceField(required=False, choices=[("", "Select currency"), *[(c, c) for c in CURRENCIES]])

    class Meta:
        model = MetaConversionMapping
        fields = [
            "pipeline", "stage", "event_name", "is_enabled", "action_source",
            "value_source", "static_value", "value_attribute", "currency",
        ]

    def __init__(self, *args, configuration, **kwargs):
        super().__init__(*args, **kwargs)
        self.instance.configuration = configuration
        self.fields["pipeline"].queryset = Pipeline.objects.filter(
            organization=configuration.organization, is_active=True,
        )
        self.fields["stage"].queryset = Stage.objects.filter(
            pipeline__organization=configuration.organization,
            pipeline__is_active=True, is_active=True,
        )

    def clean(self):
        data = super().clean()
        pipeline, stage = data.get("pipeline"), data.get("stage")
        if pipeline and stage and stage.pipeline_id != pipeline.pk:
            self.add_error("stage", "Choose a stage in the selected pipeline.")
        if self.instance.is_default:
            if (data.get("event_name") != "Lead" or
                    (stage and stage.pk != self.instance.stage_id) or
                    (pipeline and pipeline.pk != self.instance.pipeline_id)):
                raise ValidationError("The default New Lead mapping keeps its pipeline, stage and Lead event.")
        source = data.get("value_source")
        if source == "static":
            value = data.get("static_value")
            if value is None or not value.is_finite() or value < 0:
                self.add_error("static_value", "Enter a non-negative event value.")
            data["value_attribute"] = ""
        elif source == "attribute":
            key = data.get("value_attribute", "")
            if key not in {a.key for a in shareable_attributes(self.instance.configuration.organization)
                           if a.field_type == "numeric"}:
                self.add_error("value_attribute", "Choose an active numeric attribute in your organization.")
            data["static_value"] = None
        else:
            data.update(static_value=None, value_attribute="", currency="")
        if source in {"static", "attribute"} and not data.get("currency"):
            self.add_error("currency", "Choose the currency for the event value.")
        if data.get("event_name") == "Purchase" and source not in {"static", "attribute"}:
            self.add_error("value_source", "Purchase events require a value and currency.")
        return data
