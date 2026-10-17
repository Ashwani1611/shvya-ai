"""Marketing canonical URLs must ignore tracking parameters and request hosts."""
from pathlib import Path
from types import SimpleNamespace

from bs4 import BeautifulSoup
from django.conf import settings
from django.template import Context, Engine
from django.test import SimpleTestCase


class MarketingCanonicalTests(SimpleTestCase):
    def setUp(self):
        self.engine = Engine(dirs=[Path(settings.BASE_DIR) / 'templates'], libraries={
            'static': 'django.templatetags.static',
            'public_assets': 'apps.core.templatetags.public_assets',
        })

    def canonical_links(self, template, path, **context):
        request = SimpleNamespace(path=path, META={'HTTP_HOST': 'untrusted.example'},
                                  GET={'utm_source': 'campaign'})
        html = self.engine.get_template(template).render(Context({'request': request, **context}))
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
