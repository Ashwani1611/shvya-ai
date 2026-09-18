import uuid
from django import forms
from django.core.exceptions import ValidationError

from .access import allowed_pipelines, staff_users
from .fields import field_for
from .models import (CustomField, OrganizationSupportPolicy, SavedReply, SupportSettings,
                     TicketCategory, TicketIssue, TicketPriority, TicketStatus, WorkItem)


class MultipleFileInput(forms.ClearableFileInput):
    allow_multiple_selected = True


class MultipleFileField(forms.FileField):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("widget", MultipleFileInput)
        kwargs.setdefault("required", False)
        super().__init__(*args, **kwargs)

    def clean(self, data, initial=None):
        if not data:
            return []
        values = data if isinstance(data, (list, tuple)) else [data]
        return [super(MultipleFileField, self).clean(value, initial) for value in values]


class CustomMixin:
    def add_custom(self, *, staff=False, values=None):
        definitions = CustomField.objects.filter(active=True)
        if not staff:
            definitions = definitions.filter(customer_visible=True)
        self.custom_definitions = list(definitions)
        for definition in self.custom_definitions:
            field = field_for(definition)
            field.initial = (values or {}).get(definition.key)
            self.fields["custom__"+definition.key] = field

    def custom_values(self):
        return {d.key: self.cleaned_data.get("custom__"+d.key) for d in self.custom_definitions}


class CreateTicketForm(CustomMixin, forms.Form):
    category = forms.ModelChoiceField(queryset=TicketCategory.objects.none(), empty_label="Choose a category")
    issue = forms.ModelChoiceField(queryset=TicketIssue.objects.none(), empty_label="Choose an issue")
    subject = forms.CharField(max_length=200, label="What needs our attention?",
                             widget=forms.TextInput(attrs={"placeholder": "A short, specific summary"}))
    body = forms.CharField(max_length=30000, label="Tell us what happened",
                          widget=forms.Textarea(attrs={"rows": 5, "placeholder": "What did you expect? What happened instead? Steps to reproduce help us resolve it."}))
    priority = forms.ModelChoiceField(queryset=TicketPriority.objects.none(), empty_label=None)
    pipeline = forms.ModelChoiceField(queryset=None, required=False, empty_label="Not pipeline-specific")
    attachments = MultipleFileField(label="Attachments")
    client_key = forms.UUIDField(widget=forms.HiddenInput, initial=uuid.uuid4)
    source_path = forms.CharField(max_length=1000, required=False, widget=forms.HiddenInput)

    def __init__(self, *args, actor, **kwargs):
        kwargs.setdefault("auto_id", "sp-create-%s")
        super().__init__(*args, **kwargs)
        self.fields["category"].queryset = TicketCategory.objects.filter(active=True)
        self.fields["issue"].queryset = TicketIssue.objects.filter(active=True, category__active=True)
        self.fields["priority"].queryset = TicketPriority.objects.filter(active=True)
        self.fields["priority"].initial = TicketPriority.objects.filter(key="medium", active=True).first()
        self.fields["pipeline"].queryset = allowed_pipelines(actor)
        self.add_custom()

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("issue") and cleaned.get("category") and cleaned["issue"].category_id != cleaned["category"].pk:
            self.add_error("issue", "Choose an issue within the selected category.")
        return cleaned


class ReplyForm(forms.Form):
    body = forms.CharField(max_length=30000, widget=forms.Textarea(attrs={"rows": 4,
                          "placeholder": "Write a helpful reply…", "data-reply-body": ""}), label="Message")
    attachments = MultipleFileField()
    client_key = forms.UUIDField(widget=forms.HiddenInput, initial=uuid.uuid4)
    internal = forms.BooleanField(required=False, label="Internal note · Shvya-Ops only")
    status = forms.ModelChoiceField(queryset=TicketStatus.objects.none(), required=False, label="After replying")
    assign_me = forms.BooleanField(required=False, label="Assign to me")

    def __init__(self, *args, staff=False, **kwargs):
        kwargs.setdefault("auto_id", "sp-reply-%s")
        super().__init__(*args, **kwargs)
        if staff:
            self.fields["status"].queryset = TicketStatus.objects.filter(active=True)
            self.fields["status"].initial = TicketStatus.objects.filter(key="answered", system=True).first()
        else:
            for key in ("internal", "status", "assign_me"):
                self.fields.pop(key)


