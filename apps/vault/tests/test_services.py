"""Regression tests for private, reviewable customer onboarding material."""
import json
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, transaction
from django.test import override_settings
from django.utils import timezone

from apps.vault.models import Vault, VaultEvent, VaultSection
from apps.vault.sections import SECTION_KEYS
from apps.vault.services import (
    VaultConflict,
    answer_question,
    authenticate_agent_token,
    confirm_entry,
    create_profile_snapshot,
    create_vault,
    delete_entry,
    edit_client_entry,
    export_markdown,
    rotate_access_code,
    rotate_agent_token,
    set_section_state,
    submit_vault,
    upsert_call,
    upsert_entry,
    upsert_question,
    workspace_payload,
)

from .helpers import VaultTestCase


class VaultLifecycleTests(VaultTestCase):
    def test_creation_has_unique_client_gate_and_exact_fifteen_sections(self):
        self.assertRegex(self.code, r"^\d{6}$")
        self.assertNotEqual(self.vault.access_code_hash, self.code)
        self.assertTrue(self.vault.check_access_code(self.code))
        self.assertFalse(self.vault.check_access_code("wrong-code"))
        self.assertNotEqual(self.vault.slug, self.other_vault.slug)
        self.assertEqual(set(self.vault.sections.values_list("key", flat=True)), set(SECTION_KEYS))
        self.assertEqual(self.vault.sections.count(), 15)
        self.assertEqual(self.vault.status, "draft")
        self.assertEqual(self.vault.storage_used_bytes, 0)
        self.assertEqual(self.vault.storage_quota_bytes, 2 * 1024**3)

    def test_organization_cannot_have_multiple_vaults(self):
        with self.assertRaises((ValidationError, IntegrityError)):
            with transaction.atomic():
                create_vault(self.org)
        self.assertEqual(Vault.objects.filter(organization=self.org).count(), 1)

    def test_access_code_rotation_changes_version_and_invalidates_old_code(self):
        old_version = self.vault.access_version
        with patch("apps.vault.services.secrets.randbelow", return_value=(int(self.code) + 1) % 1000000):
            code = rotate_access_code(self.vault)
        self.vault.refresh_from_db()
        self.assertGreater(self.vault.access_version, old_version)
        self.assertTrue(self.vault.check_access_code(code))
        self.assertFalse(self.vault.check_access_code(self.code))

    def test_access_code_rotation_retries_a_random_collision(self):
        next_code = (int(self.code) + 1) % 1000000
        with patch("apps.vault.services.secrets.randbelow", side_effect=[int(self.code), next_code]) as random_code:
            code = rotate_access_code(self.vault)
        self.assertEqual(random_code.call_count, 2)
        self.assertEqual(code, f"{next_code:06d}")
        self.vault.refresh_from_db()
        self.assertFalse(self.vault.check_access_code(self.code))
        self.assertTrue(self.vault.check_access_code(code))

    def test_agent_tokens_are_hashed_expire_after_ninety_days_and_rotate(self):
        before = timezone.now()
        first = rotate_agent_token(self.vault)
        self.vault.refresh_from_db()
        self.assertTrue(first.startswith("sv_"))
        self.assertNotEqual(self.vault.token_hash, first)
        self.assertNotIn(first, json.dumps(workspace_payload(self.vault), default=str))
        self.assertEqual(authenticate_agent_token(first).pk, self.vault.pk)
        self.assertAlmostEqual((self.vault.token_expires_at - before).total_seconds(), 90 * 86400, delta=10)
        second = rotate_agent_token(self.vault)
        self.assertIsNone(authenticate_agent_token(first))
        self.assertEqual(authenticate_agent_token(second).pk, self.vault.pk)
        Vault.objects.filter(pk=self.vault.pk).update(token_expires_at=timezone.now() - timedelta(seconds=1))
        self.assertIsNone(authenticate_agent_token(second))

    def test_pausing_client_access_does_not_revoke_agent_token(self):
        token = rotate_agent_token(self.vault)
        Vault.objects.filter(pk=self.vault.pk).update(is_paused=True)
        self.assertEqual(authenticate_agent_token(token).pk, self.vault.pk)

    def test_payload_and_markdown_never_export_authentication_secrets(self):
        token = rotate_agent_token(self.vault)
        self.vault.refresh_from_db()
        payload = json.dumps(workspace_payload(self.vault), default=str)
        markdown = export_markdown(self.vault)
        for secret in [token, self.vault.token_hash, self.vault.access_code_hash]:
            self.assertNotIn(secret, payload)
            self.assertNotIn(secret, markdown)

    def test_submission_and_profile_snapshot_never_change_live_ai_configuration(self):
        from apps.ai_engagement.models import OrgInfo
        info, _ = OrgInfo.objects.get_or_create(organization=self.org)
        info.about = "Approved production description"
        info.ai_playbook = "Never replace this production prompt."
        info.ai_enabled = False
        info.bot_languages = "English"
        info.save()
        original = type(info).objects.filter(pk=info.pk).values().get()
        upsert_entry(self.vault, {"section": "rules", "body": "Draft replacement instructions", "external_id": "draft:rules"})
        submit_vault(self.vault)
        snapshot = create_profile_snapshot(self.vault)
        self.vault.refresh_from_db()
        self.assertEqual(self.vault.status, "submitted")
        self.assertIsNotNone(self.vault.submitted_at)
        self.assertEqual(snapshot.status, "draft")
        self.assertIn("Draft replacement instructions", json.dumps(snapshot.body))
        self.assertEqual(type(info).objects.filter(pk=info.pk).values().get(), original)
        self.assertTrue(VaultEvent.objects.filter(vault=self.vault, kind="submitted").exists())


