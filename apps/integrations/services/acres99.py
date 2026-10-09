"""99acres XML protocol adapters and safe, idempotent CRM ingestion.

Pull contract: POST form field "xml" to 99acres' fixed HTTPS endpoint.
Push contract: XML batch of Qry elements; acknowledge each QryId independently.
No user-controlled URLs, provider credentials or customer contact data in logs.
"""
from __future__ import annotations

import logging
import re
from datetime import timedelta
from xml.etree import ElementTree as ET
from zoneinfo import ZoneInfo

import requests
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils import timezone

from apps.crm.models import Lead, LeadNote
from apps.crm.models.lead import normalize_phone
from apps.integrations.acres99_models import (
    Acres99Event, Acres99Integration, Acres99Receipt,
)
from services.crm.lead_service import create_lead

logger = logging.getLogger(__name__)
PULL_BASE = "https://www.99acres.com/99api/v1/getmy99Response"
MAX_PUSH_BYTES = 2 * 1024 * 1024
MAX_PULL_BYTES = 8 * 1024 * 1024
MAX_PUSH_RECORDS = 1000
MAX_PULL_RECORDS = 5000
LOCAL_TIMEZONE = ZoneInfo("Asia/Kolkata")


class Acres99ProtocolError(Exception):
    """A provider or payload error without sensitive request details."""


def _text(element, tag):
    child = element.find(tag)
    return (child.text or "").strip() if child is not None else ""


def _parse_xml(raw, *, max_bytes):
    if not isinstance(raw, bytes) or len(raw) > max_bytes or not raw.strip():
        raise Acres99ProtocolError("XML body is missing or exceeds the permitted size.")
    # Never accept DTDs/entities from untrusted provider callbacks.
    if re.search(rb"<!\s*(?:DOCTYPE|ENTITY)\b", raw, re.IGNORECASE):
        raise Acres99ProtocolError("DTD and entity declarations are not permitted.")
    try:
        return ET.fromstring(raw)
    except (ET.ParseError, ValueError, UnicodeDecodeError) as exc:
        raise Acres99ProtocolError("Malformed provider XML.") from exc


def parse_push_xml(raw):
    root = _parse_xml(raw, max_bytes=MAX_PUSH_BYTES)
    if root.tag != "Xml":
        raise Acres99ProtocolError("Expected a 99acres Xml root element.")
    rows = root.findall("Qry")
    if not rows or len(rows) > MAX_PUSH_RECORDS:
        raise Acres99ProtocolError("Push batch must contain 1-1000 Qry elements.")
    return [
        {
            "query_id": _text(row, "QryId") or _text(row, "QryID"),
            "query_type": _text(row, "QryType"),
            "description": _text(row, "CmpctLabl"),
            "requirement": _text(row, "QryInfo"),
            "received_on": _text(row, "RcvdOn"),
            "product_type": _text(row, "ProdType"),
            "product_id": _text(row, "ProdId"),
            "project": _text(row, "Project"),
            "name": _text(row, "Name"),
            "email": _text(row, "Email"),
            "phone": _text(row, "Phone"),
        }
        for row in rows
    ]


def parse_pull_xml(raw):
    root = _parse_xml(raw, max_bytes=MAX_PULL_BYTES)
    if root.tag != "Xml":
        raise Acres99ProtocolError("Expected a 99acres Xml response.")
    if root.attrib.get("ActionStatus", "").lower() != "true":
        error = root.find("ErrorDetail")
        code = _text(error, "Code") if error is not None else ""
        code = re.sub(r"[^A-Za-z0-9-]", "", code)[:32]
        raise Acres99ProtocolError(f"99acres Pull returned an error ({code or 'unknown'}).")
    rows = root.findall("Resp")
    if len(rows) > MAX_PULL_RECORDS:
        raise Acres99ProtocolError("Provider returned more than 5000 responses.")
    parsed = []
    for row in rows:
        detail = row.find("QryDtl")
        contact = row.find("CntctDtl")
        if detail is None or contact is None:
            parsed.append({"query_id": ""})
            continue
        product = detail.find("ProdId")
        parsed.append({
            "query_id": (detail.attrib.get("TblId") or "").strip(),
            "query_type": (detail.attrib.get("ResType") or "").strip(),
            "description": _text(detail, "CmpctLabl"),
            "requirement": _text(detail, "QryInfo"),
            "received_on": _text(detail, "RcvdOn"),
            "product_type": product.attrib.get("Type", "") if product is not None else "",
            "product_id": (product.text or "").strip() if product is not None else "",
            "project": "",
            "name": _text(contact, "Name"),
            "email": _text(contact, "Email"),
            "phone": _text(contact, "Phone"),
        })
    return parsed


