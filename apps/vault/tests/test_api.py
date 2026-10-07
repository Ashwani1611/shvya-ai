import json
from datetime import timedelta
from unittest.mock import patch

from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.vault.models import Vault
from apps.vault.services import confirm_entry, rotate_agent_token, upsert_entry

from .helpers import VaultTestCase


class VaultAgentAPITests(VaultTestCase):
    def setUp(self):
        super().setUp()
        self.token = rotate_agent_token(self.vault)
        self.headers = {"HTTP_AUTHORIZATION": f"Bearer {self.token}"}

    def post(self, endpoint, data, **kwargs):
        return self.client.post(reverse(f"vault_agent:{endpoint}"), data=json.dumps(data), content_type="application/json", **self.headers, **kwargs)

    def test_bearer_required_even_with_superadmin_browser_session(self):
        self.area_login(self.superadmin())
        response = self.client.get(reverse("vault_agent:workspace"))
        self.assertEqual(response.status_code, 401)
        self.assertIn("Bearer", response["WWW-Authenticate"])
        self.assertEqual(response.json()["code"], "invalid_token")
        response = self.client.get(reverse("vault_agent:workspace"), {"token": self.token})
        self.assertEqual(response.status_code, 401)

    def test_token_workspace_and_export_never_include_another_organization(self):
        upsert_entry(self.vault, {"section": "basics", "body": "Alpha onboarding facts"})
        upsert_entry(self.other_vault, {"section": "basics", "body": "Beta confidential pricing"})
        for endpoint in ["workspace", "export"]:
            with self.subTest(endpoint=endpoint):
                response = self.client.get(reverse(f"vault_agent:{endpoint}"), {"vault_id": str(self.other_vault.pk)}, **self.headers)
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, "Alpha onboarding facts")
                self.assertNotContains(response, "Beta confidential pricing")
                self.assertIn("no-store", response["Cache-Control"])

    def test_agent_cannot_set_tenant_author_or_client_confirmation_fields(self):
        for field, value in [
            ("vault_id", str(self.other_vault.pk)),
            ("organization_id", str(self.other_org.pk)),
            ("workspace", str(self.other_vault.pk)),
            ("author_type", "client"),
            ("client_body", "forged customer statement"),
            ("confirmed_at", timezone.now().isoformat()),
        ]:
            with self.subTest(field=field):
                response = self.post("entries", {"section": "basics", "body": "Note", field: value})
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json()["code"], "protected_fields")
        self.assertEqual(self.vault.entries.count(), 0)
        self.assertEqual(self.other_vault.entries.count(), 0)

    def test_idempotent_note_replay_preserves_confirmation_and_no_extra_revision(self):
        data = {"section": "basics", "body": "Open 10 am", "external_id": "call-2:basics", "origin": "fireflies"}
        first = self.post("entries", data)
        self.assertEqual(first.status_code, 201)
        entry = self.vault.entries.get()
        confirm_entry(self.vault, entry.pk)
        entry.refresh_from_db()
        confirmed_at = entry.confirmed_at
        revision_count = entry.revisions.count()
        replay = self.post("entries", data)
        self.assertEqual(replay.status_code, 200)
        self.assertTrue(replay.json()["updated"])
        self.assertEqual(first.json()["id"], replay.json()["id"])
        entry.refresh_from_db()
        self.assertEqual(entry.confirmed_at, confirmed_at)
        self.assertEqual(entry.revisions.count(), revision_count)

    def test_external_id_cannot_silently_move_note_to_different_section(self):
        data = {"section": "basics", "body": "Opening hours", "external_id": "same-source"}
        self.assertEqual(self.post("entries", data).status_code, 201)
        conflict = self.post("entries", {**data, "section": "offers"})
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(self.vault.entries.get().section, "basics")

    def test_paused_client_workspace_still_accepts_agent_notes(self):
        Vault.objects.filter(pk=self.vault.pk).update(is_paused=True)
        self.assertEqual(self.client.get(reverse("vault_agent:workspace"), **self.headers).status_code, 200)
        self.assertEqual(self.post("entries", {"section": "other", "body": "Rep supplied a factual update"}).status_code, 201)

    def test_expired_rotated_and_revoked_tokens_fail_reads_and_writes(self):
        rotate_agent_token(self.vault)
        for endpoint in ["workspace", "export"]:
            self.assertEqual(self.client.get(reverse(f"vault_agent:{endpoint}"), **self.headers).status_code, 401)
        self.assertEqual(self.post("entries", {"section": "other", "body": "Unauthorized"}).status_code, 401)
        self.token = rotate_agent_token(self.vault)
        self.headers = {"HTTP_AUTHORIZATION": f"Bearer {self.token}"}
        Vault.objects.filter(pk=self.vault.pk).update(token_expires_at=timezone.now() - timedelta(seconds=1))
        self.assertEqual(self.client.get(reverse("vault_agent:workspace"), **self.headers).status_code, 401)
        Vault.objects.filter(pk=self.vault.pk).update(token_expires_at=timezone.now() + timedelta(days=1), token_hash=None)
        self.assertEqual(self.client.get(reverse("vault_agent:workspace"), **self.headers).status_code, 401)
        self.assertEqual(self.vault.entries.count(), 0)

    def test_rotation_between_authentication_and_write_cannot_use_stale_token(self):
        stale_vault = Vault.objects.get(pk=self.vault.pk)
        rotate_agent_token(self.vault)
        with patch("apps.vault.api.authenticate_agent_token", return_value=stale_vault):
            response = self.post("entries", {"section": "other", "body": "Stale token write"})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(self.vault.entries.count(), 0)

    def test_invalid_json_content_type_and_excessive_body_fail_cleanly(self):
        url = reverse("vault_agent:entries")
        for payload in ["{", "[]", "null", "1"]:
            with self.subTest(payload=payload):
                response = self.client.post(url, data=payload, content_type="application/json", **self.headers)
                self.assertEqual(response.status_code, 400)
        self.assertEqual(self.client.post(url, data={"body": "form"}, **self.headers).status_code, 415)
        self.assertEqual(self.post("entries", {"section": "basics", "body": "x" * 70000}).status_code, 413)
        self.assertEqual(self.vault.entries.count(), 0)

    def test_api_accepts_bearer_post_without_browser_csrf_but_requires_correct_method(self):
        client = Client(enforce_csrf_checks=True)
        response = client.post(reverse("vault_agent:entries"), data=json.dumps({"section": "other", "body": "API note"}), content_type="application/json", **self.headers)
        self.assertEqual(response.status_code, 201)
        response = client.get(reverse("vault_agent:entries"), **self.headers)
        self.assertEqual(response.status_code, 405)
        self.assertEqual(response["Allow"], "POST")

    def test_question_and_call_replays_are_scoped_and_idempotent(self):
        for endpoint, data in [
            ("questions", {"section": "basics", "text": "When do you open?", "external_id": "call-2:q:1"}),
            ("calls", {"title": "Discovery", "date": "2026-10-05", "external_id": "call-2", "summary": "Customer shared opening hours."}),
        ]:
            with self.subTest(endpoint=endpoint):
                first = self.post(endpoint, data)
                second = self.post(endpoint, data)
                self.assertEqual(first.status_code, 201)
                self.assertEqual(second.status_code, 200)
                self.assertEqual(first.json()["id"], second.json()["id"])
        self.assertEqual(self.vault.questions.count(), 1)
        self.assertEqual(self.vault.calls.count(), 1)
        self.assertEqual(self.other_vault.questions.count(), 0)
        self.assertEqual(self.other_vault.calls.count(), 0)
