"""Pure artifact checks plus the real MCP authorization/transport boundary."""
from copy import deepcopy
from hashlib import sha256
from html.parser import HTMLParser

from django.test import SimpleTestCase

from apps.crm.models import Lead
from apps.integrations.operations.handover_rendering import (
    HandoverValidationError, SECTION_TITLES, render_handover,
)
from apps.integrations.operations.response_policy import sanitize_tool_response
from apps.integrations.operations_models import OperationsAuditEvent, OperationsPolicy
from apps.integrations.operations_policy import (
    CAP_SETUP_ARTIFACTS_PREPARE, CAP_SETUP_LIBRARY_READ, ROLE_ORGANIZATION_ADMIN,
)
from apps.integrations.tests.operations_mcp_test_base import OperationsMCPBase


def document(organization_id="org-a"):
    return {
        "schema_version": 1, "organization_id": organization_id,
        "environment": "test", "evidence_date": "2026-10-11T12:00:00+05:30",
        "timezone": "Asia/Kolkata",
        "readiness": {
            "configuration": "unknown", "engine_test": "not_tested",
            "live_delivery": "not_tested", "evidence": ["configuration"],
            "note": "No runtime tests reported.",
        },
        "sources": [{
            "id": "configuration", "tool": "get_organization_configuration",
            "reference": "Fixture only.", "observed_at": "2026-10-11T12:00:00+05:30",
            "status": "unavailable", "note": "Unknown is not absent.",
        }],
        "sections": {
            name: [{"type": "paragraph", "text": "Not inspected.", "evidence": ["configuration"]}]
            for name in SECTION_TITLES
        },
    }


def journey():
    return {
        "type": "journey", "title": "Sales journey", "pipeline": "Sales",
        "stages": [
            {"id": "new", "label": "New", "state": "ai"},
            {"id": "human", "label": "Human review", "state": "human"},
        ],
        "transitions": [{
            "from": "new", "to": "human", "kind": "automatic",
            "condition": "A supplied handoff rule", "evidence": ["configuration"],
        }],
        "evidence": ["configuration"],
    }


def cadence():
    return {
        "type": "cadence", "title": "Follow-up", "channel": "Unverified",
        "start": "Reported stage entry", "stop": "Unverified opt-out protection",
        "end": "Unverified", "schedule": "Unverified", "pause": "Unverified",
        "delay_basis": "after_previous",
        "steps": [
            {"delay": {"value": 30, "unit": "minutes"}, "message": "First", "evidence": ["configuration"]},
            {"delay": {"value": 1, "unit": "days"}, "message": "Second", "evidence": ["configuration"]},
            {"delay": {"value": 2, "unit": "days"}, "message": "Third", "evidence": ["configuration"]},
        ],
        "evidence": ["configuration"],
    }