def push_acknowledgement(results):
    root = ET.Element("xml")
    for query_id, ok in results:
        item = ET.SubElement(root, "QryResp")
        ET.SubElement(item, "QryId").text = query_id
        ET.SubElement(item, "status").text = "Y" if ok else "N"
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def _normalized_phone(raw):
    value = str(raw or "").strip()
    digits = re.sub(r"\D", "", value)
    if not digits or not 8 <= len(digits) <= 15:
        raise ValidationError("Buyer phone is missing or invalid.")
    # 99acres documentation is for the Indian market. An unprefixed 10-digit
    # number is interpreted as +91; other bare numbers require country code.
    if value.startswith("+"):
        return normalize_phone(value)
    if len(digits) == 10:
        return normalize_phone("+91" + digits)
    if len(digits) == 12 and digits.startswith("91"):
        return normalize_phone("+" + digits)
    raise ValidationError("Buyer phone must include an international country code.")


def _safe_email(raw):
    value = str(raw or "").strip()[:254]
    if not value:
        return ""
    try:
        validate_email(value)
    except ValidationError:
        return ""
    return value


def _safe_str(value, limit):
    return str(value or "").strip()[:limit]


def _route_ok(connection):
    return (
        connection.is_enabled
        and connection.organization.is_active
        and connection.pipeline_id
        and connection.stage_id
        and connection.pipeline.organization_id == connection.organization_id
        and connection.stage.pipeline_id == connection.pipeline_id
        and connection.pipeline.is_active
        and connection.stage.is_active
    )


def _notes(row):
    labels = (
        ("query_id", "99acres enquiry ID"),
        ("received_on", "Received on"),
        ("query_type", "Enquiry type"),
        ("product_id", "Property listing ID"),
        ("product_type", "Property type code"),
        ("project", "Project reference"),
        ("description", "Property"),
        ("requirement", "Buyer requirement"),
    )
    return "\n".join(
        f"{label}: {_safe_str(row.get(key), 2000)}"
        for key, label in labels if row.get(key)
    )[:6000]


def record_failure(connection, row, direction, code):
    """Retain only a bounded error code, never contact data or XML."""
    safe = re.sub(r"[^A-Za-z0-9_ -]", "", str(code or "invalid_enquiry"))[:100]
    Acres99Event.objects.create(
        integration=connection,
        direction=direction,
        external_query_id=_safe_str(row.get("query_id"), 180),
        status=Acres99Event.Status.FAILED,
        error_code=safe,
    )
    Acres99Integration.objects.filter(pk=connection.pk).update(
        failed_count=F("failed_count") + 1,
        last_error=safe,
    )


def ingest_query(integration, row, *, direction):
    """Process one enquiry in its own transaction so a bad row doesn't lose peers.

    A successful receipt is the idempotency tombstone across webhook retries,
    polling overlap and lead deletion. Existing contact identity/stage is never
    overwritten by another marketplace enquiry.
    """
    external_id = _safe_str(row.get("query_id"), 180)
    if not external_id:
        raise ValidationError("Missing unique query ID.")
    if len(str(row.get("query_id", "")).strip()) > 180:
        raise ValidationError("Query ID exceeds 180 characters.")
    if direction not in {"push", "pull"}:
        raise ValidationError("Invalid source direction.")
    with transaction.atomic():
        connection = Acres99Integration.objects.select_for_update().select_related(
            "organization", "pipeline", "stage",
        ).get(pk=integration.pk)
        if not _route_ok(connection):
            raise ValidationError("99acres routing is disabled or invalid.")
        existing_receipt = connection.receipts.filter(external_query_id=external_id).first()
        if existing_receipt:
            Acres99Event.objects.create(
                integration=connection,
                external_query_id=external_id,
                direction=direction,
                status=Acres99Event.Status.DUPLICATE,
                lead=existing_receipt.lead,
            )
            return existing_receipt.lead, "duplicate"

        phone = _normalized_phone(row.get("phone"))
        lead = Lead.objects.select_for_update().filter(
            organization=connection.organization, phone=phone,
        ).first()
        note = _notes(row)
        if lead:
            if note:
                LeadNote.objects.create(lead=lead, note=note, note_type="system")
            status = Acres99Event.Status.LINKED
        else:
            attrs = {
                "acres99_latest_query_id": external_id,
                "acres99_property_id": _safe_str(row.get("product_id"), 120),
                "acres99_project": _safe_str(row.get("project"), 180),
                "acres99_query_type": _safe_str(row.get("query_type"), 90),
                "acres99_product_type": _safe_str(row.get("product_type"), 90),
            }
            lead = create_lead(
                organization=connection.organization,
                pipeline=connection.pipeline,
                stage=connection.stage,
                name=_safe_str(row.get("name"), 150) or "99acres Buyer",
                phone=phone,
                email=_safe_email(row.get("email")),
                notes=note,
                attributes=attrs,
                lead_source="99acres",
                send_welcome=False,
            )
            status = Acres99Event.Status.CREATED

        Acres99Receipt.objects.create(
            integration=connection,
            external_query_id=external_id,
            direction=direction,
            property_id=_safe_str(row.get("product_id"), 120),
            lead=lead,
        )
        Acres99Event.objects.create(
            integration=connection,
            external_query_id=external_id,
            direction=direction,
            lead=lead,
            status=status,
        )
        updates = {"last_received_at": timezone.now(), "last_error": ""}
        if status == Acres99Event.Status.CREATED:
            updates["created_count"] = F("created_count") + 1
        else:
            updates["linked_count"] = F("linked_count") + 1
        updates["received_count"] = F("received_count") + 1
        Acres99Integration.objects.filter(pk=connection.pk).update(**updates)
        return lead, status