class VaultSourcePrecedenceTests(VaultTestCase):
    def note(self, vault=None, **overrides):
        data = {"section": "offerings", "body": "Price: 500", "origin": "fireflies", "source_date": "2026-10-05", "external_id": "call-123:offerings"}
        data.update(overrides)
        return upsert_entry(vault or self.vault, data)[0]

    def test_rerunning_note_updates_one_record_and_preserves_client_correction(self):
        entry = self.note()
        edit_client_entry(self.vault, entry.pk, "Correct price: 750")
        repeated = self.note(body="New transcript says price: 550")
        self.assertEqual(repeated.pk, entry.pk)
        self.assertEqual(self.vault.entries.count(), 1)
        self.assertEqual(repeated.body, "New transcript says price: 550")
        self.assertEqual(repeated.effective_body, "Correct price: 750")
        self.assertEqual(repeated.client_body, "Correct price: 750")
        self.assertGreaterEqual(repeated.revisions.count(), 2)
        self.assertIn("Price: 500", list(repeated.revisions.values_list("body", flat=True)))
        markdown = export_markdown(self.vault)
        self.assertIn("Correct price: 750", markdown)
        self.assertIn("New transcript says price: 550", markdown)

    def test_client_confirmation_is_recorded_and_underlying_source_is_retained(self):
        entry = self.note()
        confirm_entry(self.vault, entry.pk)
        entry.refresh_from_db()
        self.assertIsNotNone(entry.confirmed_at)
        self.assertEqual(entry.author_type, "agent")
        self.assertEqual(entry.origin, "fireflies")
        self.assertEqual(entry.body, "Price: 500")
        self.assertIn("call", entry.source_label.lower())

    def test_external_ids_are_scoped_to_vault_and_author(self):
        first = self.note()
        other = self.note(self.other_vault, body="Beta private content")
        client, _ = upsert_entry(self.vault, {"section": "offerings", "body": "Customer note", "external_id": first.external_id}, author_type="client")
        self.assertNotEqual(first.pk, other.pk)
        self.assertNotEqual(first.pk, client.pk)
        self.assertEqual(self.vault.entries.count(), 2)
        self.assertNotIn("Beta private content", export_markdown(self.vault))

    def test_cross_tenant_mutations_fail_without_changing_other_vault(self):
        entry = self.note(self.other_vault)
        question, _ = upsert_question(self.other_vault, {"text": "Private question", "section": "other"})
        for action in [
            lambda: edit_client_entry(self.vault, entry.pk, "intrusion"),
            lambda: confirm_entry(self.vault, entry.pk),
            lambda: delete_entry(self.vault, entry.pk),
            lambda: answer_question(self.vault, question.pk, "intrusion"),
        ]:
            with self.subTest(action=action):
                with self.assertRaises((ValidationError, ObjectDoesNotExist)):
                    action()
        entry.refresh_from_db()
        question.refresh_from_db()
        self.assertEqual(entry.effective_body, "Price: 500")
        self.assertEqual(question.answer, "")

    def test_stale_client_edit_is_rejected_instead_of_overwriting_newer_fact(self):
        entry = self.note()
        old_timestamp = entry.updated_at.isoformat()
        edit_client_entry(self.vault, entry.pk, "Newest client fact", expected_updated_at=old_timestamp)
        with self.assertRaises(VaultConflict):
            edit_client_entry(self.vault, entry.pk, "Stale tab fact", expected_updated_at=old_timestamp)
        entry.refresh_from_db()
        self.assertEqual(entry.effective_body, "Newest client fact")

    def test_question_upsert_preserves_customer_answer(self):
        data = {"text": "What time do you open?", "section": "basics", "external_id": "call-123:q:1"}
        question, _ = upsert_question(self.vault, data)
        answer_question(self.vault, question.pk, "10 am")
        repeated, _ = upsert_question(self.vault, data)
        with self.assertRaises(VaultConflict):
            upsert_question(self.vault, {**data, "text": "What are your opening hours?"})
        self.assertEqual(repeated.pk, question.pk)
        self.assertEqual(self.vault.questions.count(), 1)
        self.assertEqual(repeated.answer, "10 am")
        self.assertIsNotNone(repeated.answered_at)
        self.assertIn("10 am", export_markdown(self.vault))

    def test_call_upsert_and_private_recording_visibility(self):
        data = {"title": "Brainstorming", "date": "2026-10-05", "external_id": "call-123", "url": "https://example.test/private-recording", "share_recording": False, "attendees": ["Customer", "Operator"], "summary": "Discussed the offering."}
        call, _ = upsert_call(self.vault, data)
        repeated, _ = upsert_call(self.vault, {**data, "summary": "Corrected call summary."})
        self.assertEqual(repeated.pk, call.pk)
        self.assertEqual(self.vault.calls.count(), 1)
        self.assertIn("Corrected call summary.", json.dumps(workspace_payload(self.vault), default=str))
        self.assertNotIn(data["url"], json.dumps(workspace_payload(self.vault, purpose="client"), default=str))

    def test_profile_orders_client_corrections_answers_and_confirmed_sources_before_unconfirmed_notes(self):
        self.note(body="Unconfirmed source", external_id="source:1")
        confirmed = self.note(body="Confirmed source", external_id="source:2")
        confirm_entry(self.vault, confirmed.pk)
        client, _ = upsert_entry(self.vault, {"section": "offerings", "body": "Direct client fact"}, author_type="client")
        corrected = self.note(body="Wrong source", external_id="source:3")
        edit_client_entry(self.vault, corrected.pk, "Client corrected fact")
        question, _ = upsert_question(self.vault, {"section": "offerings", "text": "What is your price?"})
        answer_question(self.vault, question.pk, "Client answered price")
        snapshot = create_profile_snapshot(self.vault)
        facts = snapshot.body["facts"]
        by_body = {fact["body"]: fact for fact in facts}
        self.assertEqual(facts[0]["body"], "Client corrected fact")
        self.assertLess(by_body["Direct client fact"]["precedence"], by_body["Confirmed source"]["precedence"])
        self.assertEqual(by_body["Client answered price"]["precedence"], by_body["Direct client fact"]["precedence"])
        self.assertLess(by_body["Confirmed source"]["precedence"], by_body["Unconfirmed source"]["precedence"])
        self.assertNotIn("Wrong source", by_body)
        self.assertTrue(snapshot.body["review_required"])
        self.assertFalse(snapshot.body["live_configuration_updated"])

    def test_sections_reject_unknown_keys_and_track_explicit_no_material(self):
        with self.assertRaises(ValidationError):
            self.note(section="foreign_section")
        set_section_state(self.vault, "proof", state="dont_have", is_done=True)
        section = self.vault.sections.get(key="proof")
        self.assertEqual(section.state, VaultSection.State.DONT_HAVE)
        self.assertTrue(section.is_done)
        self.assertIn("proof", json.dumps(workspace_payload(self.vault), default=str))

    def test_javascript_urls_are_rejected(self):
        with self.assertRaises(ValidationError):
            upsert_entry(self.vault, {"section": "website", "kind": "link", "url": "javascript:alert(document.cookie)"}, author_type="client")
        self.assertEqual(self.vault.entries.count(), 0)


