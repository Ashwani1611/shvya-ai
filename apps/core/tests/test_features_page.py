from unittest.mock import patch

from django.test import RequestFactory, SimpleTestCase, override_settings
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


    def test_workspace_tour_covers_current_capabilities(self):
        request = RequestFactory().get(reverse('features'))
        with patch('apps.core.views.get_crm_authenticated_user', return_value=None):
            response = FeaturesView.as_view()(request)
            response.render()

        expected = (
            'crm', 'sales', 'calendar', 'calls', 'documents', 'playbooks',
            'engagement', 'cadence', 'workflows', 'insights', 'whatsapp',
            'instagram', 'connect', 'vault', 'teams', 'support',
        )
        self.assertContains(response, '16 CAPABILITIES')
        self.assertContains(response, 'p-feature-menu')
        self.assertContains(response, 'id="panel-guide"')
        self.assertEqual(
            response.content.decode('utf-8').count('role="tab" id="tab-'),
            len(expected),
        )
        for key in expected:
            self.assertContains(response, f'id="tab-{key}"')
            self.assertContains(response, f'data-feature="{key}"')
        self.assertContains(response, 'SHVYA Calendar')
        self.assertContains(response, 'Call Intelligence')
        self.assertContains(response, 'SHVYA Sales')
        self.assertContains(response, 'SHVYA Vault')
        self.assertContains(response, 'AI Engagement')
        self.assertContains(response, 'Touchpoints with CRM attribute placeholders')

    def test_workspace_tour_metadata_matches_visible_tabs(self):
        from pathlib import Path
        source = (
            Path(__file__).resolve().parents[3]
            / 'static'
            / 'marketing'
            / 'premium-features.js'
        ).read_text(encoding='utf-8')
        for key in (
            'crm', 'sales', 'calendar', 'calls', 'documents', 'playbooks',
            'engagement', 'cadence', 'workflows', 'insights', 'whatsapp',
            'instagram', 'connect', 'vault', 'teams', 'support',
        ):
            self.assertIn(f'"{key}": [', source)
        for url in (
            '/docs/sales-calendar/calendar/',
            '/docs/operations/call-intelligence/',
            '/docs/sales-calendar/sales-overview/',
            '/docs/operations/shvya-vault/',
            '/docs/operations/support-tickets/',
        ):
            self.assertIn(url, source)
        self.assertNotIn('Touchpoints is an upcoming feature', source)
        self.assertNotIn('Call Scheduler and Call Tracker are upcoming', source)

    @override_settings(
        PUBLIC_ASSET_BASE_URL="https://assets.example.com/production/media/public-assets"
    )
    def test_features_page_uses_external_base_for_heavy_assets(self):
        request = RequestFactory().get(reverse('features'))
        with patch('apps.core.views.get_crm_authenticated_user', return_value=None):
            response = FeaturesView.as_view()(request)
            response.render()

        self.assertContains(
            response,
            'https://assets.example.com/production/media/public-assets/marketing/shvya-cinematic-film.mp4',
        )
        self.assertContains(
            response,
            'https://assets.example.com/production/media/public-assets/marketing/shvya-cinematic-poster.jpg',
        )
        self.assertContains(
            response,
            'https://assets.example.com/production/media/public-assets/images/shvya-mascot-body.png',
        )
        self.assertContains(
            response,
            'https://assets.example.com/production/media/public-assets/marketing/dark/wordmark.png',
        )
        self.assertNotContains(response, '/static/marketing/shvya-cinematic-film.mp4')
        self.assertContains(response, '/static/marketing/premium-features.css')

    def test_features_header_preserves_signed_in_navigation(self):
        request = RequestFactory().get(reverse('features'))
        with patch('apps.core.views.get_crm_authenticated_user', return_value={'name': 'Demo User'}):
            response = FeaturesView.as_view()(request)
            response.render()
        self.assertContains(response, 'Demo User')
        self.assertContains(response, reverse('crm-logout'))
        self.assertNotContains(response, 'class="nav-login"')
