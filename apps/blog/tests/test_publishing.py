from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth.models import AnonymousUser, Permission
from django.core.exceptions import PermissionDenied, ValidationError
from django.utils import timezone

from apps.accounts.models import User
from apps.blog import services
from apps.blog.models import Post, SlugRedirect
from apps.blog.tests.helpers import BlogTestCase, article_data
from apps.organizations.models import Organization


class PublishingLifecycleTests(BlogTestCase):
    def test_publish_creates_a_separate_live_snapshot(self):
        post = self.post()
        self.assertIsNone(post.published_snapshot)
        self.assertFalse(services.published_posts().filter(pk=post.pk).exists())

        self.publish(post)
        post.refresh_from_db()
        self.assertEqual(post.published_snapshot["title"], article_data()["title"])
        self.assertEqual(post.published_slug, article_data()["slug"])
        self.assertTrue(services.published_posts().filter(pk=post.pk).exists())

        live_snapshot = post.published_snapshot.copy()
        published_at = post.published_at
        self.save(post, title="Unreleased working title", body_html="<p>Private draft wording.</p>")
        self.assertEqual(post.published_snapshot, live_snapshot)
        self.assertEqual(post.published_at, published_at)
        self.assertNotEqual(post.draft["title"], post.published_snapshot["title"])

        self.publish(post)
        self.assertEqual(post.published_snapshot["title"], "Unreleased working title")
        self.assertEqual(post.published_at, published_at)

    def test_stale_save_and_publish_preserve_the_newer_draft(self):
        post = self.post()
        stale_version = post.version
        self.save(post, title="The editor's latest version")
        latest_version = post.version

        with self.assertRaises(services.ConflictError):
            services.save_draft(
                post, article_data(title="An older tab"), actor=self.publisher,
                expected_version=stale_version,
            )
        with self.assertRaises(services.ConflictError):
            services.publish_post(post, actor=self.publisher, expected_version=stale_version)

        post.refresh_from_db()
        self.assertEqual(post.version, latest_version)
        self.assertEqual(post.draft["title"], "The editor's latest version")
        self.assertIsNone(post.published_snapshot)

    def test_publication_validates_required_content(self):
        for fields in ({"title": ""}, {"body_html": "<p><br></p>"}, {"author_name": ""}):
            with self.subTest(fields=fields), self.assertRaises(ValidationError):
                post = self.post(**fields)
                self.publish(post)

    def test_slug_change_preserves_history_and_blocks_other_articles(self):
        first = self.publish(slug="original-follow-up-guide")
        self.save(first, slug="updated-follow-up-guide")
        self.publish(first)
        self.assertEqual(SlugRedirect.objects.get(old_slug="original-follow-up-guide").post_id, first.pk)

        second = self.post(slug="another-article")
        for reserved_slug in ("original-follow-up-guide", "updated-follow-up-guide"):
            with self.subTest(slug=reserved_slug), self.assertRaises(ValidationError):
                self.save(second, slug=reserved_slug)
                self.publish(second)
            second.refresh_from_db()

        first.refresh_from_db()
        self.assertEqual(first.published_slug, "updated-follow-up-guide")
        self.assertEqual(first.published_snapshot["slug"], "updated-follow-up-guide")

    def test_archiving_and_unpublishing_remove_live_visibility(self):
        for status in ("draft", "archived"):
            with self.subTest(status=status):
                post = self.publish(slug=f"lifecycle-{status}")
                services.change_status(post, status, actor=self.publisher, expected_version=post.version)
                post.refresh_from_db()
                self.assertEqual(post.status, status)
                self.assertFalse(services.published_posts().filter(pk=post.pk).exists())

    def test_scheduled_publication_is_due_only_and_idempotent(self):
        post = self.post(slug="scheduled-guide")
        due_at = timezone.now() + timedelta(hours=2)
        services.schedule_post(
            post, actor=self.publisher, expected_version=post.version, scheduled_at=due_at,
        )
        post.refresh_from_db()
        self.assertEqual(post.status, "scheduled")
        self.assertEqual(post.scheduled_snapshot["title"], post.draft["title"])

        with patch("django.utils.timezone.now", return_value=due_at - timedelta(seconds=1)):
            services.publish_due_posts()
        post.refresh_from_db()
        self.assertIsNone(post.published_snapshot)

        with patch("django.utils.timezone.now", return_value=due_at + timedelta(seconds=1)):
            services.publish_due_posts()
            post.refresh_from_db()
            version = post.version
            published_at = post.published_at
            services.publish_due_posts()
        post.refresh_from_db()
        self.assertEqual(post.status, "published")
        self.assertEqual(post.version, version)
        self.assertEqual(post.published_at, published_at)
        self.assertEqual(Post.objects.filter(pk=post.pk).count(), 1)

    def test_scheduled_worker_uses_approved_snapshot_not_mutable_draft(self):
        post = self.post(slug="frozen-schedule")
        due_at = timezone.now() + timedelta(hours=1)
        services.schedule_post(
            post, actor=self.publisher, expected_version=post.version, scheduled_at=due_at,
        )
        post.refresh_from_db()
        approved = post.scheduled_snapshot.copy()
        # Simulate a legacy writer that changes draft JSON without calling the
        # supported service. The worker must still publish only the approved copy.
        Post.objects.filter(pk=post.pk).update(draft={**post.draft, "title": "Unapproved mutation"})
        with patch("django.utils.timezone.now", return_value=due_at + timedelta(seconds=1)):
            services.publish_due_posts()
        post.refresh_from_db()
        self.assertEqual(
            {key: post.published_snapshot[key] for key in approved}, approved,
        )

    def test_editing_a_schedule_cancels_its_approval(self):
        post = self.post(slug="cancel-schedule")
        due_at = timezone.now() + timedelta(hours=1)
        services.schedule_post(
            post, actor=self.publisher, expected_version=post.version, scheduled_at=due_at,
        )
        post.refresh_from_db()
        self.save(post, title="An edit needing another publishing decision")
        with patch("django.utils.timezone.now", return_value=due_at + timedelta(seconds=1)):
            services.publish_due_posts()
        post.refresh_from_db()
        self.assertIsNone(post.published_snapshot)
        self.assertNotEqual(post.status, "scheduled")

    def test_schedule_rechecks_publisher_activation(self):
        post = self.post(slug="revoked-publisher")
        due_at = timezone.now() + timedelta(hours=1)
        services.schedule_post(
            post, actor=self.publisher, expected_version=post.version, scheduled_at=due_at,
        )
        User.objects.filter(pk=self.publisher.pk).update(is_active=False)
        with patch("django.utils.timezone.now", return_value=due_at + timedelta(seconds=1)):
            services.publish_due_posts()
        post.refresh_from_db()
        self.assertIsNone(post.published_snapshot)

    def test_schedule_rechecks_edit_permission_as_well_as_publish_permission(self):
        actor = User.objects.create_user(email="revoked-editor@example.com", role="superadmin")
        edit_permission = Permission.objects.get(content_type__app_label="blog", codename="change_post")
        publish_permission = Permission.objects.get(content_type__app_label="blog", codename="publish_post")
        actor.user_permissions.add(edit_permission, publish_permission)
        post = services.create_post(article_data(slug="revoked-editor-schedule"), actor=actor)
        due_at = timezone.now() + timedelta(hours=1)
        services.schedule_post(post, actor=actor, expected_version=post.version, scheduled_at=due_at)

        actor.user_permissions.remove(edit_permission)
        with patch("django.utils.timezone.now", return_value=due_at + timedelta(seconds=1)):
            self.assertEqual(services.publish_due_posts(), 0)

        post.refresh_from_db()
        self.assertIsNone(post.published_snapshot)
        self.assertEqual(post.status, Post.Status.REVIEW)
        self.assertIsNone(post.scheduled_at)
        self.assertTrue(post.scheduling_error)

    def test_blocked_scheduled_slug_does_not_stop_other_due_articles(self):
        due_at = timezone.now() + timedelta(hours=1)
        blocked = self.post(slug="contested-publication")
        valid = self.post(slug="uncontested-publication")
        for post in (blocked, valid):
            services.schedule_post(
                post, actor=self.publisher, expected_version=post.version, scheduled_at=due_at,
            )
        winner = self.publish(slug="contested-publication", title="Already published article")
        with patch("django.utils.timezone.now", return_value=due_at + timedelta(seconds=1)):
            services.publish_due_posts()
        blocked.refresh_from_db()
        valid.refresh_from_db()
        winner.refresh_from_db()
        self.assertIsNone(blocked.published_snapshot)
        self.assertEqual(blocked.status, "review")
        self.assertTrue(blocked.scheduling_error)
        self.assertEqual(valid.status, "published")
        self.assertEqual(winner.published_snapshot["title"], "Already published article")

    def test_revision_restore_is_draft_only_and_cannot_cross_articles(self):
        post = self.post()
        original = post.revisions.get(version=post.version)
        self.save(post, title="Approved newer headline")
        self.publish(post)
        live_snapshot = post.published_snapshot.copy()
        services.restore_revision(post, original, actor=self.publisher, expected_version=post.version)
        post.refresh_from_db()
        self.assertEqual(post.draft["title"], original.snapshot["title"])
        self.assertEqual(post.published_snapshot, live_snapshot)
        other = self.post(slug="another-revision-owner")
        foreign_revision = other.revisions.get(version=other.version)
        with self.assertRaises(ValidationError):
            services.restore_revision(post, foreign_revision, actor=self.publisher, expected_version=post.version)
        post.refresh_from_db()
        self.assertEqual(post.draft["title"], original.snapshot["title"])


