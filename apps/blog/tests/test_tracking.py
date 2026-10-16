from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from bs4 import BeautifulSoup
from django.test import Client
from django.urls import reverse

from apps.blog import services, tracking
from apps.blog.models import ArticleView
from apps.blog.tests.helpers import BlogTestCase, authenticate_area


class ArticleViewTests(BlogTestCase):
    def setUp(self):
        super().setUp()
        self.article = self.publish()
        self.browser = Client(enforce_csrf_checks=True, HTTP_USER_AGENT="Mozilla/5.0 Chrome/130 Safari/537.36")
        self.endpoint = reverse("blog:record_view", kwargs={"post_id": self.article.pk})

    def landing(self, browser=None):
        browser = browser or self.browser
        response = browser.get(self.article.get_absolute_url())
        self.assertEqual(response.status_code, 200)
        counter = BeautifulSoup(response.content, "html.parser").select_one("#blog-view-count")
        return response, counter

    def record(self, browser=None, counter=None):
        browser = browser or self.browser
        if counter is None:
            _, counter = self.landing(browser)
        return browser.post(self.endpoint, {"token": counter["data-token"]}, HTTP_X_CSRFTOKEN=counter["data-csrf"])

    def test_landing_and_head_do_not_count_until_visible_browser_posts(self):
        response, counter = self.landing()
        self.browser.head(self.article.get_absolute_url())
        self.article.refresh_from_db()
        self.assertEqual(self.article.view_count, 0)
        self.assertEqual(counter.text, "0")
        self.assertIn("private, no-store", response["Cache-Control"])
        cookie = response.cookies[tracking.COOKIE]
        self.assertTrue(cookie["httponly"])
        self.assertEqual(cookie["path"], "/blog/")
        self.assertEqual(cookie["samesite"], "Lax")
        result = self.record(counter=counter)
        self.assertEqual(result.json(), {"views": 1, "counted": True})
        self.assertIn("no-store", result["Cache-Control"])

    def test_cookie_expires_at_ist_midnight_and_is_secure_on_https(self):
        # 18:29:30 UTC is 23:59:30 IST; identity rotates at the day boundary.
        with patch("apps.blog.tracking.timezone.now", return_value=datetime(2026, 10, 16, 18, 29, 30, tzinfo=timezone.utc)):
            self.assertEqual(tracking.cookie_lifetime(), 30)
        response = self.browser.get(self.article.get_absolute_url(), secure=True)
        self.assertTrue(response.cookies[tracking.COOKIE]["secure"])
        self.assertLessEqual(int(response.cookies[tracking.COOKIE]["max-age"]), 86400)

    def test_refresh_retry_and_multiple_tabs_count_once(self):
        _, first = self.landing()
        _, second = self.landing()
        for counter in (first, second, first):
            self.assertEqual(self.record(counter=counter).json()["views"], 1)
        self.assertEqual(ArticleView.objects.count(), 1)
        _, rendered = self.landing()
        self.assertEqual(rendered.text, "1")

    def test_another_browser_and_next_day_count_separately(self):
        self.record()
        other = Client(enforce_csrf_checks=True, HTTP_USER_AGENT="Mozilla/5.0 Firefox/132")
        self.assertEqual(self.record(browser=other).json()["views"], 2)
        tomorrow = tracking.tracking_day() + timedelta(days=1)
        with patch("apps.blog.tracking.tracking_day", return_value=tomorrow):
            self.assertEqual(self.record().json()["views"], 3)
        self.assertEqual(ArticleView.objects.count(), 3)

    def test_articles_have_independent_counts_and_tokens(self):
        _, first = self.landing()
        second = self.publish(slug="second-article")
        second_url = reverse("blog:record_view", kwargs={"post_id": second.pk})
        result = self.browser.post(second_url, {"token": first["data-token"]}, HTTP_X_CSRFTOKEN=first["data-csrf"])
        self.assertEqual(result.status_code, 400)
        self.record(counter=first)
        second.refresh_from_db()
        self.assertEqual(second.view_count, 0)

    def test_csrf_and_cross_origin_requests_rejected(self):
        _, counter = self.landing()
        self.assertEqual(self.browser.post(self.endpoint, {"token": counter["data-token"]}).status_code, 403)
        result = self.browser.post(self.endpoint, {"token": counter["data-token"]},
                                   HTTP_X_CSRFTOKEN=counter["data-csrf"], HTTP_ORIGIN="https://unrelated.example")
        self.assertEqual(result.status_code, 403)
        self.assertFalse(ArticleView.objects.exists())

    def test_missing_tampered_expired_token_or_cookie_rejected(self):
        _, counter = self.landing()
        for token in ("", "bad-signature"):
            response = self.browser.post(self.endpoint, {"token": token}, HTTP_X_CSRFTOKEN=counter["data-csrf"])
            self.assertEqual(response.status_code, 400)
        with patch("django.core.signing.time.time", return_value=99999999999):
            self.assertEqual(self.record(counter=counter).status_code, 400)
        del self.browser.cookies[tracking.COOKIE]
        self.assertEqual(self.record(counter=counter).status_code, 400)
        self.assertFalse(ArticleView.objects.exists())

    def test_bots_and_privacy_signals_do_not_get_tracking(self):
        for headers in ({"HTTP_USER_AGENT": "Googlebot"}, {"HTTP_DNT": "1"}, {"HTTP_SEC_GPC": "1"}):
            browser = Client(HTTP_USER_AGENT="Mozilla/5.0")
            response = browser.get(self.article.get_absolute_url(), **headers)
            self.assertNotIn(tracking.COOKIE, response.cookies)
            self.assertNotContains(response, 'data-token=')
            self.assertContains(response, 'id="blog-view-count"')
        _, counter = self.landing()
        result = self.browser.post(self.endpoint, {"token": counter["data-token"]},
                                   HTTP_X_CSRFTOKEN=counter["data-csrf"], HTTP_DNT="1")
        self.assertEqual(result.json(), {"views": 0, "counted": False})

    def test_drafts_unpublished_previews_and_get_endpoint_never_count(self):
        draft = self.post(slug="draft-only")
        draft_url = reverse("blog:record_view", kwargs={"post_id": draft.pk})
        self.assertEqual(self.client.post(draft_url).status_code, 404)
        self.assertEqual(self.browser.get(self.endpoint).status_code, 405)
        authenticate_area(self.client, self.publisher)
        preview = self.client.get(reverse("blog_admin:preview", kwargs={"post_id": self.article.pk}))
        self.assertNotContains(preview, 'id="blog-view-count"')
        _, counter = self.landing()
        services.change_status(self.article, "draft", actor=self.publisher, expected_version=self.article.version)
        self.assertEqual(self.record(counter=counter).status_code, 404)
        self.assertFalse(ArticleView.objects.exists())

    def test_slug_edits_republication_and_pruning_preserve_total_and_revision(self):
        old_version = self.article.version
        old_updated = self.article.updated_at
        self.record()
        self.article.refresh_from_db()
        self.assertEqual(self.article.version, old_version)
        self.assertEqual(self.article.updated_at, old_updated)
        self.save(self.article, slug="new-slug")
        self.publish(self.article)
        self.assertEqual(self.record().json()["views"], 1)
        ArticleView.objects.update(day=tracking.tracking_day() - timedelta(days=9))
        self.assertEqual(tracking.prune_view_receipts(), 1)
        self.article.refresh_from_db()
        self.assertEqual(self.article.view_count, 1)
        authenticate_area(self.client, self.publisher)
        manager = self.client.get(reverse("blog_admin:index"))
        self.assertContains(manager, '<th scope="col">Views</th>', html=True)
        self.assertContains(manager, 'title="Daily browser views since tracking began">1</td>')

    def test_rate_limit_stops_automated_increments(self):
        _, counter = self.landing()
        for _ in range(120):
            self.assertEqual(self.record(counter=counter).status_code, 200)
        self.assertEqual(self.record(counter=counter).status_code, 429)
        self.article.refresh_from_db()
        self.assertEqual(self.article.view_count, 1)
