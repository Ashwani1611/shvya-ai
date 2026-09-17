from unittest.mock import patch

from django.test import RequestFactory, SimpleTestCase
from django.urls import resolve, reverse

from apps.core.views import FeaturesView


class FeaturesPageTests(SimpleTestCase):
    def test_public_features_route_renders_without_login(self):
        self.assertIs(resolve('/features/').func.view_class, FeaturesView)
        request = RequestFactory().get(reverse('features'))
        with patch('apps.core.views.get_crm_authenticated_user', return_value=None):
            response = FeaturesView.as_view()(request)
            response.render()
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'AI playbooks')
        self.assertContains(response, 'Sales cadence')
        self.assertContains(response, 'id="sales-desk"')
        self.assertContains(response, 'id="workflows"')
        self.assertContains(response, '/static/marketing/dark/logo.png')
        self.assertContains(response, '/static/marketing/dark/wordmark.png')
        self.assertContains(response, '/static/marketing/dark/site.css')
        self.assertContains(response, 'marketing/premium-features.css')
        self.assertContains(response, 'marketing/premium-features.js')
        self.assertContains(response, 'shvya-mascot-body.png')
        self.assertContains(response, 'shvya-cinematic-film.mp4')
        self.assertContains(response, 'aria-label="Explore Shvya features"')
        self.assertContains(response, 'aria-label="Customer journey"')
        self.assertContains(response, 'Pause motion')
        self.assertContains(response, 'Documentation')
        self.assertContains(response, 'SHVYA ecosystem')
        self.assertContains(response, 'href="/book-a-call/"')
        self.assertContains(response, 'href="/features/"')
        self.assertNotContains(response, 'href="/#features"')

    def test_features_header_preserves_signed_in_navigation(self):
        request = RequestFactory().get(reverse('features'))
        with patch('apps.core.views.get_crm_authenticated_user', return_value={'name': 'Demo User'}):
            response = FeaturesView.as_view()(request)
            response.render()
        self.assertContains(response, 'Demo User')
        self.assertContains(response, reverse('crm-logout'))
        self.assertNotContains(response, 'class="nav-login"')