class VaultUploadTests(VaultTestCase):
    def upload(self, filename="brochure.pdf", content=b"%PDF-1.7\nprivate customer brochure\n", content_type="application/pdf", vault=None):
        return upsert_entry(
            vault or self.vault,
            {"section": "brochures", "kind": "file", "body": "Customer brochure"},
            author_type="client",
            upload=SimpleUploadedFile(filename, content, content_type=content_type),
        )[0]

    def test_file_storage_is_private_randomized_and_metered(self):
        entry = self.upload()
        self.vault.refresh_from_db()
        self.assertEqual(self.vault.storage_used_bytes, entry.file_size)
        self.assertGreater(entry.file_size, 0)
        self.assertNotIn("brochure.pdf", entry.file.name)
        self.assertTrue(entry.file.storage.exists(entry.file.name))
        self.assertTrue(Path(entry.file.path).is_relative_to(self.storage.name))
        self.assertNotIn(b"private customer brochure", Path(entry.file.path).read_bytes())
        self.assertEqual(entry.file.open("rb").read(), b"%PDF-1.7\nprivate customer brochure\n")
        with self.assertRaises(ValueError):
            _ = entry.file.url

    def test_delete_releases_quota_and_deletes_private_file_after_commit(self):
        entry = self.upload()
        file_path = entry.file.path
        with self.captureOnCommitCallbacks(execute=True):
            delete_entry(self.vault, entry.pk)
        self.vault.refresh_from_db()
        self.assertEqual(self.vault.storage_used_bytes, 0)
        self.assertFalse(Path(file_path).exists())

    def test_over_quota_upload_leaves_no_record_or_storage_usage(self):
        self.vault.storage_quota_bytes = 4
        self.vault.save(update_fields=["storage_quota_bytes"])
        with self.assertRaises(ValidationError):
            self.upload()
        self.vault.refresh_from_db()
        self.assertEqual(self.vault.storage_used_bytes, 0)
        self.assertEqual(self.vault.entries.count(), 0)
        self.assertEqual([p for p in Path(self.storage.name).rglob("*") if p.is_file()], [])

    @override_settings(VAULT_MAX_FILE_BYTES=8)
    def test_configured_per_file_limit_rejects_large_upload_without_quota_charge(self):
        with self.assertRaises(ValidationError):
            self.upload()
        self.vault.refresh_from_db()
        self.assertEqual(self.vault.storage_used_bytes, 0)
        self.assertEqual(self.vault.entries.count(), 0)

    def test_active_file_types_and_mismatched_content_are_rejected(self):
        for name, content, mime in [
            ("attack.html", b"<script>alert(1)</script>", "text/html"),
            ("attack.svg", b"<svg onload='alert(1)'></svg>", "image/svg+xml"),
            ("attack.exe", b"MZexecutable", "application/octet-stream"),
            ("fake.pdf", b"<html>not a PDF</html>", "application/pdf"),
        ]:
            with self.subTest(filename=name), self.assertRaises(ValidationError):
                self.upload(name, content, mime)
        self.assertEqual(self.vault.entries.count(), 0)

    def test_untrusted_filename_cannot_escape_private_vault_directory(self):
        entry = self.upload(filename="../../outside.pdf")
        self.assertNotIn("..", entry.file.name)
        self.assertTrue(Path(entry.file.path).is_relative_to(self.storage.name))

    def test_audio_has_truthful_transcription_status(self):
        audio = SimpleUploadedFile("note.webm", b"\x1aE\xdf\xa3test audio", content_type="audio/webm")
        entry, _ = upsert_entry(self.vault, {"section": "other", "kind": "audio"}, author_type="client", upload=audio)
        self.assertEqual(entry.transcription_status, "unavailable")
        self.assertEqual(entry.effective_body, "")
