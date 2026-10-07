"""Tests for the public documentation portal and its independently linkable guides."""
import re
from unittest.mock import patch

from django.http import Http404
from django.test import RequestFactory, SimpleTestCase
from django.urls import resolve

from apps.core.docs_portal import (
    DOC_GROUPS,
    DOC_LOOKUP,
    DOC_ORDER,
    LEGACY_TOPICS,
    docs_context,
    render_article,
)


class DocumentationCatalogueTests(SimpleTestCase):
    def test_every_category_has_multiple_unique_articles(self):
        self.assertEqual(len(DOC_GROUPS), 8)
        self.assertGreaterEqual(len(DOC_ORDER), 60)
        self.assertEqual(len(DOC_LOOKUP), len(DOC_ORDER))
        for group in DOC_GROUPS:
            self.assertGreaterEqual(len(group["pages"]), 5)
            for page in group["pages"]:
                self.assertEqual(page["group_slug"], group["slug"])
                self.assertTrue(page["title"])
                self.assertTrue(page["summary"])
                self.assertTrue(page["body"].strip())
                self.assertEqual(page["url"], f'/docs/{group["slug"]}/{page["slug"]}/')

    def test_all_documentation_internal_article_links_resolve(self):
        article_link = re.compile(r"\]\(/docs/([a-z0-9-]+)/([a-z0-9-]+)/\)")
        broken = []
        for page in DOC_ORDER:
            for category, slug in article_link.findall(page["body"]):
                if (category, slug) not in DOC_LOOKUP:
                    broken.append((page["url"], category, slug))
        self.assertEqual(broken, [])

    def test_legacy_links_still_have_targets(self):
        for topic, target in LEGACY_TOPICS.items():
            with self.subTest(topic=topic):
                self.assertIn(target, DOC_LOOKUP)
                self.assertEqual(docs_context(legacy_topic=topic)["docs_page"]["slug"], target[1])

    def test_restricted_markdown_escapes_raw_html_and_unsafe_links(self):
        value, toc = render_article(
            "## Heading\n"
            "<script>alert('x')</script> [bad](javascript:alert(1))\n"
            "[safe](/docs/getting-started/quickstart/)\n"
            "1. **Step one**"
        )
        self.assertIn("&lt;script&gt;", str(value))
        self.assertNotIn("<script>", str(value))
        self.assertNotIn('href="javascript:', str(value))
        self.assertIn('href="/docs/getting-started/quickstart/"', str(value))
        self.assertIn("<strong>Step one</strong>", str(value))
        self.assertEqual(toc[0]["id"], "heading")

    def test_search_indexes_body_not_just_titles(self):
        results = docs_context(query="qr")
        self.assertTrue(results["docs_search"])
        self.assertIn(DOC_LOOKUP[("messaging-channels", "whatsapp-hosted")], results["docs_results"])
        self.assertEqual(docs_context(query="notarealshvyasearchtoken")["docs_results"], [])

    def test_unknown_page_is_rejected(self):
        with self.assertRaises(Http404):
            docs_context(category="unknown", slug="missing")


class DocumentationPublicRouteTests(SimpleTestCase):
    def render_docs(self, path):
        request = RequestFactory().get(path)
        request.session = {}
        with patch("apps.core.views.get_crm_authenticated_user", return_value=None):
            match = resolve(path.split("?", 1)[0])
            response = match.func(request, *match.args, **match.kwargs)
            response.render()
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def test_home_has_search_categories_and_quickstart(self):
        html = self.render_docs("/docs/")
        self.assertIn("Everything you need to", html)
        self.assertIn("Search guides, features", html)
        self.assertIn("/docs/getting-started/quickstart/", html)
        self.assertIn("Connect Hub &amp; Integrations", html)
        self.assertIn("data-marketing-theme-toggle", html)

    def test_articles_are_independently_shareable(self):
        html = self.render_docs("/docs/operations/support-tickets/")
        self.assertIn("Help &amp; Support tickets", html)
        self.assertIn("ON THIS PAGE", html)
        self.assertIn("kb-article-body", html)
        self.assertIn("/dashboard/support-portal/", html)
        self.assertIn("marketing/docs-portal.css", html)

    def test_legacy_topic_urls_work(self):
        html = self.render_docs("/docs/?topic=integrations")
        self.assertIn("Connect Hub overview", html)

    def test_search_results_and_empty_state(self):
        html = self.render_docs("/docs/?q=JustDial")
        self.assertIn("Results for", html)
        self.assertIn("JustDial lead integration", html)
        empty = self.render_docs("/docs/?q=absentnonexistentguidekeyword")
        self.assertIn("Nothing matched that search", empty)

    def test_unavailable_article_returns_404(self):
        request = RequestFactory().get("/docs/invalid/no-article/")
        request.session = {}
        with patch("apps.core.views.get_crm_authenticated_user", return_value=None):
            with self.assertRaises(Http404):
                match = resolve("/docs/invalid/no-article/")
                match.func(request, *match.args, **match.kwargs)
