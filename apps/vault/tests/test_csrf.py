"""Exercise HTTPS Vault forms through the real session and CSRF middleware."""
from bs4 import BeautifulSoup
from django.conf import settings
from django.test import Client, override_settings
from django.urls import reverse

from apps.organizations.models import Organization
from apps.vault.models import Vault
from apps.vault.views_client import cookie_name

from .helpers import VaultTestCase


@override_settings(
    DEBUG=False,
    SECURE_SSL_REDIRECT=True,
    SESSION_COOKIE_SECURE=True,
    CSRF_COOKIE_SECURE=True,
    CSRF_USE_SESSIONS=False,
    CSRF_TRUSTED_ORIGINS=[],
)
class VaultCsrfTests(VaultTestCase):
    origin = "https://testserver"

    def setUp(self):
        super().setUp()
        self.client = Client(enforce_csrf_checks=True)
        self.staff_url = reverse("vault-staff-list")
        self.client_url = reverse("vault-client", kwargs={"slug": self.vault.slug})

    def assert_private_response(self, response, *, client_page=False):
        self.assertEqual(response["Referrer-Policy"], "same-origin")
        self.assertIn("no-store", response["Cache-Control"])
        self.assertIn("noindex", response["X-Robots-Tag"])
        if client_page:
            soup = BeautifulSoup(response.content, "html.parser")
            policies = soup.select('meta[name="referrer"]')
            self.assertEqual(len(policies), 1)
            self.assertEqual(policies[0].get("content"), "same-origin")

    def get_page(self, path, *, client_page=False):
        response = self.client.get(path, secure=True)
        self.assertEqual(response.status_code, 200)
        self.assert_private_response(response, client_page=client_page)
        return response

    def rendered_form(self, response, selector):
        """Use the actual masked hidden token and target from the rendered form."""
        form = BeautifulSoup(response.content, "html.parser").select_one(selector)
        self.assertIsNotNone(form)
        self.assertEqual(form.get("method", "").lower(), "post")
        data = {
            field["name"]: field.get("value", "")
            for field in form.select('input[type="hidden"][name]')
        }
        token = data.get("csrfmiddlewaretoken")
        self.assertTrue(token)
        self.assertEqual(len(token), 64)
        self.assertNotEqual(token, self.client.cookies[settings.CSRF_COOKIE_NAME].value)
        return form["action"], data

    def creation_form(self):
        # Authenticate only through the dedicated Superadmin session cookie.
        self.area_login(self.superadmin())
        organization = Organization.objects.create(name="HTTPS Vault creation client")
        path, data = self.rendered_form(self.get_page(self.staff_url), "form.vault-create-form")
        self.assertEqual(path, self.staff_url)
        self.assertEqual(data["action"], "create")
        data.update(organization_id=str(organization.pk), name="HTTPS customer Vault")
        return organization, path, data

    def unlock_form(self):
        response = self.get_page(self.client_url, client_page=True)
        path, data = self.rendered_form(response, "form.vault-form")
        self.assertEqual(path, self.client_url)
        data["code"] = self.code
        return path, data

    def unlock(self, *, referer=False):
        path, data = self.unlock_form()
        headers = {"HTTP_REFERER": self.origin + path} if referer else {"HTTP_ORIGIN": self.origin}
        response = self.client.post(path, data, secure=True, **headers)
        self.assertEqual(response.status_code, 302)
        self.assert_private_response(response)
        grant = response.cookies[cookie_name(self.vault)]
        self.assertTrue(grant["httponly"])
        self.assertTrue(grant["secure"])
        self.assertEqual(grant["path"], self.client_url)
        return response

    def note_form(self):
        response = self.get_page(self.client_url, client_page=True)
        path, data = self.rendered_form(response, "#add-note-basics form")
        self.assertEqual(data["action"], "add_entry")
        self.assertEqual(data["kind"], "note")
        self.assertEqual(data["section"], "basics")
        data["body"] = "Opening hours confirmed by the client over HTTPS."
        return path, data

    def assert_csrf_rejected(self, path, data):
        """A privacy fix must preserve Django's token and origin protections."""
        same_origin = {"HTTP_ORIGIN": self.origin}
        valid_referer = {"HTTP_REFERER": self.origin + path}
        token = data["csrfmiddlewaretoken"]
        mismatched_token = ("A" if token[0] != "A" else "B") + token[1:]
        cases = (
            ("missing token", {key: value for key, value in data.items() if key != "csrfmiddlewaretoken"}, same_origin),
            ("malformed token", {**data, "csrfmiddlewaretoken": "short"}, same_origin),
            ("mismatched token", {**data, "csrfmiddlewaretoken": mismatched_token}, same_origin),
            ("null Origin with valid Referer", data, {**valid_referer, "HTTP_ORIGIN": "null"}),
            ("foreign Origin with valid Referer", data, {**valid_referer, "HTTP_ORIGIN": "https://untrusted.example.test"}),
            ("missing Origin and Referer", data, {}),
            ("foreign Referer", data, {"HTTP_REFERER": "https://untrusted.example.test/form/"}),
            ("insecure Referer", data, {"HTTP_REFERER": "http://testserver" + path}),
        )
        for label, payload, headers in cases:
            with self.subTest(case=label):
                response = self.client.post(path, payload, secure=True, **headers)
                self.assertEqual(response.status_code, 403)

        cookie = self.client.cookies[settings.CSRF_COOKIE_NAME].value
        try:
            with self.subTest(case="mismatched cookie"):
                self.client.cookies[settings.CSRF_COOKIE_NAME] = ("A" if cookie[0] != "A" else "B") + cookie[1:]
                response = self.client.post(path, data, secure=True, **same_origin)
                self.assertEqual(response.status_code, 403)
            with self.subTest(case="missing cookie"):
                del self.client.cookies[settings.CSRF_COOKIE_NAME]
                response = self.client.post(path, data, secure=True, **same_origin)
                self.assertEqual(response.status_code, 403)
        finally:
            self.client.cookies[settings.CSRF_COOKIE_NAME] = cookie

    def test_https_superadmin_creation_accepts_rendered_token(self):
        organization, path, data = self.creation_form()
        response = self.client.post(path, data, secure=True, HTTP_ORIGIN=self.origin)
        self.assertEqual(response.status_code, 200)
        self.assert_private_response(response)
        created = Vault.objects.get(organization=organization)
        self.assertEqual(created.name, data["name"])
        self.assertEqual(created.sections.count(), 15)
        self.assertTrue(created.check_access_code(response.context["fresh_secret"]["value"]))

    def test_https_superadmin_creation_accepts_same_origin_referer_fallback(self):
        organization, path, data = self.creation_form()
        response = self.client.post(path, data, secure=True, HTTP_REFERER=self.origin + path)
        self.assertEqual(response.status_code, 200)
        self.assert_private_response(response)
        self.assertTrue(Vault.objects.filter(organization=organization).exists())

    def test_https_client_unlock_and_note_save_accept_rendered_tokens(self):
        self.unlock()
        path, data = self.note_form()
        response = self.client.post(path, data, secure=True, HTTP_ORIGIN=self.origin)
        self.assertEqual(response.status_code, 302)
        self.assert_private_response(response)
        entry = self.vault.entries.get()
        self.assertEqual(entry.body, data["body"])
        self.assertEqual(entry.author_type, "client")
        self.assertIsNotNone(entry.confirmed_at)

    def test_https_client_unlock_and_note_save_accept_same_origin_referer_fallback(self):
        self.unlock(referer=True)
        path, data = self.note_form()
        response = self.client.post(path, data, secure=True, HTTP_REFERER=self.origin + path)
        self.assertEqual(response.status_code, 302)
        self.assert_private_response(response)
        self.assertEqual(self.vault.entries.get().body, data["body"])

    def test_superadmin_creation_rejects_invalid_csrf_requests(self):
        organization, path, data = self.creation_form()
        self.assert_csrf_rejected(path, data)
        self.assertFalse(Vault.objects.filter(organization=organization).exists())

    def test_client_unlock_rejects_invalid_csrf_requests(self):
        path, data = self.unlock_form()
        self.assert_csrf_rejected(path, data)
        self.assertNotIn(cookie_name(self.vault), self.client.cookies)
        self.vault.refresh_from_db()
        self.assertEqual(self.vault.failed_access_attempts, 0)

    def test_client_note_save_rejects_invalid_csrf_requests(self):
        self.unlock()
        path, data = self.note_form()
        self.assert_csrf_rejected(path, data)
        self.assertFalse(self.vault.entries.exists())
