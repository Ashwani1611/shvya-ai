import uuid
from zoneinfo import ZoneInfo

from django import forms
from django.utils import timezone
from django.utils.text import slugify

from .content import MAX_HTML_LENGTH


class PostForm(forms.Form):
    title = forms.CharField(max_length=220)
    slug = forms.SlugField(max_length=180, required=False)
    excerpt = forms.CharField(max_length=500, required=False, widget=forms.Textarea(attrs={"rows": 3}))
    body_html = forms.CharField(max_length=MAX_HTML_LENGTH, required=False, widget=forms.Textarea)
    category = forms.CharField(max_length=80, required=False)
    tags = forms.CharField(max_length=500, required=False)
    author_name = forms.CharField(max_length=120, required=False)
    author_bio = forms.CharField(max_length=500, required=False, widget=forms.Textarea(attrs={"rows": 3}))
    author_url = forms.URLField(max_length=500, required=False)
    cover_image = forms.UUIDField(required=False, widget=forms.HiddenInput)
    seo_title = forms.CharField(max_length=220, required=False)
    meta_description = forms.CharField(max_length=500, required=False, widget=forms.Textarea(attrs={"rows": 3}))
    focus_keyword = forms.CharField(max_length=160, required=False)
    social_title = forms.CharField(max_length=220, required=False)
    social_description = forms.CharField(max_length=500, required=False, widget=forms.Textarea(attrs={"rows": 3}))
    social_image = forms.UUIDField(required=False, widget=forms.HiddenInput)
    featured = forms.BooleanField(required=False)
    noindex = forms.BooleanField(required=False)
    canonical_url = forms.URLField(max_length=500, required=False)
    version = forms.IntegerField(min_value=0, widget=forms.HiddenInput)
    creation_id = forms.UUIDField(required=False, widget=forms.HiddenInput)
    schedule_at = forms.DateTimeField(required=False, input_formats=["%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S"],
                                       widget=forms.DateTimeInput(format="%Y-%m-%dT%H:%M", attrs={"type": "datetime-local"}))

    def __init__(self, *args, post=None, **kwargs):
        initial = dict(post.draft) if post else {"author_name": "SHVYA Team"}
        if isinstance(initial.get("category"), dict):
            initial["category"] = initial["category"].get("name", "")
        if isinstance(initial.get("tags"), list):
            initial["tags"] = ", ".join(initial["tags"])
        initial["version"] = post.version if post else 0
        initial["creation_id"] = post.pk if post else uuid.uuid4()
        if post and post.scheduled_at:
            initial["schedule_at"] = timezone.localtime(post.scheduled_at, ZoneInfo("Asia/Kolkata"))
        initial.update(kwargs.pop("initial", {}))
        super().__init__(*args, initial=initial, **kwargs)
        self.post = post
        self.fields["schedule_at"].help_text = "India Standard Time (IST)."

    def clean_slug(self):
        return self.cleaned_data.get("slug") or slugify(self.cleaned_data.get("title", ""))[:180]

    def clean_schedule_at(self):
        value = self.cleaned_data.get("schedule_at")
        if value:
            # datetime-local has no timezone: the product consistently labels this IST.
            value = value.replace(tzinfo=None).replace(tzinfo=ZoneInfo("Asia/Kolkata"))
        return value
