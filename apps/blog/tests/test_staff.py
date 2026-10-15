import json

from django.contrib.auth.models import Permission
from django.contrib.sessions.backends.db import SessionStore
from django.contrib.sessions.models import Session
from django.test import Client
from django.urls import reverse

from apps.accounts.models import User
from apps.accounts.session_utils import get_session_cookie_name
from apps.blog.models import BlogImage, Post
from apps.blog.tests.helpers import BlogTestCase, article_data, authenticate_area, image_upload
from apps.organizations.models import Organization


def editor_payload(post=None, **overrides):
    data = dict(post.draft) if post else article_data()
    if isinstance(data.get("category"), dict):
        data["category"] = data["category"].get("name", "")
    if isinstance(data.get("tags"), list):
        data["tags"] = ", ".join(data["tags"])
    data["version"] = post.version if post else 0
    data.update(overrides)
    return data


class StaffBlogTests(BlogTestCase):
    def editor(self):
        user = User.objects.create_user(
            email="restricted-blog-editor@example.com", role="superadmin",
            name="Blog Editor", password="Editor-password-123!",
        )
        user.user_permissions.add(Permission.objects.get(content_type__app_label="blog", codename="change_post"))
        return user

    def json_post(self, url, data, client=None):
        return (client or self.client).post(url, json.dumps(data), content_type="application/json")

    def test_authenticated_publisher_can_create_preview_publish_and_update(self):
        authenticate_area(self.client, self.publisher)
        upload = self.client.post(reverse("blog_admin:upload_image"), {
            "image": image_upload(), "alt_text": "Workflow example",
        })
        self.assertEqual(upload.status_code, 201)
        media = upload.json()
        created = self.json_post(reverse("blog_admin:new"), editor_payload(cover_image=media["id"]))
        self.assertEqual(created.status_code, 201, created.content)
        post = Post.objects.get(pk=created.json()["post_id"])
        preview = self.client.get(created.json()["preview_url"])
        self.assertContains(preview, post.draft["title"])
        self.assertIn("no-store", preview["Cache-Control"])
        self.assertIn("noindex", preview["X-Robots-Tag"])
        self.assertContains(preview, media["preview_url"])
        self.assertEqual(Client().get(media["public_url"]).status_code, 404)

        published = self.json_post(created.json()["edit_url"], editor_payload(post, action="publish"))
        self.assertEqual(published.status_code, 200, published.content)
        post.refresh_from_db()
        self.assertEqual(post.status, "published")
        self.assertContains(Client().get(post.get_absolute_url()), post.published_snapshot["title"])

        update = self.json_post(created.json()["autosave_url"], editor_payload(post, title="Pending editorial update"))
        self.assertEqual(update.status_code, 200, update.content)
        post.refresh_from_db()
        self.assertEqual(post.draft["title"], "Pending editorial update")
        self.assertNotEqual(post.published_snapshot["title"], "Pending editorial update")

    def test_stale_autosave_returns_conflict_without_overwriting(self):
        authenticate_area(self.client, self.publisher)
        post = self.post()
        payload = editor_payload(post, title="First browser tab")
        url = reverse("blog_admin:autosave", kwargs={"post_id": post.pk})
        saved = self.json_post(url, payload)
        self.assertEqual(saved.status_code, 200)
        stale = self.json_post(url, {**payload, "title": "Stale browser tab"})
        self.assertEqual(stale.status_code, 409)
        post.refresh_from_db()
        self.assertEqual(post.draft["title"], "First browser tab")

    def test_invalid_publish_rolls_back_draft_changes_atomically(self):
        authenticate_area(self.client, self.publisher)
        post = self.publish()
        original_draft = post.draft.copy()
        original_live = post.published_snapshot.copy()
        version = post.version
        response = self.json_post(
            reverse("blog_admin:edit", kwargs={"post_id": post.pk}),
            editor_payload(post, title="Should not be silently saved", body_html="", action="publish"),
        )
        self.assertEqual(response.status_code, 400)
        post.refresh_from_db()
        self.assertEqual(post.version, version)
        self.assertEqual(post.draft, original_draft)
        self.assertEqual(post.published_snapshot, original_live)

    def test_restricted_editor_login_rotates_session_and_stays_within_blog(self):
        editor = self.editor()
        preauth = SessionStore()
        preauth["preauth_marker"] = "preserved"
        preauth.save()
        self.client.cookies[get_session_cookie_name("superadmin")] = preauth.session_key
        response = self.client.post(reverse("blog_admin:login"), {
            "username": editor.email, "password": "Editor-password-123!", "next": "/superadmin/",
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], reverse("blog_admin:index"))
        self.assertNotEqual(response.cookies[get_session_cookie_name("superadmin")].value, preauth.session_key)
        self.assertFalse(Session.objects.filter(session_key=preauth.session_key).exists())
        self.assertEqual(self.client.get(reverse("blog_admin:index")).status_code, 200)

        created = self.json_post(reverse("blog_admin:new"), editor_payload())
        self.assertEqual(created.status_code, 201, created.content)
        post = Post.objects.get(pk=created.json()["post_id"])
        response = self.json_post(created.json()["edit_url"], editor_payload(post, action="publish"))
        self.assertEqual(response.status_code, 403)
        post.refresh_from_db()
        self.assertIsNone(post.published_snapshot)
        self.assertEqual(self.client.get("/superadmin/").status_code, 302)
        still_authorized = self.client.get(reverse("blog_admin:index"))
        self.assertEqual(still_authorized.status_code, 200)
        self.assertNotContains(still_authorized, f'href="{reverse("superadmin-org-list")}"')
        self.assertNotContains(still_authorized, "Organization tools")

    def test_customer_with_blog_permissions_cannot_log_in_or_access_editor(self):
        organization = Organization.objects.create(name="Customer Blog Permission Boundary")
        customer = User.objects.create_user(
            email="customer-blog-admin@example.com", organization=organization, role="admin",
            password="Customer-password-123!", is_staff=True,
        )
        customer.user_permissions.add(*Permission.objects.filter(content_type__app_label="blog"))
        response = self.client.post(reverse("blog_admin:login"), {
            "username": customer.email, "password": "Customer-password-123!",
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "platform blog editor account")
        authenticate_area(self.client, customer)
        response = self.json_post(reverse("blog_admin:new"), editor_payload())
        self.assertIn(response.status_code, (302, 403))
        self.assertFalse(Post.objects.exists())

    def test_anonymous_requests_and_wrong_session_area_cannot_read_drafts(self):
        post = self.post(title="Confidential staff draft")
        image = self.upload()
        urls = (
            reverse("blog_admin:index"), reverse("blog_admin:new"),
            reverse("blog_admin:edit", kwargs={"post_id": post.pk}),
            reverse("blog_admin:preview", kwargs={"post_id": post.pk}), image.preview_url,
        )
        for url in urls:
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 302)
                self.assertNotContains(response, "Confidential staff draft", status_code=302)
        self.client.force_login(self.publisher)
        self.assertEqual(self.client.get(reverse("blog_admin:preview", kwargs={"post_id": post.pk})).status_code, 302)

    def test_mutating_browser_endpoints_enforce_csrf(self):
        client = Client(enforce_csrf_checks=True)
        authenticate_area(client, self.publisher)
        post = self.post()
        for url, payload in (
            (reverse("blog_admin:new"), editor_payload()),
            (reverse("blog_admin:autosave", kwargs={"post_id": post.pk}), editor_payload(post, title="Forbidden write")),
            (reverse("blog_admin:edit", kwargs={"post_id": post.pk}), editor_payload(post, action="publish")),
        ):
            with self.subTest(url=url):
                self.assertEqual(self.json_post(url, payload, client).status_code, 403)
        self.assertEqual(client.post(reverse("blog_admin:upload_image"), {"image": image_upload()}).status_code, 403)
        self.assertFalse(BlogImage.objects.exists())
        post.refresh_from_db()
        self.assertIsNone(post.published_snapshot)

    def test_malformed_autosave_and_missing_upload_return_actionable_errors(self):
        authenticate_area(self.client, self.publisher)
        post = self.post()
        response = self.client.post(
            reverse("blog_admin:autosave", kwargs={"post_id": post.pk}), "{broken", content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.json()["ok"])
        response = self.client.post(reverse("blog_admin:upload_image"), {})
        self.assertEqual(response.status_code, 400)
        self.assertIn("image", response.json()["detail"].lower())