class PublishingAuthorizationTests(BlogTestCase):
    def test_publish_permission_alone_does_not_grant_publication_authority(self):
        actor = User.objects.create_user(email="publish-only@example.com", role="superadmin")
        actor.user_permissions.add(Permission.objects.get(content_type__app_label="blog", codename="publish_post"))
        post = self.post(slug="publish-only-denied")

        with self.assertRaises(PermissionDenied):
            services.publish_post(post, actor=actor, expected_version=post.version)
        with self.assertRaises(PermissionDenied):
            services.schedule_post(post, actor=actor, expected_version=post.version,
                                   scheduled_at=timezone.now() + timedelta(hours=1))
        post.refresh_from_db()
        self.assertIsNone(post.published_snapshot)
        self.assertEqual(post.status, Post.Status.DRAFT)

    def test_tenant_admin_and_tenant_superuser_cannot_modify_company_blog(self):
        organization = Organization.objects.create(name="Customer Tenant")
        for elevated in (False, True):
            user = User.objects.create_user(
                email=f"customer-admin-{elevated}@example.com", organization=organization,
                role="admin", is_staff=True, is_superuser=elevated,
            )
            with self.subTest(elevated=elevated), self.assertRaises(PermissionDenied):
                services.create_post(article_data(), actor=user)
        self.assertFalse(Post.objects.exists())

    def test_anonymous_and_inactive_publisher_cannot_write(self):
        inactive = User.objects.create_superuser(email="inactive-blog@example.com", is_active=False)
        for actor in (AnonymousUser(), inactive):
            with self.subTest(actor=actor), self.assertRaises(PermissionDenied):
                services.create_post(article_data(), actor=actor)
        self.assertFalse(Post.objects.exists())

    def test_editor_permission_does_not_grant_publish_or_schedule(self):
        editor = User.objects.create_user(email="blog-editor@example.com", role="superadmin")
        editor.user_permissions.add(Permission.objects.get(content_type__app_label="blog", codename="change_post"))
        post = services.create_post(article_data(), actor=editor)
        services.save_draft(post, article_data(title="An editor's draft"), actor=editor, expected_version=post.version)
        post.refresh_from_db()

        with self.assertRaises(PermissionDenied):
            services.publish_post(post, actor=editor, expected_version=post.version)
        with self.assertRaises(PermissionDenied):
            services.schedule_post(
                post, actor=editor, expected_version=post.version,
                scheduled_at=timezone.now() + timedelta(days=1),
            )
        post.refresh_from_db()
        self.assertIsNone(post.published_snapshot)
