"""Shared fixtures for real blog publishing and request-boundary tests."""

from io import BytesIO
from tempfile import TemporaryDirectory

from django.contrib.auth import BACKEND_SESSION_KEY, HASH_SESSION_KEY, SESSION_KEY
from django.contrib.sessions.backends.db import SessionStore
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from PIL import Image

from apps.accounts.models import User
from apps.accounts.session_utils import get_session_cookie_name
from apps.blog import services


def article_data(**overrides):
    data = {
        "title": "A practical guide to reliable sales follow-ups",
        "slug": "reliable-sales-follow-ups",
        "excerpt": "A practical guide to keeping sales conversations useful and timely.",
        "body_html": (
            "<h2>Make the next action clear</h2><p>Useful follow-ups begin with "
            "the customer's actual request. Record what the customer needs, "
            "confirm who owns the conversation, and agree on the next action. "
            "When an appointment is booked, stop sending enquiry reminders. "
            "Review the delivery history before repeating a message.</p>"
            "<h2>Review your workflow</h2><p>Give every stage a clear purpose "
            "and review the results with your team each week.</p>"
        ),
        "category": {"name": "CRM & Sales", "slug": "crm-sales"},
        "tags": ["CRM", "Follow-ups"],
        "author_name": "SHVYA Editorial Team",
        "author_bio": "The team building practical tools for sales teams.",
        "author_url": "https://shvya-ai.com/",
        "seo_title": "Reliable sales follow-ups | SHVYA AI",
        "meta_description": "Learn how to keep follow-ups relevant, assign ownership, and stop enquiry reminders after a customer books.",
        "focus_keyword": "sales follow-ups",
        "social_title": "A practical guide to sales follow-ups",
        "social_description": "Keep every customer conversation useful.",
        "cover_image": "",
        "social_image": "",
        "canonical_url": "",
        "featured": False,
        "noindex": False,
    }
    data.update(overrides)
    return data


def image_upload(name="example.png", size=(48, 32), image_format="PNG"):
    output = BytesIO()
    Image.new("RGB", size, (36, 99, 216)).save(output, format=image_format)
    return SimpleUploadedFile(name, output.getvalue(), content_type=f"image/{image_format.lower()}")


def authenticate_area(client, user, area="superadmin"):
    """Use the real dedicated session cookie rather than bypassing middleware."""
    session = SessionStore()
    session[SESSION_KEY] = str(user.pk)
    session[BACKEND_SESSION_KEY] = "django.contrib.auth.backends.ModelBackend"
    session[HASH_SESSION_KEY] = user.get_session_auth_hash()
    session.save()
    client.cookies[get_session_cookie_name(area)] = session.session_key


@override_settings(
    API_ACTIVITY_ENABLED=False,
    BLOG_PUBLIC_ORIGIN="https://shvya-ai.com",
    CACHES={"default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "blog-regression-tests",
    }},
)
class BlogTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.publisher = User.objects.create_superuser(
            email="blog-publisher@example.com", name="Blog Publisher", password="Test-password-123!",
        )

    def setUp(self):
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)
        media = TemporaryDirectory(prefix="shvya-blog-tests-")
        self.addCleanup(media.cleanup)
        storage_settings = override_settings(
            MEDIA_ROOT=media.name,
            STORAGES={
                "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
                "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
            },
        )
        storage_settings.enable()
        self.addCleanup(storage_settings.disable)

    def post(self, **overrides):
        return services.create_post(article_data(**overrides), actor=self.publisher)

    def publish(self, post=None, **overrides):
        post = post or self.post(**overrides)
        services.publish_post(post, actor=self.publisher, expected_version=post.version)
        post.refresh_from_db()
        return post

    def save(self, post, **overrides):
        data = {**post.draft, **overrides}
        services.save_draft(post, data, actor=self.publisher, expected_version=post.version)
        post.refresh_from_db()
        return post

    def upload(self, **kwargs):
        return services.upload_image(
            image_upload(**kwargs), actor=self.publisher, alt_text="A blue workflow illustration",
        )