class PropertiesForm(CustomMixin, forms.Form):
    status = forms.ModelChoiceField(queryset=TicketStatus.objects.filter(active=True))
    priority = forms.ModelChoiceField(queryset=TicketPriority.objects.filter(active=True))
    assignee = forms.ModelChoiceField(queryset=None, required=False, empty_label="Unassigned")
    category = forms.ModelChoiceField(queryset=TicketCategory.objects.filter(active=True))
    issue = forms.ModelChoiceField(queryset=TicketIssue.objects.filter(active=True, category__active=True))
    version = forms.IntegerField(widget=forms.HiddenInput)

    def __init__(self, *args, ticket, **kwargs):
        kwargs.setdefault("auto_id", "sp-properties-%s")
        super().__init__(*args, **kwargs)
        self.fields["assignee"].queryset = staff_users()
        for name in ("status", "priority", "assignee", "category", "issue"):
            self.fields[name].initial = getattr(ticket, name+"_id")
        self.fields["version"].initial = ticket.version
        self.add_custom(staff=True, values=ticket.custom_values)


class ShareForm(forms.Form):
    label = forms.CharField(max_length=80, initial="Shared viewer", label="Viewer label")
    days = forms.IntegerField(min_value=1, max_value=30, initial=7, label="Expires after (days)")
    can_reply = forms.BooleanField(required=False, label="Allow replies")
    can_close = forms.BooleanField(required=False, label="Allow close / reopen")
    acknowledge = forms.BooleanField(label="Anyone with this link can read the public conversation and download its public attachments.")


class WorkItemForm(forms.ModelForm):
    class Meta:
        model = WorkItem
        fields = ("kind", "title", "assigned_to", "due_at")
        widgets = {"due_at": forms.DateTimeInput(attrs={"type": "datetime-local"})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["assigned_to"].queryset = staff_users()
        self.fields["due_at"].help_text = "Shown in the application's current timezone."


def option_form(model, fields):
    return forms.modelform_factory(model, fields=fields)


CONFIG_FORMS = {
    "categories": (TicketCategory, option_form(TicketCategory, ["name", "description", "order", "active"])),
    "issues": (TicketIssue, option_form(TicketIssue, ["category", "name", "guidance", "order", "active"])),
    "statuses": (TicketStatus, option_form(TicketStatus, ["name", "key", "behavior", "order", "active"])),
    "priorities": (TicketPriority, option_form(TicketPriority, ["name", "key", "order", "active"])),
    "fields": (CustomField, option_form(CustomField, ["name", "key", "kind", "options", "help_text", "customer_visible", "required", "required_on_close", "order", "active"])),
    "replies": (SavedReply, option_form(SavedReply, ["name", "body", "knowledge_url", "order", "active"])),
    "access": (OrganizationSupportPolicy, option_form(OrganizationSupportPolicy, ["organization", "own_tickets_only", "email_notifications"])),
    "settings": (SupportSettings, option_form(SupportSettings, ["auto_assign_first_reply", "notify_customer", "notify_staff", "assignee_only_notifications", "notify_organization_admins", "allow_customer_close", "auto_close_hours", "max_files", "max_file_mb", "max_total_mb", "allowed_extensions", "email_intake_enabled", "email_replies_only", "email_issue", "email_priority", "blocked_senders", "blocked_subject_terms", "new_tickets_per_hour"])),
}


class FilterForm(forms.Form):
    q = forms.CharField(max_length=200, required=False)
    status = forms.IntegerField(min_value=1, required=False)
    category = forms.IntegerField(min_value=1, required=False)
    priority = forms.IntegerField(min_value=1, required=False)
    organization = forms.UUIDField(required=False)
    assignee = forms.UUIDField(required=False)
    view = forms.ChoiceField(choices=[("", "All tickets"), ("mine", "Assigned to me"), ("unassigned", "Unassigned"), ("waiting", "Waiting on customer")], required=False)
    date_from = forms.DateField(required=False, widget=forms.DateInput(attrs={"type": "date"}))
    date_to = forms.DateField(required=False, widget=forms.DateInput(attrs={"type": "date"}))

    def clean(self):
        data = super().clean()
        if data.get("date_from") and data.get("date_to") and data["date_from"] > data["date_to"]:
            raise ValidationError("The start date must be before the end date.")
        return data
