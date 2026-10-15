import json
from uuid import UUID, uuid4

from bs4 import BeautifulSoup
from django.urls import reverse

from apps.accounts.models import User
from apps.blog import services
from apps.blog.models import Post, Revision
from apps.blog.tests.helpers import BlogTestCase, article_data, authenticate_area


class BlogCreationRetryTests(BlogTestCase):
    def setUp(self):
        super().setUp()
        authenticate_area(self.client, self.publisher)
        self.url = reverse("blog_admin:new")

    def data(self, **overrides):
        data = article_data()
        data.update(category=data["category"]["name"], tags=", ".join(data["tags"]),
                    creation_id=str(uuid4()), version=0, action="save")
        data.update(overrides)
        return data

    def send(self, data):
        return self.client.post(self.url, json.dumps(data), content_type="application/json")

    def test_new_editor_includes_a_unique_stable_creation_key(self):
        keys = []
        for _ in range(2):
            response = self.client.get(self.url)
            field = BeautifulSoup(response.content, "html.parser").find("input", attrs={"name": "creation_id"})
            self.assertEqual(field["type"], "hidden")
            keys.append(UUID(field["value"]))
        self.assertNotEqual(*keys)

    def test_lost_save_response_can_be_retried_without_duplicate_article_or_revision(self):
        data = self.data()
        first = self.send(data)
        retry = self.send(data)
        self.assertEqual(first.status_code, 201)
        self.assertEqual(retry.status_code, 200)
        self.assertEqual(first.json()["post_id"], retry.json()["post_id"])
        self.assertEqual(first.json()["version"], retry.json()["version"])
        self.assertEqual(Post.objects.count(), 1)
        self.assertEqual(Revision.objects.count(), 1)

    def test_changed_retry_payload_cannot_overwrite_the_first_committed_draft(self):
        data = self.data()
        first = self.send(data)
        conflict = self.send({**data, "title": "Newer unsaved browser edits"})
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(conflict.json()["edit_url"], first.json()["edit_url"])
        post = Post.objects.get(pk=data["creation_id"])
        self.assertEqual(post.draft["title"], data["title"])
        self.assertEqual(post.revisions.count(), 1)

    def test_old_creation_retry_cannot_replace_later_saved_content(self):
        data = self.data()
        first = self.send(data)
        post = Post.objects.get(pk=data["creation_id"])
        self.save(post, title="A newer saved article title")
        conflict = self.send(data)
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(conflict.json()["edit_url"], first.json()["edit_url"])
        post.refresh_from_db()
        self.assertEqual(post.draft["title"], "A newer saved article title")
        self.assertEqual(post.revisions.count(), 2)

    def test_creation_key_from_another_author_returns_no_recovery_url(self):
        data = self.data()
        self.assertEqual(self.send(data).status_code, 201)
        other = User.objects.create_superuser(email="other-blog-author@example.com", password="Test-123!")
        authenticate_area(self.client, other)
        conflict = self.send(data)
        self.assertEqual(conflict.status_code, 409)
        self.assertNotIn("edit_url", conflict.json())
        self.assertEqual(Post.objects.count(), 1)
        self.assertEqual(Revision.objects.count(), 1)

    def test_creation_retries_never_replay_publishing_after_unpublish(self):
        data = self.data(action="publish")
        first = self.send(data)
        self.assertEqual(first.status_code, 201)
        post = Post.objects.get(pk=data["creation_id"])
        revision_count = post.revisions.count()
        conflict = self.send(data)
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(conflict.json()["edit_url"], first.json()["edit_url"])
        self.assertEqual(post.revisions.count(), revision_count)
        services.change_status(post, Post.Status.DRAFT, self.publisher, post.version)
        post.refresh_from_db()
        unpublished_version = post.version
        self.assertEqual(self.send(data).status_code, 409)
        post.refresh_from_db()
        self.assertIsNone(post.published_snapshot)
        self.assertEqual(post.version, unpublished_version)

    def test_invalid_creation_key_is_a_form_error(self):
        response = self.send(self.data(creation_id="not-a-uuid"))
        self.assertEqual(response.status_code, 400)
        self.assertIn("creation_id", response.json()["errors"])
        self.assertFalse(Post.objects.exists())
