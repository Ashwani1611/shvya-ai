"""Metadata-only outcomes for marketplace and Meta lead intake."""

from apps.core.api_activity.services import record_activity


def record_lead_activity(
    *,
    integration,
    result,
    connection=None,
    lead=None,
    lead_id=None,
    provider_reference="",
    reason_code="",
    error_category="",
    summary="",
    metadata=None,
    method="POST",
    operation="lead.import",
):
    """Record the confirmed CRM outcome without copying provider answers.

    Call successful outcomes in the same transaction as the CRM receipt. Call
    failures after any failed transaction exits so its rollback cannot erase
    the evidence. Transport acknowledgement remains a separate HTTP activity.
    """
    status = {
        "created": "completed",
        "updated": "completed",
        "duplicate": "skipped",
        "ignored": "skipped",
        "blocked": "blocked",
        "failed": "failed",
    }[result]
    entity_id = str(lead.id) if lead is not None else str(lead_id or "")
    return record_activity(
        integration=integration,
        provider="meta" if integration == "meta_leads" else integration,
        kind="lead_ingestion",
        direction="inbound",
        operation=operation,
        organization_id=getattr(connection, "organization_id", None),
        connection_id=str(connection.pk) if connection is not None else "",
        entity_type="lead" if entity_id else "",
        entity_id=entity_id,
        provider_reference=provider_reference,
        method=method,
        status=status,
        error_code=reason_code,
        error_category=error_category or (
            ("validation" if result == "failed" else "configuration")
            if reason_code and result in {"failed", "blocked"} else ""
        ),
        summary=summary,
        metadata={"result": result, **(metadata or {})},
    )