class Tags(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags, self.attrs = [], []

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        self.attrs.extend(attrs)


class HandoverRenderingTests(SimpleTestCase):
    def render(self, value=None, **kwargs):
        return render_handover(
            value if value is not None else document(),
            organization_id="org-a", organization_name="Example Company", **kwargs,
        )

    def assemble(self, value=None, **kwargs):
        chunks, offset, digest = [], 0, None
        while True:
            result = self.render(value, offset=offset, **kwargs)
            self.assertEqual(result["offset"], offset)
            self.assertEqual(result["returned_chars"], len(result["text"]))
            self.assertEqual(result["chunk_sha256"], sha256(result["text"].encode()).hexdigest())
            if digest is not None:
                self.assertEqual(result["artifact_sha256"], digest)
            digest = result["artifact_sha256"]
            chunks.append(result["text"])
            if result["next_offset"] is None:
                break
            offset = result["next_offset"]
        full = "".join(chunks)
        self.assertEqual(len(full), result["total_chars"])
        self.assertEqual(sha256(full.encode()).hexdigest(), digest)
        return full, result

    def test_lossless_chunks_unicode_and_shared_document_digest(self):
        value = document()
        value["sections"]["overview"][0]["text"] = "हैलो traveller 🙂\nSecond line."
        for format in ("html", "markdown"):
            small, metadata = self.assemble(value, format=format, limit=137)
            large, other = self.assemble(value, format=format, limit=20000)
            self.assertEqual(small, large)
            self.assertEqual(metadata["artifact_sha256"], other["artifact_sha256"])
            self.assertEqual(metadata["template_version"], "account-handover-v1")
            self.assertFalse(metadata["validation"]["evidence_independently_verified"])
            self.assertTrue(metadata["validation"]["script_only"])
            end = self.render(value, format=format, offset=len(small))
            self.assertEqual(end["text"], "")
            self.assertIsNone(end["next_offset"])
        self.assertEqual(
            self.render(value, format="html")["document_sha256"],
            self.render(value, format="markdown")["document_sha256"],
        )

    def test_literal_html_mermaid_and_cumulative_cadence(self):
        value = document()
        value["sections"]["overview"][0]["text"] = '<script>alert("x")</script><img src="https://example.test/pixel" onerror="alert(1)">'
        graph = journey()
        graph["stages"][0]["label"] = 'quote"]\n\x60\x60\x60\nclick s0 "https://example.test"'
        value["sections"]["flow"] = [graph]
        value["sections"]["automation"] = [cadence()]
        output, unused = self.assemble(value)
        tags = Tags()
        tags.feed(output)
        self.assertNotIn("script", tags.tags)
        self.assertNotIn("img", tags.tags)
        self.assertFalse(any(name.startswith("on") for name, unused in tags.attrs))
        self.assertIn("&lt;script&gt;", output)
        self.assertIn('href="#source-configuration"', output)
        self.assertIn("Content-Security-Policy", output)
        markdown, unused = self.assemble(value, format="markdown")
        self.assertEqual(markdown.count("\x60\x60\x60"), 2)
        self.assertNotIn('\nclick s0 "https://example.test"', markdown)
        self.assertIn("#34;", markdown)
        self.assertIn("3 days 30 minutes", markdown)
        self.assertIn("not delivery promises", markdown)

    def test_secrets_are_rejected_in_paragraph_source_and_server_title(self):
        secrets = (
            "password=private-value", "Bearer abcdefghijklmnopqrst",
            '{"password":"short-pass-123"}', "{'api_key':'short-key'}",
            '"authorization" : "short-value"',
            "https://user:pass@example.test/file",
            "https://example.test/file?signature=abc",
            "data:image/png;base64,aGVsbG8=", "javascript:alert(1)",
        )
        for secret in secrets:
            for field in ("paragraph", "source", "title"):
                value = document()
                name = "Example Company"
                if field == "paragraph":
                    value["sections"]["overview"][0]["text"] = secret
                elif field == "source":
                    value["sources"][0]["note"] = secret
                else:
                    name = secret
                with self.subTest(secret=secret, field=field), self.assertRaises(HandoverValidationError) as caught:
                    render_handover(value, organization_id="org-a", organization_name=name)
                self.assertNotIn(secret, str(caught.exception))

    def test_invalid_shapes_references_and_bounds_fail_safely(self):
        variants = []
        value = document()
        value["title"] = "Forged"
        variants.append(value)
        value = document()
        del value["sections"]["flow"]
        variants.append(value)
        value = document()
        value["sections"]["flow"][0]["evidence"] = ["missing"]
        variants.append(value)
        value = document()
        value["sources"].append(deepcopy(value["sources"][0]))
        variants.append(value)
        value = document()
        value["evidence_date"] = "2026-10-11T12:00:00"
        variants.append(value)
        value = document()
        value["schema_version"] = True
        variants.append(value)
        variants.append(document("org-b"))
        for value in variants:
            with self.assertRaises(HandoverValidationError):
                self.render(value)
        for kwargs in ({"limit": True}, {"offset": True}, {"limit": 0}, {"limit": 20001}, {"offset": -1}, {"offset": 999999}):
            with self.assertRaises(HandoverValidationError):
                self.render(**kwargs)
        value = document()
        value["sections"]["overview"] = [
            {"type": "paragraph", "text": "safe word " * 700, "evidence": ["configuration"]}
            for unused in range(40)
        ]
        with self.assertRaises(HandoverValidationError):
            self.render(value)

    def test_nonstring_enums_and_stage_references_are_controlled_errors(self):
        for invalid in ([], {}):
            mutations = [
                lambda v: v["readiness"].update(configuration=invalid),
                lambda v: v["readiness"].update(engine_test=invalid),
                lambda v: v["readiness"].update(live_delivery=invalid),
                lambda v: v["sources"][0].update(status=invalid),
                lambda v: v["sections"]["flow"][0]["stages"][0].update(state=invalid),
                lambda v: v["sections"]["flow"][0]["transitions"][0].update(kind=invalid),
                lambda v: v["sections"]["flow"][0]["transitions"][0].update(**{"from": invalid}),
                lambda v: v["sections"]["flow"][0]["transitions"][0].update(to=invalid),
                lambda v: v["sections"]["automation"][0]["steps"][0]["delay"].update(unit=invalid),
            ]
            for mutate in mutations:
                value = document()
                value["sections"]["flow"] = [journey()]
                value["sections"]["automation"] = [cadence()]
                mutate(value)
                with self.assertRaises(HandoverValidationError):
                    self.render(value)
            with self.assertRaises(HandoverValidationError):
                self.render(format=invalid)

    def test_response_policy_does_not_damage_artifact_or_hashes(self):
        result = self.render()
        self.assertIs(sanitize_tool_response("render_account_handover", result), result)
        for key in ("artifact_sha256", "document_sha256", "chunk_sha256"):
            self.assertEqual(len(result[key]), 64)


class HandoverMCPTransportTests(OperationsMCPBase):
    def setUp(self):
        super().setUp()
        self.policy = OperationsPolicy.objects.create(
            organization=self.organization, organization_admin_enabled=True,
            allowed_capabilities=[CAP_SETUP_ARTIFACTS_PREPARE],
        )
        self.bearer = self._token(
            actor=self.admin, role=ROLE_ORGANIZATION_ADMIN,
            organization=self.organization,
        )

    def test_real_transport_binds_server_name_and_preserves_chunk_hash(self):
        tools = {tool["name"]: tool for tool in self._list_tools(self.bearer)["tools"]}
        self.assertTrue(tools["render_account_handover"]["annotations"]["readOnlyHint"])
        value = document(str(self.organization.id))
        before = Lead.objects.count()
        result = self._result(self._call(
            self.bearer, "render_account_handover",
            {"document": value, "format": "html", "limit": 20000},
        ))
        self.assertFalse(result["isError"], result)
        data = result["structuredContent"]
        self.assertIn(self.organization.name + ": how the account works", data["text"])
        self.assertEqual(data["organization_id"], str(self.organization.id))
        self.assertEqual(data["chunk_sha256"], sha256(data["text"].encode()).hexdigest())
        self.assertEqual(len(data["artifact_sha256"]), 64)
        self.assertEqual(Lead.objects.count(), before)
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.stage_id, self.new_stage.id)
        audit = OperationsAuditEvent.objects.latest("created_at")
        self.assertNotIn("Not inspected", str(audit.change_summary))

    def test_transport_denies_cross_tenant_unsafe_text_and_revoked_capability(self):
        values = [document(str(self.other_organization.id))]
        value = document(str(self.organization.id))
        value["sections"]["overview"][0]["text"] = '{"password":"short-pass-123"}'
        values.append(value)
        value = document(str(self.organization.id))
        value["readiness"]["engine_test"] = {}
        values.append(value)
        for value in values:
            result = self._result(self._call(self.bearer, "render_account_handover", {"document": value}))
            self.assertTrue(result["isError"], result)
            self.assertNotIn("short-pass-123", str(result))
        self.policy.allowed_capabilities = [CAP_SETUP_LIBRARY_READ]
        self.policy.save(update_fields=["allowed_capabilities"])
        result = self._result(self._call(
            self.bearer, "render_account_handover",
            {"document": document(str(self.organization.id))},
        ))
        self.assertTrue(result["isError"], result)
