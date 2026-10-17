"""Public SEO reflects the page content and preserves indexing boundaries."""
import json

from bs4 import BeautifulSoup
from django.template.loader import render_to_string
from django.test import RequestFactory, SimpleTestCase

from apps.blog.seo import listing_schema
from apps.core.docs_portal import DOC_ORDER
from apps.core.marketing_context import DESCRIPTIONS, marketing_context, public_page_metadata
from apps.core.views import DocumentationView


class PublicSeoMetadataTests(SimpleTestCase):
    def test_documentation_uses_unique_curated_summaries_and_canonical_breadcrumbs(self):
        descriptions = set()
        for page in DOC_ORDER:
            with self.subTest(page=page['url']):
                request = RequestFactory().get(page['url'] + '?utm_source=campaign')
                category, slug = page['url'].strip('/').split('/')[1:]
                response = DocumentationView.as_view()(request, category=category, slug=slug).render()
                soup = BeautifulSoup(response.content, 'html.parser')
                description = soup.select_one('meta[name="description"]')['content']
                self.assertEqual(description, page['summary'])
                self.assertNotIn(description, descriptions)
                descriptions.add(description)
                self.assertEqual(soup.select_one('meta[property="og:description"]')['content'], description)
                self.assertIn(page['title'], soup.select_one('meta[property="og:title"]')['content'])
                graph = json.loads(soup.select_one('script[type="application/ld+json"]').string)['@graph']
                breadcrumb = next(node for node in graph if node['@type'] == 'BreadcrumbList')
                self.assertEqual(breadcrumb['itemListElement'][-1]['item'], 'https://shvya-ai.com' + page['url'])

    def test_documentation_search_is_noindex_but_article_queries_remain_indexable(self):
        for url, expected in [('/docs/?q=whatsapp', 'noindex'), ('/docs/?topic=playbooks&q=whatsapp', 'index')]:
            request = RequestFactory().get(url)
            response = DocumentationView.as_view()(request).render()
            soup = BeautifulSoup(response.content, 'html.parser')
            self.assertEqual(soup.select_one('meta[name="robots"]')['content'].split(',')[0], expected)

    def test_public_descriptions_are_distinct_and_schema_cannot_close_script(self):
        self.assertEqual(len(DESCRIPTIONS), len(set(DESCRIPTIONS.values())))
        title = 'A </script><script>alert(1)</script> & title'
        context = public_page_metadata('/docs/example/', title=title, description='Public summary')
        self.assertNotIn('</script>', context['marketing_schema'])
        graph = json.loads(context['marketing_schema'])['@graph']
        self.assertEqual(graph[-1]['name'], title)

    def test_social_fallback_preserves_editorial_image_and_preview_noindex(self):
        request = RequestFactory().get('/blog/example/')
        context = marketing_context(request)
        for image in ('', 'https://shvya-ai.com/blog/images/editorial/'):
            html = render_to_string('blog/public/base.html', {
                **context, 'preview': True,
                'seo': {'title': 'Preview', 'description': 'Draft', 'canonical': 'https://shvya-ai.com/blog/example/',
                        'robots': 'noindex, nofollow', 'image': image},
            })
            soup = BeautifulSoup(html, 'html.parser')
            self.assertEqual(soup.select_one('meta[property="og:image"]')['content'], image or context['marketing_image'])
            self.assertEqual(soup.select_one('meta[name="robots"]')['content'], 'noindex, nofollow')
            self.assertFalse(soup.select('script[type="application/ld+json"]'))

    def test_collection_schema_keeps_page_canonical_and_escapes_category_names(self):
        canonical = 'https://shvya-ai.com/blog/category/crm/?page=2'
        data = listing_schema('CRM | Page 2', 'CRM guides', canonical, 'CRM </script>')
        self.assertNotIn('</script>', data)
        graph = json.loads(data)['@graph']
        self.assertEqual(graph[0]['url'], canonical)
        self.assertEqual(graph[0]['@type'], 'CollectionPage')
        self.assertEqual(graph[1]['itemListElement'][-1]['item'], canonical.split('?')[0])
