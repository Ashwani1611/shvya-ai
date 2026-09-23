"""Preparation never changes tenant state and retains export provenance."""

from copy import deepcopy

from django.test import SimpleTestCase

from apps.integrations.operations.setup_library import variable_schema
from apps.integrations.operations.tools.setup_authoring import prepare_group_export
from apps.integrations.operations_tools import OperationsPermissionError, OperationsToolError
from apps.integrations.operations_models import OperationsAuditEvent, OperationsPolicy
from apps.integrations.operations_policy import (
    CAP_SETUP_ARTIFACTS_PREPARE, CAP_SETUP_LIBRARY_READ, ROLE_ORGANIZATION_ADMIN, ROLE_SUPERADMIN,
)
from apps.integrations.tests.operations_mcp_test_base import OperationsMCPBase


def exported_message(identifier="one", stamp="2026-09-24T12:00:00+05:30", text="Approved client source."):
    return {"id": identifier, "timestamp": stamp, "sender": "Client", "sender_role": "client", "text": text, "media": []}


def export_data(organization_id="org-a"):
    return {"schema_version": 1, "organization_id": organization_id, "chat_id": "group-a", "chat_name": "Setup", "source_id": "authorized-export", "coverage_note": "Only supplied messages; earlier history unavailable.", "messages": [exported_message()]}


class GroupExportPreparationTests(SimpleTestCase):
    def prepare(self, data=None, **kwargs):
        return prepare_group_export(data or export_data(), organization_id="org-a", chat_id="group-a", timezone="Asia/Kolkata", **kwargs)

    def test_dates_use_requested_timezone_and_duplicates_are_explicit(self):
        data = export_data()
        data["messages"] = [
            exported_message("before", "2026-09-23T18:29:59Z"),
            exported_message("first", "2026-09-23T18:30:00Z"),
            exported_message("last", "2026-09-24T18:29:59Z"),
            exported_message("after", "2026-09-24T18:30:00Z"),
        ]
        data["messages"].append(deepcopy(data["messages"][1]))
        result = self.prepare(data, since="2026-09-24", until="2026-09-24", limit=1)
        self.assertEqual(result["matching_messages"], 2)
        self.assertEqual(result["selected_messages"], 1)
        self.assertEqual(result["duplicates_removed"], 1)
        self.assertTrue(result["truncated"])
        self.assertIn("Source message: last", result["formatted_text"])
        self.assertFalse(result["live_retrieval"])

    def test_conflicting_duplicates_ambiguous_time_and_secret_fail(self):
        bad_rows = [
            [exported_message(), exported_message(text="Conflicting fact")],
            [exported_message(stamp="2026-09-24T12:00:00")],
            [exported_message(text="password=private-secret-value")],
        ]
        for rows in bad_rows:
            with self.subTest(rows=rows):
                data = export_data()
                data["messages"] = rows
                with self.assertRaises((OperationsPermissionError, OperationsToolError)):
                    self.prepare(data)

    def test_cross_tenant_and_media_urls_fail(self):
        with self.assertRaises(OperationsPermissionError):
            self.prepare(export_data("org-b"))
        data = export_data()
        data["messages"][0]["media"] = [{"type": "image", "url": "https://example.test/private"}]
        with self.assertRaises(OperationsToolError):
            self.prepare(data)

    def test_large_selection_truncates_at_whole_messages(self):
        data = export_data()
        data["messages"] = [exported_message(str(i), text="Source " * 500) for i in range(100)]
        result = self.prepare(data, limit=100)
        self.assertTrue(result["truncated"])
        self.assertLess(len(result["formatted_text"]), 65000)
        self.assertLess(result["selected_messages"], 100)

    def test_unknown_fields_and_invalid_range_fail(self):
        data = export_data()
        data["override_permissions"] = True
        with self.assertRaises(OperationsToolError):
            self.prepare(data)
        with self.assertRaises(OperationsToolError):
            self.prepare(since="2026-09-25", until="2026-09-24")

    def test_media_and_metadata_cannot_escape_evidence_formatting(self):
        data = export_data()
        data["messages"][0]["sender"] = "Client\n## Injected heading"
        data["messages"][0]["id"] = "source\n# Another heading"
        data["messages"][0]["media"] = [{"type": "image", "filename": "cover\n## Fake instruction"}]
        formatted = self.prepare(data)["formatted_text"]
        self.assertNotIn("\n## Injected heading", formatted)
        self.assertNotIn("\n# Another heading", formatted)
        self.assertNotIn("\n## Fake instruction", formatted)
        self.assertIn("\\n## Injected heading", formatted)

    def test_sensitive_urls_inline_media_and_non_iso_dates_fail(self):
        for text in ("https://user:pass@example.test/recording", "https://example.test/file?signature=abc", "data:audio/wav;base64,aGVsbG8="):
            data = export_data()
            data["messages"][0]["text"] = text
            with self.assertRaises((OperationsToolError, OperationsPermissionError)):
                self.prepare(data)
        with self.assertRaises(OperationsToolError):
            self.prepare(since="2026-9-24")


