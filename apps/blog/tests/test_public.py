import json
from xml.etree import ElementTree

from bs4 import BeautifulSoup
from django.test import Client
from django.urls import reverse

from apps.blog import services
from apps.blog.tests.helpers import BlogTestCase
from apps.core.docs_portal import DOC_ORDER
from unittest.mock import patch


class PublicBlogTests(BlogTestCase):
    def article_url(self, slug):
        return reverse("blog:detail", kwargs={"slug": slug})

    def test_draft_is_absent_from_detail_search_categories_and_sitemap(self):
        self.post(
            title="Unannounced confidential initiative", slug="unannounced-confidential-initiative",
            category={"name": "Private Category", "slug": "private-category"},
        )
        self.assertEqual(self.client.get(self.article_url("unannounced-confidential-initiative")).status_code, 404)
        self.assertEqual(self.client.get(reverse("blog:category", kwargs={"slug": "private-category"})).status_code, 404)
        for url in (reverse("blog:index"), f"{reverse('blog:index')}?q=confidential", reverse("website-sitemap")):
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                self.assertNotContains(response, "Unannounced confidential initiative")
                self.assertNotContains(response, "private-category")
                self.assertNotContains(response, "unannounced-confidential-initiative")

    def test_live_pages_search_metadata_and_categories_ignore_draft_edits(self):
        post = self.publish()
        live_slug = post.published_slug
        live_title = post.published_snapshot["title"]
        live_seo_title = post.published_snapshot["seo_title"]
        self.save(
            post, title="Secret unreleased title", slug="secret-unreleased-url",
            body_html="<p>Secret unreleased product roadmap</p>",
            category={"name": "Confidential Category", "slug": "confidential-category"},
            seo_title="Secret unreleased metadata", meta_description="Secret unreleased description",
        )
        article = self.client.get(self.article_url(live_slug))
        self.assertContains(article, live_title)
        self.assertContains(article, live_seo_title)
        self.assertNotContains(article, "Secret unreleased")
        self.assertNotContains(article, "Confidential Category")
        listing = self.client.get(reverse("blog:index"))
        self.assertContains(listing, live_title)
        self.assertNotContains(listing, "Secret unreleased")
        self.assertNotContains(listing, "confidential-category")
        search = self.client.get(reverse("blog:index"), {"q": "roadmap"})
        self.assertEqual(search.status_code, 200)
        self.assertEqual(search.context["page_obj"].paginator.count, 0)
        self.assertEqual(self.client.get(self.article_url("secret-unreleased-url")).status_code, 404)
        sitemap = self.client.get(reverse("website-sitemap"))
        self.assertContains(sitemap, self.article_url(live_slug))
        self.assertNotContains(sitemap, "secret-unreleased-url")

    def test_slug_history_redirects_to_current_url_and_stops_when_unpublished(self):
        post = self.publish(slug="first-guide-url")
        self.save(post, slug="second-guide-url")
        self.publish(post)
        self.save(post, slug="final-guide-url")
        self.publish(post)
        for old_slug in ("first-guide-url", "second-guide-url"):
            response = self.client.get(self.article_url(old_slug))
            self.assertEqual(response.status_code, 301)
            self.assertEqual(response["Location"], self.article_url("final-guide-url"))
        services.change_status(post, "draft", actor=self.publisher, expected_version=post.version)
        for slug in ("first-guide-url", "second-guide-url", "final-guide-url"):
            self.assertEqual(self.client.get(self.article_url(slug)).status_code, 404)

    def test_unpublish_and_archive_remove_article_sitemap_and_images(self):
        for status in ("draft", "archived"):
            with self.subTest(status=status):
                image = self.upload()
                post = self.publish(slug=f"hide-{status}", cover_image=str(image.pk))
                response = self.client.get(image.public_url)
                self.assertEqual(response.status_code, 200)
                self.assertTrue(b"".join(response.streaming_content))
                services.change_status(post, status, actor=self.publisher, expected_version=post.version)
                self.assertEqual(self.client.get(self.article_url(f"hide-{status}")).status_code, 404)
                self.assertEqual(self.client.get(image.public_url).status_code, 404)
                sitemap = self.client.get(reverse("website-sitemap"))
                self.assertNotContains(sitemap, self.article_url(f"hide-{status}"))

    def test_images_become_public_only_while_a_live_article_references_them(self):
        first = self.upload()
        replacement = self.upload()
        self.assertEqual(self.client.get(first.public_url).status_code, 404)
        post = self.publish(cover_image=str(first.pk))
        self.save(post, cover_image=str(replacement.pk))
        response = self.client.get(first.public_url)
        self.assertEqual(response.status_code, 200)
        self.assertIn("must-revalidate", response["Cache-Control"])
        self.assertTrue(b"".join(response.streaming_content))
        self.assertEqual(self.client.get(replacement.public_url).status_code, 404)
        self.publish(post)
        self.assertEqual(self.client.get(first.public_url).status_code, 404)
        response = self.client.get(replacement.public_url)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(b"".join(response.streaming_content))

    def test_shared_image_stays_live_until_last_article_is_unpublished(self):
        image = self.upload()
        first = self.publish(slug="first-shared-image", cover_image=str(image.pk))
        second = self.publish(slug="second-shared-image", cover_image=str(image.pk))
        services.change_status(first, "draft", actor=self.publisher, expected_version=first.version)
        response = self.client.get(image.public_url)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(b"".join(response.streaming_content))
        services.change_status(second, "draft", actor=self.publisher, expected_version=second.version)
        self.assertEqual(self.client.get(image.public_url).status_code, 404)

    def test_pagination_has_self_canonical_and_search_filters_are_noindex(self):
        for index in range(11):
            self.publish(slug=f"guide-{index}", title=f"Sales guide {index}")
        page = self.client.get(reverse("blog:index"), {"page": 2})
        self.assertEqual(page.status_code, 200)
        self.assertEqual(page.context["seo"]["canonical"], "https://shvya-ai.com/blog/?page=2")
        self.assertEqual(page.context["seo"]["robots"], "index, follow")
        collection = json.loads(page.context["seo"]["schema"])["@graph"][0]
        self.assertEqual(collection["url"], page.context["seo"]["canonical"])
        self.assertEqual(collection["@type"], "CollectionPage")
        self.assertEqual(len(page.context["posts"]), 2)
        self.assertContains(page, 'rel="canonical"')
        for filters in ({"q": "Sales"}, {"utm_source": "test"}, {"q": ""}):
            with self.subTest(filters=filters):
                response = self.client.get(reverse("blog:index"), filters)
                self.assertEqual(response.status_code, 200)
                self.assertIn("noindex", response.context["seo"]["robots"])
                self.assertIn("noindex", response["X-Robots-Tag"])
                self.assertEqual(response.context["seo"]["schema"], "")
        self.assertEqual(self.client.get(reverse("blog:index"), {"page": 100000}).status_code, 404)

    def test_metadata_and_schema_escape_untrusted_editorial_text(self):
        dangerous = '</script><script id="injected-script">alert(1)</script>'
        post = self.publish(title=f"Article {dangerous}", seo_title=f"SEO {dangerous}", meta_description=f"Description {dangerous}")
        response = self.client.get(self.article_url(post.published_slug))
        self.assertEqual(response.status_code, 200)
        soup = BeautifulSoup(response.content, "html.parser")
        self.assertIsNone(soup.find("script", id="injected-script"))
        schema_nodes = soup.find_all("script", type="application/ld+json")
        self.assertTrue(schema_nodes)
        schemas = [json.loads(node.string) for node in schema_nodes]
        article_schema = next(
            item for schema in schemas for item in schema.get("@graph", [schema])
            if item.get("@type") == "BlogPosting"
        )
        self.assertEqual(article_schema["headline"], post.published_snapshot["title"])
        self.assertEqual(article_schema["mainEntityOfPage"]["@id"], f"https://shvya-ai.com{self.article_url(post.published_slug)}")
        self.assertNotIn("</script>", response.context["seo"]["schema"])

    def test_canonical_does_not_use_request_host_and_noindex_articles_leave_sitemap(self):
        public = self.publish(slug="indexed-guide")
        private_search = self.publish(slug="noindex-guide", noindex=True)
        response = self.client.get(self.article_url(public.published_slug), HTTP_HOST="localhost")
        self.assertEqual(response.context["seo"]["canonical"], "https://shvya-ai.com/blog/indexed-guide/")
        noindex = self.client.get(self.article_url(private_search.published_slug))
        self.assertIn("noindex", noindex["X-Robots-Tag"])
        sitemap = self.client.get(reverse("website-sitemap"))
        root = ElementTree.fromstring(sitemap.content)
        locations = {node.text for node in root.findall("{*}url/{*}loc")}
        self.assertIn("https://shvya-ai.com/blog/indexed-guide/", locations)
        self.assertNotIn("https://shvya-ai.com/blog/noindex-guide/", locations)

    def test_public_routes_are_read_only(self):
        post = self.publish()
        for url in (reverse("blog:index"), self.article_url(post.published_slug), reverse("website-sitemap"), reverse("website-robots")):
            with self.subTest(url=url):
                self.assertEqual(Client().post(url, {}).status_code, 405)

    def test_sitemap_covers_public_docs_categories_and_all_article_pages(self):
        first = self.publish(slug="first-sitemap-guide")
        second = self.publish(slug="second-sitemap-guide")
        with patch("apps.blog.public.SITEMAP_PAGE_SIZE", 1):
            index = ElementTree.fromstring(self.client.get(reverse("website-sitemap")).content)
            maps = [node.text for node in index.findall("{*}sitemap/{*}loc")]
            self.assertEqual(len(maps), 2)
            locations = []
            for number in (1, 2):
                response = self.client.get(reverse("website-sitemap"), {"page": number})
                root = ElementTree.fromstring(response.content)
                locations.extend(node.text for node in root.findall("{*}url/{*}loc"))
            self.assertEqual(len(locations), len(set(locations)))
            for post in (first, second):
                self.assertIn(f"https://shvya-ai.com{post.get_absolute_url()}", locations)
            for article in DOC_ORDER:
                self.assertIn(f"https://shvya-ai.com{article['url']}", locations)
            self.assertIn("https://shvya-ai.com/blog/category/crm-sales/", locations)
            self.assertNotIn("https://shvya-ai.com/privacy-policy/", locations)
            self.assertNotIn("https://shvya-ai.com/terms-conditions/", locations)
            self.assertEqual(self.client.get(reverse("website-sitemap"), {"page": 3}).status_code, 404)

    def test_robots_preserves_public_crawling_and_requested_exclusions(self):
        response = self.client.get(reverse("website-robots"))
        self.assertContains(response, "Sitemap: https://shvya-ai.com/sitemap.xml")
        for path in ("/contact-us", "/privacy-policy", "/terms-conditions", "/faq", "/*?search", "/*&search", "/superadmin/"):
            self.assertContains(response, f"Disallow: {path}\n")
        self.assertNotContains(response, "Disallow: /blog/")
        self.assertNotContains(response, "Disallow: /docs/")
        self.assertNotContains(response, "tecknowacademy")
        self.assertEqual(response["Cache-Control"], "public, max-age=0, must-revalidate")
