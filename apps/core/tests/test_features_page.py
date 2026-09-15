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
        self.assertContains(response, 'AI PLAYBOOK')
        self.assertContains(response, 'CADENCE')
        self.assertContains(response, 'id="sales-desk"')
        self.assertContains(response, 'id="workflows"')
        self.assertContains(response, 'shvya-brand-2026.png')
        self.assertContains(response, 'shvya-mascot-body.png')
        self.assertNotContains(response, 'shvya-logo.svg')
        self.assertContains(response, 'shvya-features-film.mp4')
        self.assertContains(response, 'shvya-features-film.vtt')
        self.assertContains(response, 'href="/features/"', count=2)
        self.assertNotContains(response, 'href="/#features"')

    def test_features_header_preserves_signed_in_navigation(self):
        request = RequestFactory().get(reverse('features'))
        with patch('apps.core.views.get_crm_authenticated_user', return_value={'name': 'Demo User'}):
            response = FeaturesView.as_view()(request)
            response.render()
        self.assertContains(response, 'Demo User')
        self.assertContains(response, reverse('crm-logout'))
        self.assertNotContains(response, 'class="nav-login"')
