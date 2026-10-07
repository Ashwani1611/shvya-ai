from datetime import timedelta
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.organizations.models import Organization
from apps.vault.models import Vault
from apps.vault.services import (
    create_profile_snapshot,
    rotate_access_code,
    rotate_agent_token,
    signed_download_url,
    upsert_entry,
    upsert_question,
)
from apps.vault.views_client import cookie_name

from .helpers import VaultTestCase


class VaultClientViewTests(VaultTestCase):
    def setUp(self):
        super().setUp()
        self.url = reverse("vault-client", kwargs={"slug": self.vault.slug})
        self.other_url = reverse("vault-client", kwargs={"slug": self.other_vault.slug})

    def unlock(self, client=None):
        response = (client or self.client).post(self.url, {"code": self.code})
        self.assertEqual(response.status_code, 302)
        return response

    def test_gate_does_not_leak_workspace_material_and_direct_posts_require_unlock(self):
        upsert_entry(self.vault, {"section": "basics", "body": "Sensitive customer setup text"})
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Sensitive customer setup text")
        self.assertIn("no-store", response["Cache-Control"])
        denied = self.client.post(self.url, {"action": "add_entry", "section": "basics", "kind": "note", "body": "Unauthenticated write"})
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(self.vault.entries.count(), 1)

    def test_unlock_cookie_is_http_only_scoped_and_does_not_grant_superadmin(self):
        response = self.unlock()
        cookie = response.cookies[cookie_name(self.vault)]
        self.assertTrue(cookie["httponly"])
        self.assertEqual(cookie["samesite"], "Lax")
        self.assertEqual(cookie["path"], self.url)
        self.assertNotIn(self.code, cookie.value)
        self.assertEqual(self.client.get(self.url).status_code, 200)
        denied = self.client.get(reverse("vault-staff-list"))
        self.assertEqual(denied.status_code, 302)
        self.assertIn("/superadmin/login/", denied["Location"])

    def test_client_authentication_is_per_vault_and_cookie_tampering_fails(self):
        self.unlock()
        response = self.client.post(self.other_url, {"action": "add_entry", "section": "basics", "kind": "note", "body": "Cross-tenant write"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.other_vault.entries.count(), 0)
        name = cookie_name(self.vault)
        self.client.cookies[name] = self.client.cookies[name].value + "tamper"
        response = self.client.post(self.url, {"action": "add_entry", "section": "basics", "kind": "note", "body": "Forged session write"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.vault.entries.count(), 0)

    def test_rotation_revokes_existing_client_cookie_and_old_code(self):
        self.unlock()
        with patch("apps.vault.services.secrets.randbelow", return_value=(int(self.code) + 1) % 1000000):
            new_code = rotate_access_code(self.vault)
        blocked = self.client.post(self.url, {"action": "add_entry", "section": "basics", "kind": "note", "body": "Stale grant"})
        self.assertEqual(blocked.status_code, 403)
        self.assertEqual(self.client.post(self.url, {"code": self.code}).status_code, 400)
        self.assertEqual(self.client.post(self.url, {"code": new_code}).status_code, 302)
        self.assertEqual(self.vault.entries.count(), 0)

    def test_pause_blocks_unlocked_client_reads_writes_and_exports(self):
        self.unlock()
        Vault.objects.filter(pk=self.vault.pk).update(is_paused=True)
        self.assertEqual(self.client.get(self.url).status_code, 403)
        self.assertEqual(self.client.post(self.url, {"action": "submit"}).status_code, 403)
        self.assertEqual(self.client.get(reverse("vault-client-export", kwargs={"slug": self.vault.slug})).status_code, 403)

    def test_code_attempt_lock_applies_across_ips_and_expires(self):
        wrong = f"{(int(self.code) + 1) % 1000000:06d}"
        for index in range(8):
            response = self.client.post(self.url, {"code": wrong}, REMOTE_ADDR=f"198.51.100.{index + 10}")
            self.assertEqual(response.status_code, 400)
            self.assertContains(response, "access code is incorrect", status_code=400)
        blocked = self.client.post(self.url, {"code": self.code}, REMOTE_ADDR="203.0.113.80")
        self.assertEqual(blocked.status_code, 429)
        self.assertIn("Retry-After", blocked)
        Vault.objects.filter(pk=self.vault.pk).update(access_locked_until=timezone.now() - timedelta(seconds=1))
        self.assertEqual(self.client.post(self.url, {"code": self.code}).status_code, 302)
        self.vault.refresh_from_db()
        self.assertEqual(self.vault.failed_access_attempts, 0)

    def test_unlock_and_customer_writes_require_csrf(self):
        csrf_client = Client(enforce_csrf_checks=True)
        self.assertEqual(csrf_client.post(self.url, {"code": self.code}).status_code, 403)
        csrf_client.get(self.url)
        token = csrf_client.cookies[settings.CSRF_COOKIE_NAME].value
        response = csrf_client.post(self.url, {"code": self.code, "csrfmiddlewaretoken": token})
        self.assertEqual(response.status_code, 302)
        data = {"action": "add_entry", "section": "basics", "kind": "note", "body": "Verified customer note"}
        self.assertEqual(csrf_client.post(self.url, data).status_code, 403)
        self.assertEqual(csrf_client.post(self.url, {**data, "csrfmiddlewaretoken": token}).status_code, 302)
        self.assertEqual(self.vault.entries.get().body, data["body"])

    def test_client_cannot_forge_author_or_mutate_foreign_entries_and_questions(self):
        self.unlock()
        response = self.client.post(self.url, {"action": "add_entry", "section": "basics", "kind": "note", "body": "Client input", "author_type": "agent", "vault_id": self.other_vault.pk})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.vault.entries.get().author_type, "client")
        entry, _ = upsert_entry(self.other_vault, {"section": "basics", "body": "Foreign source"})
        question, _ = upsert_question(self.other_vault, {"text": "Foreign question"})
        for data in [
            {"action": "update_entry", "entry_id": entry.pk, "body": "intrusion"},
            {"action": "confirm_entry", "entry_id": entry.pk},
            {"action": "delete_entry", "entry_id": entry.pk},
            {"action": "answer_question", "question_id": question.pk, "answer": "intrusion"},
        ]:
            with self.subTest(action=data["action"]):
                self.assertEqual(self.client.post(self.url, data).status_code, 404)
        entry.refresh_from_db()
        question.refresh_from_db()
        self.assertEqual(entry.body, "Foreign source")
        self.assertEqual(question.answer, "")

    def test_client_can_correct_agent_material_but_not_delete_its_provenance(self):
        self.unlock()
        entry, _ = upsert_entry(self.vault, {"section": "basics", "body": "Original call note"})
        response = self.client.post(self.url, {"action": "update_entry", "entry_id": entry.pk, "body": "Customer correction", "expected_updated_at": entry.updated_at.isoformat()})
        self.assertEqual(response.status_code, 302)
        entry.refresh_from_db()
        self.assertEqual(entry.body, "Original call note")
        self.assertEqual(entry.client_body, "Customer correction")
        self.assertEqual(self.client.post(self.url, {"action": "delete_entry", "entry_id": entry.pk}).status_code, 403)
        self.assertTrue(self.vault.entries.filter(pk=entry.pk).exists())

    def test_content_is_escaped_in_workspace_and_export_is_authenticated(self):
        dangerous = '<script>alert("vault")</script>'
        upsert_entry(self.vault, {"section": "basics", "body": dangerous})
        export_url = reverse("vault-client-export", kwargs={"slug": self.vault.slug})
        self.assertEqual(self.client.get(export_url).status_code, 403)
        self.unlock()
        response = self.client.get(self.url)
        self.assertContains(response, "&lt;script&gt;")
        self.assertNotContains(response, dangerous)
        export = self.client.get(export_url)
        self.assertEqual(export.status_code, 200)
        self.assertIn("attachment", export["Content-Disposition"])
        self.assertIn("no-store", export["Cache-Control"])

    def test_partial_submission_records_notification_and_logout_revokes_cookie(self):
        self.unlock()
        response = self.client.post(self.url, {"action": "set_section", "section": "proof", "state": "dont_have", "is_done": "true"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.client.post(self.url, {"action": "submit"}).status_code, 302)
        self.vault.refresh_from_db()
        self.assertEqual(self.vault.status, "submitted")
        self.assertTrue(self.vault.events.filter(kind="submitted").exists())
        self.assertEqual(self.client.post(self.url, {"action": "logout"}).status_code, 302)
        self.assertEqual(self.client.post(self.url, {"action": "submit"}).status_code, 403)


class VaultSuperadminViewTests(VaultTestCase):
    def setUp(self):
        super().setUp()
        self.user = self.superadmin()
        self.url = reverse("vault-staff-list")
        self.detail_url = reverse("vault-staff-detail", kwargs={"vault_id": self.vault.pk})

    def test_default_admin_cookie_and_crm_cookie_cannot_open_superadmin_vault(self):
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(self.url).status_code, 302)
        self.area_login(self.user, area="dashboard")
        self.assertEqual(self.client.get(self.url).status_code, 302)
        self.area_login(self.user)
        self.assertEqual(self.client.get(self.url).status_code, 200)

    def test_regular_org_admin_cannot_use_dedicated_superadmin_cookie(self):
        user = User.objects.create_user(email="regular@example.test", password="test-only", name="Org admin", organization=self.org, role=User.Role.ADMIN)
        self.area_login(user)
        self.assertEqual(self.client.get(self.url).status_code, 302)
        self.assertEqual(self.client.post(self.url, {"action": "pause", "vault_id": self.vault.pk}).status_code, 302)
        self.vault.refresh_from_db()
        self.assertFalse(self.vault.is_paused)

    def test_staff_actions_are_csrf_protected(self):
        client = Client(enforce_csrf_checks=True)
        self.area_login(self.user, client)
        self.assertEqual(client.post(self.url, {"action": "pause", "vault_id": self.vault.pk}).status_code, 403)
        self.vault.refresh_from_db()
        self.assertFalse(self.vault.is_paused)

    def test_create_organization_vault_displays_code_once_and_rejects_duplicate(self):
        self.area_login(self.user)
        org = Organization.objects.create(name="New Vault Client")
        response = self.client.post(self.url, {"action": "create", "organization_id": org.pk})
        self.assertEqual(response.status_code, 200)
        vault = Vault.objects.get(organization=org)
        self.assertEqual(vault.sections.count(), 15)
        secret = response.context["fresh_secret"]["value"]
        self.assertTrue(vault.check_access_code(secret))
        detail = self.client.get(reverse("vault-staff-detail", kwargs={"vault_id": vault.pk}))
        self.assertNotIn("fresh_secret", detail.context)
        self.client.post(self.url, {"action": "create", "organization_id": org.pk})
        self.assertEqual(Vault.objects.filter(organization=org).count(), 1)

    def test_pause_resume_revokes_existing_client_grant_and_token_revocation_is_immediate(self):
        client = Client()
        client_url = reverse("vault-client", kwargs={"slug": self.vault.slug})
        self.assertEqual(client.post(client_url, {"code": self.code}).status_code, 302)
        token = rotate_agent_token(self.vault)
        self.area_login(self.user)
        for action in ["pause", "resume"]:
            self.assertEqual(self.client.post(self.url, {"action": action, "vault_id": self.vault.pk}).status_code, 302)
        self.assertEqual(client.post(client_url, {"action": "submit"}).status_code, 403)
        self.assertEqual(self.client.post(self.url, {"action": "revoke_token", "vault_id": self.vault.pk}).status_code, 302)
        response = client.get(reverse("vault_agent:workspace"), HTTP_AUTHORIZATION=f"Bearer {token}")
        self.assertEqual(response.status_code, 401)

    def test_staff_cannot_overwrite_or_delete_client_authored_material(self):
        entry, _ = upsert_entry(self.vault, {"section": "basics", "body": "Client statement"}, author_type="client")
        self.area_login(self.user)
        for action in ["update_entry", "delete_entry"]:
            response = self.client.post(self.detail_url, {"action": action, "entry_id": entry.pk, "body": "Operator override"})
            self.assertEqual(response.status_code, 403)
        entry.refresh_from_db()
        self.assertEqual(entry.effective_body, "Client statement")

    def test_profile_download_requires_staff_and_matching_vault(self):
        snapshot = create_profile_snapshot(self.other_vault)
        url = reverse("vault-staff-profile", kwargs={"vault_id": self.other_vault.pk, "snapshot_id": snapshot.pk})
        self.assertEqual(self.client.get(url).status_code, 302)
        self.area_login(self.user)
        self.assertEqual(self.client.get(url).status_code, 200)
        wrong = reverse("vault-staff-profile", kwargs={"vault_id": self.vault.pk, "snapshot_id": snapshot.pk})
        self.assertEqual(self.client.get(wrong).status_code, 404)

    def test_staff_file_edit_preserves_source_and_customer_correction(self):
        from apps.vault.services import edit_client_entry
        entry, _ = upsert_entry(self.vault, {
            "section": "brochures", "kind": "file", "body": "Original catalogue",
            "origin": "fireflies", "source_date": "2026-10-05",
            "allowed_for_ai_sharing": True, "send_when": "After a product enquiry",
        }, author_type="team", upload=SimpleUploadedFile("catalogue.pdf", b"%PDF-1.7\nexample"))
        edit_client_entry(self.vault, entry.pk, "Customer correction")
        entry.refresh_from_db()
        original_file = entry.file.name
        self.area_login(self.user)
        response = self.client.post(self.detail_url, {"action": "update_entry", "entry_id": entry.pk,
            "body": "Updated team description", "expected_updated_at": entry.updated_at.isoformat(), "section": "brochures"})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response["Location"].endswith("#brochures"))
        entry.refresh_from_db()
        self.assertEqual(entry.body, "Updated team description")
        self.assertEqual(entry.effective_body, "Customer correction")
        self.assertEqual(entry.file.name, original_file)
        self.assertEqual(entry.source_date.isoformat(), "2026-10-05")
        self.assertTrue(entry.allowed_for_ai_sharing)
        self.assertEqual(entry.send_when, "After a product enquiry")

    def test_staff_call_form_parses_duration_and_sharing_control(self):
        self.area_login(self.user)
        response = self.client.post(self.detail_url, {"action": "add_call", "title": "Discovery call",
            "date": "2026-10-05", "duration_min": "38", "attendees": "Client\nSHVYA team",
            "summary": "Confirmed the opening hours.", "url": "https://example.test/recording", "share_recording": "on"})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response["Location"].endswith("#calls"))
        call = self.vault.calls.get()
        self.assertEqual(call.duration_min, 38)
        self.assertTrue(call.share_recording)
        self.assertEqual(call.attendees, ["Client", "SHVYA team"])

    def test_quota_rejects_invalid_or_below_usage_allowance(self):
        self.area_login(self.user)
        original = self.vault.storage_quota_bytes
        Vault.objects.filter(pk=self.vault.pk).update(storage_used_bytes=700 * 1024**2)
        for quota in ["-1", "not-an-integer", "512"]:
            response = self.client.post(self.url, {"action": "quota", "vault_id": self.vault.pk, "storage_quota_mb": quota})
            self.assertEqual(response.status_code, 200)
            self.vault.refresh_from_db()
            self.assertEqual(self.vault.storage_quota_bytes, original)


