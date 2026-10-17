"""Superadmin forms use the same organisation AI fields as the CRM."""
from django import forms

from apps.ai_engagement.services.playbook import MAX_PLAYBOOK_CHARS, validate_playbook


class AIBrainForm(forms.Form):
    revision = forms.CharField(widget=forms.HiddenInput)
    about = forms.CharField(label="Business description", required=False, max_length=100000, widget=forms.Textarea(attrs={"rows": 6}))
    bot_languages = forms.CharField(label="Bot languages", required=False, max_length=500, help_text="For example: English, Hindi, Hinglish.")
    ai_playbook = forms.CharField(label="AI Playbook", required=False, max_length=MAX_PLAYBOOK_CHARS, widget=forms.Textarea(attrs={"rows": 24}), help_text="The organisation's rules, welcome message, qualification questions and criteria, acknowledgement, stage movement, attribute mapping and reminders.")
    ai_enabled = forms.BooleanField(label="Enable organisation AI replies", required=False)
    bump_up_enabled = forms.BooleanField(label="Enable AI bump-ups", required=False)
    bump_up_count = forms.IntegerField(label="Maximum bump-ups per lead", min_value=0, max_value=2147483647)

    def clean_ai_playbook(self):
        try:
            return validate_playbook(self.cleaned_data["ai_playbook"])
        except (TypeError, ValueError) as exc:
            raise forms.ValidationError(str(exc)) from exc


class KnowledgeURLForm(forms.Form):
    name = forms.CharField(required=False, max_length=255)
    url = forms.URLField(label="Website URL", max_length=200)


class KnowledgeUploadForm(forms.Form):
    name = forms.CharField(required=False, max_length=255)
    file = forms.FileField(label="Knowledge file")
    share_instruction = forms.CharField(label="When should the AI share this file?", required=False, max_length=10000, widget=forms.Textarea(attrs={"rows": 3}), help_text="Optional. Leave empty for knowledge-only files. Indexing and file-sharing readiness are tracked separately.")


class FAQForm(forms.Form):
    question = forms.CharField(max_length=10000, widget=forms.Textarea(attrs={"rows": 2}))
    answer = forms.CharField(max_length=50000, widget=forms.Textarea(attrs={"rows": 4}))
    is_active = forms.BooleanField(required=False, initial=True, label="Use this FAQ in AI answers")