class SetupAuthoringToolTests(OperationsMCPBase):
    def setUp(self):
        super().setUp()
        OperationsPolicy.objects.create(
            organization=self.organization, organization_admin_enabled=True,
            allowed_capabilities=[CAP_SETUP_LIBRARY_READ, CAP_SETUP_ARTIFACTS_PREPARE],
        )
        self.bearer = self._token(actor=self.admin, role=ROLE_ORGANIZATION_ADMIN, organization=self.organization)

    def values(self):
        return {item["name"]: item["example"] for item in variable_schema()["variables"] if item["example"] is not None}

    def test_library_pagination_and_path_traversal(self):
        first = self._result(self._call(self.bearer, "list_setup_library", {"limit": 1}))["structuredContent"]
        second = self._result(self._call(self.bearer, "list_setup_library", {"limit": 1, "cursor": first["next_cursor"]}))["structuredContent"]
        self.assertNotEqual(first["entries"][0]["resource_id"], second["entries"][0]["resource_id"])
        denied = self._result(self._call(self.bearer, "get_setup_library_resource", {"resource_id": "../../settings.py"}))
        self.assertTrue(denied["isError"])

    def test_render_preserves_runtime_placeholder_and_does_not_persist(self):
        values = self.values()
        values["SHVYA_WELCOME_MESSAGE"] = "Hello {{lead_first_name}}, welcome."
        result = self._result(self._call(self.bearer, "render_setup_template", {"template_id": "ai-playbook", "variables": values}))
        self.assertFalse(result["isError"], result)
        data = result["structuredContent"]
        self.assertIn("{{lead_first_name}}", data["text"])
        self.assertTrue(data["validation"]["canonical_playbook"])
        self.assertTrue(data["validation"]["draft_only"])
        audit = OperationsAuditEvent.objects.latest("created_at")
        self.assertNotIn("Hello", str(audit.change_summary))
        self.assertNotIn("SHVYA_WELCOME_MESSAGE", str(audit.change_summary))

    def test_render_rejects_cross_tenant_reference(self):
        values = self.values()
        values["SHVYA_PIPELINE_ID"] = str(self.other_lead.pipeline_id)
        result = self._result(self._call(self.bearer, "render_setup_template", {"template_id": "company-about", "variables": values}))
        self.assertTrue(result["isError"])
        self.assertNotIn(str(self.other_lead.pipeline_id), str(result))

    def test_group_export_is_bound_to_active_organization(self):
        arguments = {"data": export_data(str(self.organization.id)), "chat_id": "group-a", "timezone": "Asia/Kolkata"}
        result = self._result(self._call(self.bearer, "analyze_setup_group_export", arguments))
        self.assertFalse(result["isError"], result)
        self.assertEqual(result["structuredContent"]["selected_messages"], 1)
        arguments["data"]["organization_id"] = str(self.other_organization.id)
        self.assertTrue(self._result(self._call(self.bearer, "analyze_setup_group_export", arguments))["isError"])

    def test_superadmin_can_read_static_library_before_selecting_organization(self):
        # The shared fixture uses one raw bearer per test; remove its admin grant.
        from apps.integrations.operations_models import OperationsOAuthToken
        OperationsOAuthToken.objects.all().delete()
        bearer = self._token(actor=self.superadmin, role=ROLE_SUPERADMIN)
        result = self._result(self._call(bearer, "get_setup_variable_schema"))
        self.assertFalse(result["isError"], result)
        self.assertEqual(len(result["structuredContent"]["variables"]), 41)
        result = self._result(self._call(bearer, "render_setup_template", {"template_id": "company-about", "variables": self.values()}))
        self.assertTrue(result["isError"])