def _pull_url(connection):
    """Construct an allowlisted HTTPS URL with this tenant's encrypted token."""
    token = connection.get_provider_token()
    if not token or not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", token):
        raise Acres99ProtocolError("99acres Pull token is missing or invalid.")
    return f"{PULL_BASE}/{token}/uid/"


def _request_xml(username, password, start, end):
    root = ET.Element("query")
    ET.SubElement(root, "user_name").text = username
    # Vendor samples use "pswd" even though the introductory list says "pwd".
    ET.SubElement(root, "pswd").text = password
    ET.SubElement(root, "start_date").text = start.astimezone(LOCAL_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")
    ET.SubElement(root, "end_date").text = end.astimezone(LOCAL_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")
    return ET.tostring(root, encoding="unicode")


def sync_connection(integration_id, *, manual=False):
    """Pull at most six requests per hour; advance cursor only on full success.

    A 2-minute overlap handles minor provider latency and avoids missed
    boundary timestamps; the query-ID receipt handles duplicates.
    """
    now = timezone.now()
    with transaction.atomic():
        connection = Acres99Integration.objects.select_for_update().select_related(
            "organization", "pipeline", "stage",
        ).get(pk=integration_id)
        if not _route_ok(connection) or connection.mode not in {
            Acres99Integration.Mode.PULL, Acres99Integration.Mode.BOTH,
        } or not connection.has_credentials:
            return {"status": "not_ready"}
        if not manual and connection.last_poll_at and now - connection.last_poll_at < timedelta(minutes=15):
            return {"status": "not_due"}
        if not connection.poll_hour_start or now - connection.poll_hour_start >= timedelta(hours=1):
            connection.poll_hour_start = now
            connection.poll_hour_count = 0
        if connection.poll_hour_count >= 6:
            return {"status": "rate_limited"}
        connection.poll_hour_count += 1
        connection.last_poll_at = now
        connection.save(update_fields=["poll_hour_start", "poll_hour_count", "last_poll_at", "updated_at"])
        credentials = connection.get_credentials()
        start = connection.sync_cursor - timedelta(minutes=2) if connection.sync_cursor else now - timedelta(days=1)
        start = max(start, now - timedelta(days=29))
        end = min(start + timedelta(days=2), now)
    if not credentials:
        Acres99Integration.objects.filter(pk=integration_id).update(last_error="Pull credentials cannot be decrypted.")
        return {"status": "credentials_unavailable"}
    if end <= start:
        return {"status": "not_due"}
    try:
        response = requests.post(
            _pull_url(connection),
            data={"xml": _request_xml(*credentials, start, end)},
            timeout=(5, 20),
            allow_redirects=False,
            headers={"User-Agent": "SHVYA-99acres-Integration/1.0"},
        )
        if response.status_code != 200:
            raise Acres99ProtocolError(f"99acres returned HTTP {response.status_code}.")
        parsed = parse_pull_xml(response.content)
        if len(parsed) >= MAX_PULL_RECORDS:
            # Without pagination, advancing beyond a saturated result loses leads.
            raise Acres99ProtocolError("99acres result limit reached; split or reduce the sync window.")
        successful = 0
        failures = 0
        for row in parsed:
            try:
                ingest_query(connection, row, direction="pull")
                successful += 1
            except (ValidationError, IntegrityError, Acres99ProtocolError) as exc:
                record_failure(connection, row, "pull", type(exc).__name__)
                failures += 1
        if failures:
            Acres99Integration.objects.filter(pk=integration_id).update(
                last_error=f"{failures} enquiry(s) could not be imported; the window will be retried."
            )
            return {"status": "partial", "imported": successful, "failed": failures}
        with transaction.atomic():
            refreshed = Acres99Integration.objects.select_for_update().get(pk=integration_id)
            if not refreshed.sync_cursor or end > refreshed.sync_cursor:
                refreshed.sync_cursor = end
            refreshed.last_synced_at = timezone.now()
            refreshed.last_error = ""
            refreshed.save(update_fields=["sync_cursor", "last_synced_at", "last_error", "updated_at"])
        return {"status": "synced", "imported": successful}
    except (requests.RequestException, Acres99ProtocolError) as exc:
        code = str(exc) if isinstance(exc, Acres99ProtocolError) else "99acres request failed."
        Acres99Integration.objects.filter(pk=integration_id).update(last_error=code[:500])
        logger.warning("99acres Pull sync failed for connection %s: %s", integration_id, code)
        return {"status": "failed"}
