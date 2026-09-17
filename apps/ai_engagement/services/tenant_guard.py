from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable


TENANT_SCOPE_MISMATCH = "TENANT_SCOPE_MISMATCH"
OBJECT_NOT_IN_ORGANIZATION = "OBJECT_NOT_IN_ORGANIZATION"


class TenantScopeError(Exception):
    """Fail-closed tenant validation error with a deliberately non-sensitive message."""

    def __init__(
        self,
        code: str = TENANT_SCOPE_MISMATCH,
        *,
        object_type: str = "object",
    ) -> None:
        self.code = code
        self.object_type = object_type
        super().__init__(code)


@dataclass(frozen=True)
class TenantGuard:
    """Defense-in-depth ownership validation for one resolved organization.

    The authenticated/runtime backend must resolve ``organization`` before this
    guard is created. The guard never asks an LLM to select a tenant and never
    exposes foreign-tenant values in its exceptions.
    """

    organization: Any

    def __post_init__(self) -> None:
        if self.organization is None or getattr(self.organization, "id", None) is None:
            raise TenantScopeError(
                OBJECT_NOT_IN_ORGANIZATION,
                object_type="organization",
            )

    @property
    def organization_id(self):
        return self.organization.id

    def _reject(
        self,
        *,
        object_type: str,
        code: str = TENANT_SCOPE_MISMATCH,
    ):
        raise TenantScopeError(code, object_type=object_type)

    def _require(self, obj, *, object_type: str):
        if obj is None:
            self._reject(
                object_type=object_type,
                code=OBJECT_NOT_IN_ORGANIZATION,
            )
        return obj

    def validate_lead(self, lead):
        lead = self._require(lead, object_type="lead")
        if getattr(lead, "organization_id", None) != self.organization_id:
            self._reject(object_type="lead")
        return lead

    def validate_pipeline(
        self,
        pipeline,
        *,
        lead=None,
        require_current: bool = False,
    ):
        pipeline = self._require(pipeline, object_type="pipeline")
        if getattr(pipeline, "organization_id", None) != self.organization_id:
            self._reject(object_type="pipeline")
        if lead is not None:
            self.validate_lead(lead)
            if require_current and getattr(lead, "pipeline_id", None) != getattr(
                pipeline,
                "id",
                None,
            ):
                self._reject(object_type="pipeline")
        return pipeline

    def validate_stage(
        self,
        stage,
        *,
        pipeline=None,
        lead=None,
        require_current: bool = False,
    ):
        stage = self._require(stage, object_type="stage")
        owning_pipeline = self._require(
            getattr(stage, "pipeline", None),
            object_type="pipeline",
        )
        self.validate_pipeline(owning_pipeline)
        if getattr(stage, "pipeline_id", None) != getattr(owning_pipeline, "id", None):
            self._reject(object_type="stage")
        if pipeline is not None:
            self.validate_pipeline(pipeline)
            if getattr(stage, "pipeline_id", None) != getattr(pipeline, "id", None):
                self._reject(object_type="stage")
        if lead is not None:
            self.validate_lead(lead)
            if require_current:
                if getattr(lead, "stage_id", None) != getattr(stage, "id", None):
                    self._reject(object_type="stage")
                if getattr(lead, "pipeline_id", None) != getattr(stage, "pipeline_id", None):
                    self._reject(object_type="stage")
        return stage

    def validate_whatsapp_account(self, account):
        account = self._require(account, object_type="whatsapp_account")
        if getattr(account, "organization_id", None) != self.organization_id:
            self._reject(object_type="whatsapp_account")
        return account

    def validate_contact(self, contact, *, lead=None):
        contact = self._require(contact, object_type="contact")
        owning_lead = self._require(getattr(contact, "lead", None), object_type="lead")
        self.validate_lead(owning_lead)
        if lead is not None:
            self.validate_lead(lead)
            if getattr(contact, "lead_id", None) != getattr(lead, "id", None):
                self._reject(object_type="contact")
        return contact

    def validate_attribute(self, attribute):
        attribute = self._require(attribute, object_type="attribute")
        if getattr(attribute, "organization_id", None) != self.organization_id:
            self._reject(object_type="attribute")
        return attribute

    def validate_lead_attribute(self, *, lead, attribute):
        self.validate_lead(lead)
        self.validate_attribute(attribute)
        return attribute

    def validate_document(self, document):
        document = self._require(document, object_type="document")
        if getattr(document, "organization_id", None) != self.organization_id:
            self._reject(object_type="document")
        return document

    def validate_file(self, document):
        """Validate an AI-shareable file through its canonical Document owner."""
        return self.validate_document(document)

    def validate_chunk(self, chunk):
        chunk = self._require(chunk, object_type="chunk")
        if getattr(chunk, "organization_id", None) != self.organization_id:
            self._reject(object_type="chunk")
        document = self._require(getattr(chunk, "document", None), object_type="document")
        self.validate_document(document)
        if getattr(document, "id", None) != getattr(chunk, "document_id", None):
            self._reject(object_type="chunk")
        if getattr(document, "organization_id", None) != getattr(
            chunk,
            "organization_id",
            None,
        ):
            self._reject(object_type="chunk")
        return chunk

    def validate_note(self, note, *, lead=None):
        note = self._require(note, object_type="note")
        owning_lead = self._require(getattr(note, "lead", None), object_type="lead")
        self.validate_lead(owning_lead)
        if lead is not None:
            self.validate_lead(lead)
            if getattr(note, "lead_id", None) != getattr(lead, "id", None):
                self._reject(object_type="note")
        return note

    def validate_reminder(self, reminder, *, lead=None):
        reminder = self._require(reminder, object_type="reminder")
        owning_lead = self._require(getattr(reminder, "lead", None), object_type="lead")
        self.validate_lead(owning_lead)
        if lead is not None:
            self.validate_lead(lead)
            if getattr(reminder, "lead_id", None) != getattr(lead, "id", None):
                self._reject(object_type="reminder")
        return reminder

    def validate_summary(self, summary, *, lead=None):
        summary = self._require(summary, object_type="conversation_summary")
        if getattr(summary, "organization_id", None) != self.organization_id:
            self._reject(object_type="conversation_summary")
        owning_lead = self._require(getattr(summary, "lead", None), object_type="lead")
        self.validate_lead(owning_lead)
        if lead is not None:
            self.validate_lead(lead)
            if getattr(summary, "lead_id", None) != getattr(lead, "id", None):
                self._reject(object_type="conversation_summary")
        return summary

    def validate_trace(self, trace, *, lead=None):
        trace = self._require(trace, object_type="ai_trace")
        if getattr(trace, "organization_id", None) != self.organization_id:
            self._reject(object_type="ai_trace")
        owning_lead = self._require(getattr(trace, "lead", None), object_type="lead")
        self.validate_lead(owning_lead)
        if lead is not None:
            self.validate_lead(lead)
            if getattr(trace, "lead_id", None) != getattr(lead, "id", None):
                self._reject(object_type="ai_trace")
        return trace

    def validate_message(
        self,
        message,
        *,
        lead=None,
        account=None,
    ):
        message = self._require(message, object_type="whatsapp_message")
        if getattr(message, "organization_id", None) != self.organization_id:
            self._reject(object_type="whatsapp_message")

        owning_account = self._require(
            getattr(message, "account", None),
            object_type="whatsapp_account",
        )
        self.validate_whatsapp_account(owning_account)
        if getattr(message, "account_id", None) != getattr(owning_account, "id", None):
            self._reject(object_type="whatsapp_message")

        if account is not None:
            self.validate_whatsapp_account(account)
            if getattr(message, "account_id", None) != getattr(account, "id", None):
                self._reject(object_type="whatsapp_message")

        message_lead = getattr(message, "lead", None)
        if message_lead is not None:
            self.validate_lead(message_lead)
        if lead is not None:
            self.validate_lead(lead)
            if getattr(message, "lead_id", None) not in {None, getattr(lead, "id", None)}:
                self._reject(object_type="whatsapp_message")
        return message

    def validate_many(
        self,
        objects: Iterable[Any],
        *,
        validator,
    ) -> tuple[Any, ...]:
        return tuple(validator(obj) for obj in objects)

    def validate_crm_action(self, *, lead, action: dict[str, Any]):
        """Validate referenced CRM targets before the canonical executor mutates.

        Lookups are scoped to the expected organization/lead. A missing or foreign
        identifier receives the same safe error and no foreign data is exposed.
        """

        self.validate_lead(lead)
        action_type = str(action.get("type") or "")

        if action_type == "pipeline_transition":
            from apps.crm.models import Stage

            stage_id = (action.get("stage_shift") or {}).get("stage_id")
            stage = (
                Stage.objects.select_related("pipeline")
                .filter(
                    pk=stage_id,
                    pipeline__organization_id=self.organization_id,
                )
                .first()
            )
            if stage is None:
                self._reject(
                    object_type="stage",
                    code=OBJECT_NOT_IN_ORGANIZATION,
                )
            self.validate_stage(stage)
            return action

        if action_type == "contact_updates":
            from apps.crm.models import LeadContact

            contact_ids = [
                item.get("contact_id")
                for item in action.get("updates") or []
                if isinstance(item, dict) and item.get("contact_id")
            ]
            contacts = {
                str(contact.id): contact
                for contact in LeadContact.objects.select_related("lead").filter(
                    id__in=contact_ids,
                    lead=lead,
                    lead__organization_id=self.organization_id,
                )
            }
            for contact_id in contact_ids:
                contact = contacts.get(str(contact_id))
                if contact is None:
                    self._reject(
                        object_type="contact",
                        code=OBJECT_NOT_IN_ORGANIZATION,
                    )
                self.validate_contact(contact, lead=lead)
            return action

        if action_type == "attribute_updates":
            from apps.crm.models import AttributeDefinition

            keys = {
                str(item.get("key") or "")
                for item in action.get("updates") or []
                if isinstance(item, dict) and item.get("key")
            }
            if not keys:
                return action
            definitions = list(
                AttributeDefinition.objects.filter(
                    organization_id=self.organization_id,
                    key__in=keys,
                )
            )
            for definition in definitions:
                self.validate_attribute(definition)
            if {definition.key for definition in definitions} != keys:
                self._reject(
                    object_type="attribute",
                    code=OBJECT_NOT_IN_ORGANIZATION,
                )
            return action

        # add_note/create_reminder target the already validated current Lead and
        # contain no foreign object identifier in the current canonical schema.
        return action

    def validate_current_lead_context(self, lead):
        self.validate_lead(lead)
        pipeline = getattr(lead, "pipeline", None)
        if pipeline is not None:
            self.validate_pipeline(pipeline, lead=lead, require_current=True)
        stage = getattr(lead, "stage", None)
        if stage is not None:
            self.validate_stage(
                stage,
                pipeline=pipeline,
                lead=lead,
                require_current=True,
            )
        return lead
