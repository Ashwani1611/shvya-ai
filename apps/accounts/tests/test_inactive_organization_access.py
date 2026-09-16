from asgiref.sync import async_to_sync
from django.contrib.auth import SESSION_KEY
from django.contrib.sessions.backends.db import SessionStore
from django.http import HttpResponse
from django.test import RequestFactory, TransactionTestCase
from rest_framework.exceptions import AuthenticationFailed
from rest_framework.test import APIRequestFactory

from apps.accounts.channels_middleware import _get_crm_user
from apps.accounts.middleware import SHVYAAreaAuthenticationMiddleware
from apps.accounts.models import User
from apps.accounts.session_utils import (
    get_session_cookie_name,
    set_authenticated_user,
)
from apps.crm.authentication import (
    SHVYAAPIKeyAuthentication,
    get_crm_authenticated_user,
)
from apps.organizations.models import APIKey, Organization


class InactiveOrganizationAccessTests(TransactionTestCase):
    def setUp(self):
        self.organization = Organization.objects.create(
            name="Inactive access test org"
        )
        self.user = User.objects.create_user(
            email="inactive-access@example.com",
            password="test-password",
            name="Inactive Access Admin",
            organization=self.organization,
            role=User.Role.ADMIN,
        )
        self.api_key, self.raw_api_key = APIKey.issue(
            organization=self.organization,
            name="Inactive access key",
        )
        self.request_factory = RequestFactory()
        self.api_request_factory = APIRequestFactory()

    def _create_crm_session(self):
        session = SessionStore()
        set_authenticated_user(session, self.user)
        session.save()
        return session.session_key

    def _dashboard_request(self, session_key):
        request = self.request_factory.get("/dashboard/")
        request.COOKIES[get_session_cookie_name("dashboard")] = session_key
        return request

    def _disable_organization(self):
        self.organization.is_active = False
        self.organization.save(update_fields=["is_active", "updated_at"])

    def _assert_session_auth_cleared(self, session_key):
        session = SessionStore(session_key=session_key)
        session.load()
        self.assertIsNone(session.get(SESSION_KEY))

    def test_active_organization_remains_authorized(self):
        session_key = self._create_crm_session()
        request = self._dashboard_request(session_key)

        middleware = SHVYAAreaAuthenticationMiddleware(
            lambda _request: HttpResponse("ok")
        )
        response = middleware(request)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(request.crm_user.pk, self.user.pk)
        self.assertTrue(request.user.is_authenticated)

        api_request = self.api_request_factory.get(
            "/api/leads/",
            HTTP_X_SHVYA_API_KEY=self.raw_api_key,
        )
        principal, api_key = SHVYAAPIKeyAuthentication().authenticate(
            api_request
        )
        self.assertTrue(principal.is_authenticated)
        self.assertEqual(api_key.pk, self.api_key.pk)

        websocket_user = async_to_sync(_get_crm_user)(session_key)
        self.assertEqual(websocket_user.pk, self.user.pk)

    def test_http_middleware_revokes_existing_session_for_inactive_org(self):
        session_key = self._create_crm_session()
        self._disable_organization()
        request = self._dashboard_request(session_key)

        middleware = SHVYAAreaAuthenticationMiddleware(
            lambda _request: HttpResponse("ok")
        )
        response = middleware(request)

        self.assertEqual(response.status_code, 200)
        self.assertFalse(request.user.is_authenticated)
        self.assertIsNone(request.crm_user)
        self._assert_session_auth_cleared(session_key)

    def test_direct_crm_session_resolution_revokes_inactive_org(self):
        session_key = self._create_crm_session()
        self._disable_organization()
        request = self._dashboard_request(session_key)

        self.assertIsNone(get_crm_authenticated_user(request))
        self._assert_session_auth_cleared(session_key)

    def test_api_key_authentication_rejects_inactive_org(self):
        self._disable_organization()
        request = self.api_request_factory.get(
            "/api/leads/",
            HTTP_X_SHVYA_API_KEY=self.raw_api_key,
        )

        with self.assertRaisesMessage(
            AuthenticationFailed,
            "Organization account is disabled.",
        ):
            SHVYAAPIKeyAuthentication().authenticate(request)

        self.api_key.refresh_from_db()
        self.assertIsNone(self.api_key.last_used_at)

    def test_websocket_session_resolution_rejects_inactive_org(self):
        session_key = self._create_crm_session()
        self._disable_organization()

        websocket_user = async_to_sync(_get_crm_user)(session_key)

        self.assertIsNone(websocket_user)
        self._assert_session_auth_cleared(session_key)
