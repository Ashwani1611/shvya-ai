"""Intake evidence must retain Operations tenant, consent and approval boundaries."""

import json
import uuid
from unittest.mock import patch

from django.db import IntegrityError, transaction

from apps.integrations.models import (
    OperationsAuditEvent,
    OperationsIntakeEntry,
    OperationsOAuthToken,
    OperationsPolicy,
)
from apps.integrations.operations.tools import setup_intake
from apps.integrations.operations_policy import (
    CAP_SETUP_INTAKE_READ,
    CAP_SETUP_INTAKE_WRITE,
    ROLE_ORGANIZATION_ADMIN,
)
from apps.integrations.tests.operations_mcp_test_base import OperationsMCPBase


class OperationsSetupIntakeTests(OperationsMCPBase):
    def setUp(self):
        super().setUp()
        self.policy = OperationsPolicy.objects.create(
            organization=self.organization,
            organization_admin_enabled=True,
            allowed_capabilities=[CAP_SETUP_INTAKE_READ, CAP_SETUP_INTAKE_WRITE],
            approval_required_capabilities=[CAP_SETUP_INTAKE_WRITE],
        )
        self.bearer = self._token(
            actor=self.admin, role=ROLE_ORGANIZATION_ADMIN, organization=self.organization,
        )
        self.data = {
            "kind": "note", "section": "basics", "external_id": "brief-v1-hours",
            "origin": "client_document", "source_id": "brief-v1", "source_ref": "Brief page 2",
            "source_date": "2026-09-20", "status": "reported",
            "body": "Private onboarding evidence: business hours are 9am to 5pm.",
        }
        self.reason = "Record the supplied onboarding brief for company setup review."

    def _ok(self, name, arguments=None):
        result = self._result(self._call(self.bearer, name, arguments))
        self.assertFalse(result["isError"], result)
        return result["structuredContent"]

    def _error(self, name, arguments, code=None):
        result = self._result(self._call(self.bearer, name, arguments))
        self.assertTrue(result["isError"], result)
        if code:
            self.assertEqual(result["structuredContent"]["error_code"], code)
        return result

    def _approved(self, name, arguments):
        preview = self._ok(name, {**arguments, "dry_run": True})
        self.assertEqual(preview["status"], "DRY_RUN")
        return self._ok(name, {**arguments, "dry_run": False, "approved": True,
                               "approval_event_id": preview["approval_event_id"]})

    def _create(self, **data):
        return self._approved("upsert_setup_intake_entry", {
            "data": {**self.data, **data}, "reason": self.reason,
        })["entry"]

    def test_dry_run_and_approved_creation_do_not_publish_knowledge(self):
        arguments = {"data": self.data, "reason": self.reason}
        preview = self._ok("upsert_setup_intake_entry", arguments)
        self.assertTrue(preview["approval_required"])
        self.assertEqual(OperationsIntakeEntry.objects.count(), 0)
        self._error("upsert_setup_intake_entry", {**arguments, "dry_run": False})
        created = self._ok("upsert_setup_intake_entry", {
            **arguments, "dry_run": False, "approved": True, "approval_event_id": preview["approval_event_id"],
        })
        self.assertEqual(created["entry"]["revision"], 1)
        self.assertFalse(created["automatic_publication"])
        row = OperationsIntakeEntry.objects.get()
        self.assertEqual(row.organization_id, self.organization.id)
        self.assertEqual(row.created_by_id, self.admin.id)
        self.assertEqual(row.history, [])
        from apps.ai_engagement.models import FAQ, KnowledgeSource
        self.assertFalse(FAQ.objects.filter(organization=self.organization).exists())
        self.assertFalse(KnowledgeSource.objects.filter(organization=self.organization).exists())

    def test_upsert_is_idempotent_by_stable_source_identity(self):
        first = self._create()
        second = self._approved("upsert_setup_intake_entry", {"data": self.data, "reason": self.reason})
        self.assertEqual(second["status"], "UNCHANGED")
        self.assertEqual(second["entry"]["id"], first["id"])
        self.assertEqual(second["entry"]["revision"], 1)
        self.assertEqual(OperationsIntakeEntry.objects.count(), 1)

    def test_unique_identity_is_enforced_by_database_and_scoped_to_organization(self):
        self._create()
        with self.assertRaises(IntegrityError), transaction.atomic():
            OperationsIntakeEntry.objects.create(organization=self.organization, **self.data)
        other = OperationsIntakeEntry.objects.create(organization=self.other_organization, **self.data)
        self.assertEqual(other.revision, 1)

    def test_reads_and_writes_reject_foreign_ids_without_disclosing_existence(self):
        other = OperationsIntakeEntry.objects.create(organization=self.other_organization, **self.data)
        current = self._create()
        listed = self._ok("get_setup_intake")
        self.assertEqual([row["id"] for row in listed["entries"]], [current["id"]])
        for name, args in (
            ("get_setup_intake", {}),
            ("upsert_setup_intake_entry", {"data": self.data, "reason": self.reason, "expected_revision": 1}),
            ("archive_setup_intake_entry", {"reason": self.reason, "expected_revision": 1}),
        ):
            foreign = self._error(name, {**args, "entry_id": str(other.id)})
            missing = self._error(name, {**args, "entry_id": str(uuid.uuid4())})
            self.assertEqual(foreign["content"], missing["content"])
        other.refresh_from_db()
        self.assertTrue(other.is_active)
        self.assertEqual(other.revision, 1)

    def test_capability_removal_and_old_grant_do_not_gain_intake_access(self):
        self.policy.allowed_capabilities = [CAP_SETUP_INTAKE_READ]
        self.policy.save(update_fields=["allowed_capabilities"])
        self._error("upsert_setup_intake_entry", {"data": self.data, "reason": self.reason})
        self.policy.allowed_capabilities = [CAP_SETUP_INTAKE_READ, CAP_SETUP_INTAKE_WRITE]
        self.policy.save(update_fields=["allowed_capabilities"])
        OperationsOAuthToken.objects.filter(actor=self.admin).update(granted_capabilities=[])
        self._error("get_setup_intake", {})
        self._error("upsert_setup_intake_entry", {"data": self.data, "reason": self.reason})
        self.assertFalse(OperationsIntakeEntry.objects.exists())

    def test_changes_require_revision_and_preserve_previous_source_evidence(self):
        row = self._create()
        arguments = {"entry_id": row["id"], "data": {**self.data, "body": "Corrected business hours are 10am to 5pm."},
                     "reason": self.reason}
        self._error("upsert_setup_intake_entry", arguments)
        self._error("upsert_setup_intake_entry", {**arguments, "expected_revision": 2})
        changed = self._approved("upsert_setup_intake_entry", {**arguments, "expected_revision": 1})
        self.assertEqual(changed["entry"]["revision"], 2)
        saved = OperationsIntakeEntry.objects.get()
        self.assertEqual(saved.history[0]["data"]["body"], self.data["body"])
        self.assertEqual(saved.history[0]["data"]["source_ref"], "Brief page 2")
        self.assertEqual(saved.history[0]["revision"], 1)
        self.assertNotIn("history", changed["entry"])

    def test_stale_approval_receipt_cannot_overwrite_changed_content(self):
        entry = self._create()
        arguments = {"entry_id": entry["id"], "expected_revision": 1,
                     "data": {**self.data, "body": "Proposed new hours."}, "reason": self.reason}
        preview = self._ok("upsert_setup_intake_entry", arguments)
        OperationsIntakeEntry.objects.filter(pk=entry["id"]).update(body="A newer confirmed source.", revision=2)
        self._error("upsert_setup_intake_entry", {
            **arguments, "dry_run": False, "approved": True, "approval_event_id": preview["approval_event_id"],
        })
        self.assertEqual(OperationsIntakeEntry.objects.get().body, "A newer confirmed source.")

    def test_locked_proposal_is_rechecked_after_initial_validation(self):
        entry = self._create()
        arguments = {"entry_id": entry["id"], "expected_revision": 1,
                     "data": {**self.data, "body": "Proposed new hours."}, "reason": self.reason}
        preview = self._ok("upsert_setup_intake_entry", arguments)
        original = setup_intake._ensure_approved_proposal_unchanged
        calls = []

        def intervene(**kwargs):
            original(**kwargs)
            calls.append(True)
            if len(calls) == 1:
                OperationsIntakeEntry.objects.filter(pk=entry["id"]).update(body="Changed during execution.")

        with patch.object(setup_intake, "_ensure_approved_proposal_unchanged", side_effect=intervene):
            self._error("upsert_setup_intake_entry", {
                **arguments, "dry_run": False, "approved": True, "approval_event_id": preview["approval_event_id"],
            })
        self.assertEqual(OperationsIntakeEntry.objects.get().body, "Changed during execution.")

    def test_receipt_cannot_be_reused_to_repeat_execution(self):
        arguments = {"data": self.data, "reason": self.reason}
        preview = self._ok("upsert_setup_intake_entry", arguments)
        execution = {**arguments, "dry_run": False, "approved": True,
                     "approval_event_id": preview["approval_event_id"]}
        self._ok("upsert_setup_intake_entry", execution)
        self._error("upsert_setup_intake_entry", execution)
        self.assertEqual(OperationsIntakeEntry.objects.count(), 1)

    def test_archive_preserves_history_requires_revision_and_can_be_restored(self):
        entry = self._create()
        args = {"entry_id": entry["id"], "expected_revision": 1, "reason": self.reason}
        archived = self._approved("archive_setup_intake_entry", args)
        self.assertEqual(archived["status"], "ARCHIVED")
        self.assertFalse(archived["entry"]["is_active"])
        self.assertEqual(self._ok("get_setup_intake")["count"], 0)
        found = self._ok("get_setup_intake", {"entry_id": entry["id"], "include_archived": True, "include_history": True})
        self.assertEqual(found["entries"][0]["history"][0]["data"]["is_active"], True)
        self._error("archive_setup_intake_entry", args)
        restored = self._approved("upsert_setup_intake_entry", {
            "entry_id": entry["id"], "expected_revision": 2, "data": self.data, "reason": self.reason,
        })
        self.assertTrue(restored["entry"]["is_active"])
        self.assertEqual(restored["entry"]["revision"], 3)

    def test_history_storage_and_read_are_bounded(self):
        entry = self._create()
        self.policy.approval_required_capabilities = []
        self.policy.save(update_fields=["approval_required_capabilities"])
        for revision in range(1, 13):
            self._ok("upsert_setup_intake_entry", {
                "entry_id": entry["id"], "expected_revision": revision,
                "data": {**self.data, "body": f"Source correction {revision}."},
                "reason": self.reason, "dry_run": False,
            })
        row = OperationsIntakeEntry.objects.get()
        self.assertEqual(len(row.history), 10)
        self.assertEqual(row.history[0]["revision"], 3)
        found = self._ok("get_setup_intake", {"entry_id": entry["id"], "include_history": True})["entries"][0]
        self.assertEqual(found["previous_revision_count"], 12)
        self.assertEqual(found["retained_revision_count"], 10)
        self.assertEqual(len(found["history"]), 5)
        self.assertTrue(found["history_truncated"])
        self._error("get_setup_intake", {"include_history": True})

    def test_keyset_pagination_body_limits_and_section_inventory(self):
        long_body = "Business evidence. " * 500
        entries = [OperationsIntakeEntry.objects.create(
            organization=self.organization, **{**self.data, "body": long_body, "external_id": f"source-{index}"},
        ) for index in range(3)]
        first = self._ok("get_setup_intake", {"limit": 2})
        self.assertEqual(len(first["sections"]), 15)
        self.assertEqual(first["count"], 2)
        self.assertTrue(first["entries"][0]["body_truncated"])
        self.assertLessEqual(len(first["entries"][0]["body"]), 4001)
        second = self._ok("get_setup_intake", {"limit": 2, "cursor": first["next_cursor"]})
        self.assertIsNone(second["next_cursor"])
        self.assertEqual({row["id"] for row in first["entries"] + second["entries"]}, {str(row.id) for row in entries})
        single = self._ok("get_setup_intake", {"entry_id": str(entries[0].id)})["entries"][0]
        self.assertFalse(single["body_truncated"])
        self.assertEqual(single["body"], long_body)

    def test_strict_bounds_and_secret_or_embedded_media_rejection(self):
        for changed in (
            {"body": "password=secret-example"},
            {"source_ref": "https://example.test/file?signature=signed-reference"},
            {"body": "https://alice:example@example.test/source"},
            {"source_ref": "s3://bucket/source?signature=provider-reference"},
            {"body": "data:audio/wav;base64,aGVsbG8="},
            {"body": "a " * 6001},
            {"body": {"unexpected": "object"}},
            {"source_date": "2026-99-20"},
            {"section": "unknown"},
            {"organization_id": str(self.other_organization.id)},
        ):
            with self.subTest(changed=tuple(changed)):
                self._error("upsert_setup_intake_entry", {"data": {**self.data, **changed}, "reason": self.reason})
        self._error("upsert_setup_intake_entry", {"data": self.data, "reason": self.reason, "organization_id": str(self.other_organization.id)})
        for arguments in ({"limit": 51}, {"limit": True}, {"include_archived": "true"}, {"cursor": ""}, {"entry_id": ""}):
            self._error("get_setup_intake", arguments)
        self.assertFalse(OperationsIntakeEntry.objects.exists())

    def test_audit_persists_metadata_only_for_reads_and_writes(self):
        entry = self._create()
        self._ok("get_setup_intake", {"entry_id": entry["id"]})
        events = OperationsAuditEvent.objects.filter(tool_name__in=["get_setup_intake", "upsert_setup_intake_entry"])
        self.assertGreaterEqual(events.count(), 3)
        for event in events:
            encoded = json.dumps(event.change_summary)
            for raw in (self.data["body"], self.data["source_ref"], self.data["source_id"]):
                self.assertNotIn(raw, encoded)
            self.assertNotIn("body", event.change_summary)
            self.assertEqual(event.organization_id, self.organization.id)
        self.assertTrue(events.filter(change_summary__has_key="proposal_digest").exists())
