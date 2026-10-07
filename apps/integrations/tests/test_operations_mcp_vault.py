"""Native Vault MCP must retain tenant, source, client and approval boundaries."""

import base64
import json
import tempfile
import uuid
from unittest import skipUnless
from unittest.mock import patch

from django.apps import apps
from django.test import override_settings

from apps.integrations.models import OperationsAuditEvent, OperationsOAuthToken, OperationsPolicy
from apps.integrations.operations.tools import vault as vault_tools
from apps.integrations.operations.vault_catalog import CAP_VAULT_READ, CAP_VAULT_WRITE
from apps.integrations.operations_policy import ROLE_ORGANIZATION_ADMIN, ROLE_SUPERADMIN
from apps.integrations.tests.operations_mcp_test_base import OperationsMCPBase


@skipUnless(apps.is_installed("apps.vault"), "Requires the native Vault application deployment")
class OperationsMCPVaultTests(OperationsMCPBase):
    def setUp(self):
        super().setUp()
        from apps.vault import models, services
        self.models, self.services = models, services
        self.storage = tempfile.TemporaryDirectory(prefix="shvya-mcp-vault-")
        self.addCleanup(self.storage.cleanup)
        settings_override = override_settings(VAULT_STORAGE_ROOT=self.storage.name)
        settings_override.enable()
        self.addCleanup(settings_override.disable)
        self.policy = OperationsPolicy.objects.create(
            organization=self.organization, organization_admin_enabled=True,
            allowed_capabilities=[CAP_VAULT_READ, CAP_VAULT_WRITE],
            approval_required_capabilities=[CAP_VAULT_WRITE],
        )
        self.bearer = self._token(actor=self.admin, role=ROLE_ORGANIZATION_ADMIN, organization=self.organization)
        self.vault, self.access_code = services.create_vault(self.organization)
        self.other_vault, _ = services.create_vault(self.other_organization)
        self.reason = "Record the approved customer onboarding source in their Vault."
        self.note = {"section": "basics", "external_id": "brief-001-hours", "body": "Open Monday to Friday from 9am to 6pm.",
                     "origin": "ops_chat", "source_date": "2026-10-07"}

    def _ok(self, name, arguments=None):
        result = self._result(self._call(self.bearer, name, arguments))
        self.assertFalse(result["isError"], result)
        return result["structuredContent"]

    def _error(self, name, arguments=None):
        result = self._result(self._call(self.bearer, name, arguments))
        self.assertTrue(result["isError"], result)
        return result

    def _approved(self, name, arguments):
        preview = self._ok(name, {**arguments, "dry_run": True})
        self.assertEqual(preview["status"], "DRY_RUN")
        return self._ok(name, {**arguments, "dry_run": False, "approved": True,
                              "approval_event_id": preview["approval_event_id"]})

    def _create(self, **overrides):
        return self._approved("upsert_vault_entry", {"data": {**self.note, **overrides}, "reason": self.reason})["item"]

    def test_status_is_native_and_never_reveals_credentials_or_client_link(self):
        token = self.services.rotate_agent_token(self.vault)
        result = self._ok("get_vault")
        self.assertTrue(result["exists"])
        self.assertEqual(result["section_count"], 15)
        self.assertEqual(result["id"], str(self.vault.pk))
        encoded = json.dumps(result)
        for value in (self.vault.slug, self.access_code, self.vault.access_code_hash, token, self.vault.token_hash):
            self.assertNotIn(value, encoded)
        self.assertNotIn("download_url", encoded)

    def test_dry_run_and_approval_create_client_visible_fact_without_live_ai_publication(self):
        arguments = {"data": self.note, "reason": self.reason}
        preview = self._ok("upsert_vault_entry", arguments)
        self.assertTrue(preview["client_visible"])
        self.assertFalse(self.vault.entries.exists())
        self._error("upsert_vault_entry", {**arguments, "dry_run": False})
        result = self._ok("upsert_vault_entry", {**arguments, "dry_run": False, "approved": True,
                                                "approval_event_id": preview["approval_event_id"]})
        self.assertFalse(result["automatic_publication"])
        entry = self.vault.entries.get()
        self.assertEqual(entry.author_type, "agent")
        self.assertEqual(entry.created_by_id, self.admin.pk)
        self.assertEqual(entry.effective_body, self.note["body"])
        from apps.ai_engagement.models import FAQ, KnowledgeSource
        self.assertFalse(FAQ.objects.filter(organization=self.organization).exists())
        self.assertFalse(KnowledgeSource.objects.filter(organization=self.organization).exists())

    def test_update_preserves_client_override_and_source_revision(self):
        first = self._create()
        self.services.edit_client_entry(self.vault, first["id"], "Client correction: 10am to 7pm.")
        result = self._create(body="Transcript correction: 8am to 6pm.")
        self.assertEqual(result["body"], "Client correction: 10am to 7pm.")
        self.assertEqual(result["original_body"], "Transcript correction: 8am to 6pm.")
        self.assertTrue(result["client_override_present"])
        self.assertIsNotNone(result["confirmed_at"])
        self.assertEqual(self.vault.entries.count(), 1)
        self.assertEqual(self.vault.entries.get().revisions.count(), 2)

    def test_stale_approval_and_receipt_reuse_are_rejected(self):
        row = self._create()
        args = {"data": {**self.note, "body": "Proposed correction."}, "reason": self.reason}
        preview = self._ok("upsert_vault_entry", args)
        self.services.edit_client_entry(self.vault, row["id"], "New client correction.")
        self._error("upsert_vault_entry", {**args, "dry_run": False, "approved": True,
                                           "approval_event_id": preview["approval_event_id"]})
        self.assertEqual(self.vault.entries.get().body, self.note["body"])
        preview = self._ok("upsert_vault_entry", args)
        execution = {**args, "dry_run": False, "approved": True, "approval_event_id": preview["approval_event_id"]}
        self._ok("upsert_vault_entry", execution)
        self._error("upsert_vault_entry", execution)

    def test_proposal_rechecked_under_lock(self):
        self._create()
        args = {"data": {**self.note, "body": "Proposed correction."}, "reason": self.reason}
        preview = self._ok("upsert_vault_entry", args)
        original = vault_tools._ensure_approved_proposal_unchanged
        calls = []

        def intervene(**kwargs):
            original(**kwargs)
            calls.append(True)
            if len(calls) == 1:
                self.vault.entries.update(body="Concurrent source update.")

        with patch.object(vault_tools, "_ensure_approved_proposal_unchanged", side_effect=intervene):
            self._error("upsert_vault_entry", {**args, "dry_run": False, "approved": True,
                                              "approval_event_id": preview["approval_event_id"]})
        self.assertEqual(self.vault.entries.get().body, "Concurrent source update.")

    def test_reads_reject_foreign_entry_ids_and_export_is_tenant_scoped(self):
        other, _ = self.services.upsert_entry(self.other_vault, self.note)
        own = self._create()
        result = self._ok("export_vault")
        self.assertEqual([row["id"] for row in result["items"]], [own["id"]])
        foreign = self._error("get_vault_entry", {"entry_id": str(other.pk)})
        missing = self._error("get_vault_entry", {"entry_id": str(uuid.uuid4())})
        self.assertEqual(foreign["content"], missing["content"])
        self._error("upsert_vault_entry", {"data": {**self.note, "vault_id": str(self.other_vault.pk)}, "reason": self.reason})

    def test_approval_binds_to_exact_asset_bytes_and_storage_is_encrypted(self):
        raw = b"Verified brochure text for the client."
        data = {**self.note, "kind": "file", "filename": "brochure.txt", "content_base64": base64.b64encode(raw).decode()}
        args = {"data": data, "reason": self.reason}
        preview = self._ok("upload_vault_asset", args)
        self.assertFalse(self.vault.entries.exists())
        self.assertNotIn(data["content_base64"], json.dumps(preview))
        altered = {**args, "data": {**data, "content_base64": base64.b64encode(b"Different approved content.").decode()}}
        self._error("upload_vault_asset", {**altered, "dry_run": False, "approved": True,
                                           "approval_event_id": preview["approval_event_id"]})
        result = self._approved("upload_vault_asset", args)
        row = self.vault.entries.get(pk=result["item"]["id"])
        from pathlib import Path
        encrypted = Path(row.file.path).read_bytes()
        self.assertNotIn(raw, encrypted)
        self.assertTrue(encrypted.startswith(b"SHVYA-VAULT-ENCRYPTED-1"))
        self.vault.refresh_from_db()
        self.assertEqual(self.vault.storage_used_bytes, len(raw))
        found = self._ok("get_vault_asset", {"entry_id": str(row.pk), "include_text": True})
        self.assertEqual(found["text"], raw.decode())
        self.assertNotIn("download_url", json.dumps(found))
        other, _ = self.services.upsert_entry(self.other_vault, self.note)
        self._error("get_vault_asset", {"entry_id": str(other.pk)})

    def test_asset_limits_quota_and_type_checks_apply_before_any_write(self):
        data = {**self.note, "kind": "file", "filename": "invalid.pdf", "content_base64": base64.b64encode(b"not a pdf").decode()}
        self._error("upload_vault_asset", {"data": data, "reason": self.reason})
        data.update(filename="document.txt", content_base64=base64.b64encode(b"1234567890").decode())
        self.models.Vault.objects.filter(pk=self.vault.pk).update(storage_quota_bytes=5)
        self._error("upload_vault_asset", {"data": data, "reason": self.reason})
        data["content_base64"] = "a" * (699052 + 4)
        self._error("upload_vault_asset", {"data": data, "reason": self.reason})
        self.assertFalse(self.vault.entries.exists())

    def test_question_answer_cannot_be_overwritten_or_impersonated(self):
        args = {"data": {"external_id": "ask-01", "section": "qualification", "text": "What makes a qualified lead?"}, "reason": self.reason}
        first = self._approved("upsert_vault_question", args)["item"]
        self.services.answer_question(self.vault, first["id"], "Customers with a confirmed timeline.")
        self._error("upsert_vault_question", {**args, "data": {**args["data"], "text": "A different question?"}})
        self._error("upsert_vault_question", {**args, "data": {**args["data"], "answer": "Invented answer"}})
        exported = self._ok("export_vault", {"collection": "questions"})["items"][0]
        self.assertEqual(exported["answer"], "Customers with a confirmed timeline.")

    def test_call_upsert_preserves_recording_visibility_and_hides_private_url(self):
        data = {"external_id": "meeting-01", "title": "Onboarding", "date": "2026-10-07", "summary": "Discussed service areas.",
                "url": "https://example.test/private-recording", "attendees": ["Client"], "share_recording": False}
        result = self._approved("upsert_vault_call", {"data": data, "reason": self.reason})["item"]
        self.assertEqual(result["url"], "")
        self.assertTrue(result["recording_hidden"])
        self.assertEqual(self._ok("export_vault", {"collection": "calls"})["items"][0]["url"], "")
        self.assertEqual(self.vault.calls.count(), 1)
        result = self._approved("upsert_vault_call", {"data": {**data, "share_recording": True}, "reason": self.reason})["item"]
        self.assertEqual(result["url"], data["url"])

    def test_progress_validates_actual_content(self):
        self._error("set_vault_section", {"section": "basics", "state": "filled", "reason": self.reason})
        self._approved("set_vault_section", {"section": "proof", "state": "dont_have", "is_done": True, "reason": self.reason})
        self._create()
        self._error("set_vault_section", {"section": "basics", "state": "dont_have", "reason": self.reason})
        self._approved("set_vault_section", {"section": "basics", "is_done": True, "reason": self.reason})
        status = self._ok("get_vault")
        self.assertEqual(status["completed_sections"], 2)

    def test_large_evidence_paginates_without_silent_truncation(self):
        body = "Longword " * 250
        for index in range(3):
            self.services.upsert_entry(self.vault, {**self.note, "external_id": f"source-{index}", "body": body})
        first = self._ok("export_vault", {"limit": 2})
        second = self._ok("export_vault", {"limit": 2, "cursor": first["next_cursor"]})
        self.assertEqual(len(first["items"]), 2)
        self.assertEqual(len(second["items"]), 1)
        self.assertIsNone(second["next_cursor"])
        self.assertEqual(first["items"][0]["body"], body.strip())

    def test_secret_redaction_and_protected_field_rejection(self):
        for override in ({"client_body": "Forged client correction"}, {"confirmed_at": "2026-10-07"}, {"author_type": "client"},
                         {"body": "password=very-private-value"}, {"url": "https://example.test/a?signature=private"},
                         {"body": "https://example.test/vault/private-client-slug"}):
            self._error("upsert_vault_entry", {"data": {**self.note, **override}, "reason": self.reason})
        row, _ = self.services.upsert_entry(self.vault, {**self.note, "body": "password=very-private-value https://example.test/a?signature=hidden"})
        encoded = json.dumps(self._ok("get_vault_entry", {"entry_id": str(row.pk)}))
        self.assertNotIn("very-private-value", encoded)
        self.assertNotIn("signature=hidden", encoded)

    def test_capability_grants_and_read_only_tokens_remain_enforced(self):
        self.policy.allowed_capabilities = [CAP_VAULT_READ]
        self.policy.save(update_fields=["allowed_capabilities"])
        self._error("upsert_vault_entry", {"data": self.note, "reason": self.reason})
        OperationsOAuthToken.objects.filter(actor=self.admin).update(granted_capabilities=[])
        self._error("get_vault")
        self.assertFalse(self.vault.entries.exists())

    def test_superadmin_only_creation_provisions_native_vault_without_credentials(self):
        self._error("create_vault_workspace", {"reason": self.reason})
        self.vault.delete()
        OperationsOAuthToken.objects.all().delete()
        self.bearer = self._token(actor=self.superadmin, role=ROLE_SUPERADMIN)
        self._ok("select_organization_context", {"organization_id": str(self.organization.pk), "reason": self.reason})
        result = self._approved("create_vault_workspace", {"name": "Client Portal", "reason": self.reason})
        row = self.models.Vault.objects.get(organization=self.organization)
        self.assertEqual(row.sections.count(), 15)
        self.assertEqual(result["vault"]["name"], "Client Portal")
        encoded = json.dumps(result)
        self.assertNotIn(row.slug, encoded)
        self.assertNotIn(row.access_code_hash, encoded)

    def test_audit_contains_no_client_source_content(self):
        self._create()
        self._ok("export_vault")
        for event in OperationsAuditEvent.objects.filter(tool_name__in=["upsert_vault_entry", "export_vault"]):
            self.assertEqual(event.organization_id, self.organization.pk)
            content = json.dumps(event.change_summary)
            self.assertNotIn(self.note["body"], content)
            self.assertNotIn(self.vault.slug, content)
            self.assertNotIn(self.access_code, content)