class VaultPrivateDownloadTests(VaultTestCase):
    def setUp(self):
        super().setUp()
        self.content = b"%PDF-1.7\nprivate file payload\n"
        self.entry, _ = upsert_entry(self.vault, {"section": "brochures", "kind": "file"}, author_type="client", upload=SimpleUploadedFile("catalogue.pdf", self.content, content_type="application/pdf"))
        self.other_entry, _ = upsert_entry(self.other_vault, {"section": "brochures", "kind": "file"}, author_type="client", upload=SimpleUploadedFile("other.pdf", b"%PDF-1.7\nother company\n", content_type="application/pdf"))

    def download(self, response):
        self.assertEqual(response.status_code, 200)
        self.assertIn("attachment", response["Content-Disposition"])
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")
        self.assertIn("no-store", response["Cache-Control"])
        self.assertEqual(b"".join(response.streaming_content), self.content)

    def test_client_file_requires_grant_and_scope_and_returns_plaintext_attachment(self):
        url = reverse("vault-client-file", kwargs={"slug": self.vault.slug, "entry_id": self.entry.pk})
        self.assertEqual(self.client.get(url).status_code, 403)
        self.client.post(reverse("vault-client", kwargs={"slug": self.vault.slug}), {"code": self.code})
        self.download(self.client.get(url))
        foreign = reverse("vault-client-file", kwargs={"slug": self.vault.slug, "entry_id": self.other_entry.pk})
        self.assertEqual(self.client.get(foreign).status_code, 404)

    def test_staff_file_uses_dedicated_session_and_scope(self):
        url = reverse("vault-staff-file", kwargs={"vault_id": self.vault.pk, "entry_id": self.entry.pk})
        self.assertEqual(self.client.get(url).status_code, 302)
        self.area_login(self.superadmin())
        self.download(self.client.get(url))
        foreign = reverse("vault-staff-file", kwargs={"vault_id": self.vault.pk, "entry_id": self.other_entry.pk})
        self.assertEqual(self.client.get(foreign).status_code, 404)

    def test_signed_client_url_expires_after_one_hour_and_cannot_target_another_entry(self):
        url = signed_download_url(self.entry)
        self.download(self.client.get(url))
        signature = parse_qs(urlsplit(url).query)["signature"][0]
        other = reverse("vault-signed-file", kwargs={"entry_id": self.other_entry.pk})
        self.assertEqual(self.client.get(other, {"signature": signature}).status_code, 403)
        with patch("django.core.signing.time.time", return_value=timezone.now().timestamp() + 3601):
            self.assertEqual(self.client.get(url).status_code, 403)

    def test_signed_client_url_is_revoked_by_code_rotation_and_pause(self):
        url = signed_download_url(self.entry)
        rotate_access_code(self.vault)
        self.assertEqual(self.client.get(url).status_code, 403)
        self.entry.refresh_from_db()
        fresh = signed_download_url(self.entry)
        Vault.objects.filter(pk=self.vault.pk).update(is_paused=True)
        self.assertEqual(self.client.get(fresh).status_code, 403)

    def test_signed_agent_url_survives_pause_but_not_token_rotation_revocation_or_expiry(self):
        rotate_agent_token(self.vault)
        self.entry.refresh_from_db()
        url = signed_download_url(self.entry, purpose="agent")
        Vault.objects.filter(pk=self.vault.pk).update(is_paused=True)
        self.download(self.client.get(url))
        rotate_agent_token(self.vault)
        self.assertEqual(self.client.get(url).status_code, 403)
        self.entry.refresh_from_db()
        current = signed_download_url(self.entry, purpose="agent")
        Vault.objects.filter(pk=self.vault.pk).update(token_expires_at=timezone.now() - timedelta(seconds=1))
        self.assertEqual(self.client.get(current).status_code, 403)
        Vault.objects.filter(pk=self.vault.pk).update(token_expires_at=timezone.now() + timedelta(days=1), token_hash=None)
        self.assertEqual(self.client.get(current).status_code, 403)

    def test_unsigned_tampered_or_staff_only_links_cannot_be_redeemed(self):
        url = reverse("vault-signed-file", kwargs={"entry_id": self.entry.pk})
        self.assertEqual(self.client.get(url).status_code, 403)
        signed = signed_download_url(self.entry)
        self.assertEqual(self.client.get(signed + "tampered").status_code, 403)
        self.assertEqual(self.client.get(signed_download_url(self.entry, purpose="staff")).status_code, 403)
