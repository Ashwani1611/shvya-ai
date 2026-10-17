from types import SimpleNamespace

from django.contrib.sessions.middleware import SessionMiddleware
from django.test import RequestFactory, SimpleTestCase, override_settings
from django.urls import resolve

from apps.accounts.views_flat import signup_complete_view
from apps.core.marketing_context import marketing_context


@override_settings(GOOGLE_ANALYTICS_ID='G-TEST123', DEBUG=False, ALLOWED_HOSTS=['*'])
class MarketingAnalyticsTests(SimpleTestCase):
    def request(self, path='/', host='shvya-ai.com'):
        request = RequestFactory().get(path, HTTP_HOST=host)
        request.resolver_match = resolve(request.path)
        request.user = SimpleNamespace(is_authenticated=False)
        return request

    def test_public_routes_and_sensitive_query(self):
        for path in ['/', '/features/', '/pricing/', '/book-a-call/?email=private', '/blog/', '/blog/public-article/', '/signup/']:
            with self.subTest(path=path):
                context = marketing_context(self.request(path))
                self.assertEqual(context['marketing_ga_id'], 'G-TEST123')
                self.assertNotIn('private', context['marketing_canonical'])

    def test_private_routes_and_nonproduction_fail_closed(self):
        for path in ['/dashboard/', '/superadmin/', '/signup/verify-email/?token=private']:
            self.assertEqual(marketing_context(self.request(path))['marketing_ga_id'], '')
        self.assertEqual(marketing_context(self.request(host='staging.shvya-ai.com'))['marketing_ga_id'], '')
        with override_settings(DEBUG=True):
            self.assertEqual(marketing_context(self.request())['marketing_ga_id'], '')
        with override_settings(GOOGLE_ANALYTICS_ID='invalid'):
            self.assertEqual(marketing_context(self.request())['marketing_ga_id'], '')

    def test_authenticated_visitors_and_dedicated_session_cookies(self):
        request = self.request()
        request.user.is_authenticated = True
        self.assertEqual(marketing_context(request)['marketing_ga_id'], '')
        for name in ['shvya_crm_sessionid', 'shvya_superadmin_sessionid']:
            request = self.request('/blog/')
            request.COOKIES[name] = 'opaque-session'
            self.assertEqual(marketing_context(request)['marketing_ga_id'], '')

    def test_signup_confirmation_requires_one_time_server_receipt(self):
        request = self.request('/signup/complete/')
        SessionMiddleware(lambda r: None).process_request(request)
        self.assertEqual(signup_complete_view(request).status_code, 302)
        request.session['marketing_signup_receipt'] = 'random-receipt'
        response = signup_complete_view(request)
        self.assertContains(response, 'data-analytics-signup="random-receipt"')
        self.assertEqual(response['Cache-Control'], 'private, no-store')
        self.assertEqual(signup_complete_view(request).status_code, 302)
