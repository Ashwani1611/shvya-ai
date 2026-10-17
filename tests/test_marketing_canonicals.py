"""Marketing canonical URLs must ignore tracking parameters and request hosts."""
from pathlib import Path

from bs4 import BeautifulSoup
from django.conf import settings
from django.template import Context, Engine
from django.test import RequestFactory, SimpleTestCase

from apps.core.marketing_context import marketing_context
from apps.core.views import DocumentationView


class MarketingCanonicalTests(SimpleTestCase):
    def setUp(self):
        self.engine = Engine(dirs=[Path(settings.BASE_DIR) / 'templates'], libraries={
            'static': 'django.templatetags.static',
            'public_assets': 'apps.core.templatetags.public_assets',
        })

    def canonical_links(self, template, path, **context):
        request = RequestFactory().get(
            path, data=context.pop('query', {'utm_source': 'campaign'}),
            HTTP_HOST='untrusted.example',
        )
        html = self.engine.get_template(template).render(Context({'request': request, **marketing_context(request), **context}))
        return [node['href'] for node in BeautifulSoup(html, 'html.parser').select('link[rel=canonical]')]

    def test_default_canonical_uses_fixed_origin_and_path(self):
        for path in ('/', '/pricing/', '/services/', '/product-suite/', '/docs/',
                     '/book-a-call/', '/privacy-policy/', '/terms-conditions/',
                     '/cookie-policy/', '/refund-policy/'):
            with self.subTest(path=path):
                self.assertEqual(self.canonical_links('marketing/dark_base.html', path),
                                 ['https://shvya-ai.com' + path])

    def test_features_does_not_duplicate_default_canonical(self):
        self.assertEqual(self.canonical_links('features.html', '/features/'),
                         ['https://shvya-ai.com/features/'])

    def test_blog_retains_its_paginated_canonical(self):
        self.assertEqual(self.canonical_links('blog/public/base.html', '/blog/',
                         seo={'canonical': 'https://shvya-ai.com/blog/?page=2',
                              'title': 'Blog', 'description': 'Sales guides'}),
                         ['https://shvya-ai.com/blog/?page=2'])

    def assert_docs_metadata(self, url, **view_kwargs):
        request = RequestFactory().get(url)
        response = DocumentationView.as_view()(request, **view_kwargs).render()
        soup = BeautifulSoup(response.content, 'html.parser')
        canonical = 'https://shvya-ai.com/docs/ai-automation/ai-brain/'
        self.assertEqual([tag['href'] for tag in soup.select('link[rel=canonical]')], [canonical])
        self.assertEqual(soup.select_one('meta[property="og:url"]')['content'], canonical)
        self.assertIn('max-image-preview:large', soup.select_one('meta[name=robots]')['content'])
        self.assertIsNotNone(soup.select_one('meta[name="twitter:card"]'))
        self.assertIsNotNone(soup.select_one('script[type="application/ld+json"]'))

    def test_legacy_doc_topics_retain_article_canonical_and_shared_metadata(self):
        self.assert_docs_metadata('/docs/?topic=playbooks&utm_source=campaign')

    def test_documentation_article_uses_its_own_canonical(self):
        self.assert_docs_metadata('/docs/ai-automation/ai-brain/',
                                  category='ai-automation', slug='ai-brain')
