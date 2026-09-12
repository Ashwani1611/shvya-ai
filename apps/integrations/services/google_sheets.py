from __future__ import annotations

import json
import re
from typing import Any

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import F
from django.utils import timezone

from apps.crm.models import AttributeDefinition
from apps.integrations.models import GoogleSheetIntegration
from services.crm.lead_service import DuplicateLeadError, upsert_lead

GOOGLE_SHEETS_SECRET_HEADER = "HTTP_X_SHVYA_SHEETS_SECRET"
GOOGLE_SHEETS_BATCH_SIZE = 200
MAX_ROWS_PER_WEBHOOK = 500

CORE_TARGETS = {
    "core:name",
    "core:phone",
    "core:email",
    "core:notes",
}


def normalize_sheet_phone(value: Any) -> str:
    """Convert common spreadsheet phone formats into SHVYA's +country format."""
    raw = str(value or "").strip()
    if not raw:
        return ""
    digits = re.sub(r"\D", "", raw)
    return f"+{digits}" if digits else ""


def sanitize_headers(headers: Any) -> list[str]:
    """Keep non-empty, unique headers in their original order."""
    if not isinstance(headers, list):
        return []

    cleaned: list[str] = []
    seen: set[str] = set()
    for header in headers:
        value = str(header or "").strip()
        key = value.casefold()
        if not value or key in seen:
            continue
        seen.add(key)
        cleaned.append(value[:180])
    return cleaned


def build_google_apps_script(*, integration: GoogleSheetIntegration, webhook_url: str) -> str:
    """Return a bound Apps Script that pushes edits and reconciles missed appended rows."""
    suffix = str(integration.id).replace("-", "")[:8].upper()
    config_name = f"SHVYA_CONFIG_{suffix}"
    setup_name = f"setupShvyaSync_{suffix}"
    register_name = f"shvyaRegister_{suffix}"
    edit_name = f"shvyaOnEdit_{suffix}"
    submit_name = f"shvyaOnFormSubmit_{suffix}"
    reconcile_name = f"shvyaReconcile_{suffix}"
    send_name = f"shvyaSendRows_{suffix}"
    request_name = f"shvyaRequest_{suffix}"
    props_key = f"SHVYA_LAST_ROW_{suffix}"

    config = {
        "webhookUrl": webhook_url,
        "secret": integration.get_secret(),
        "sheetName": integration.worksheet_name,
        "batchSize": GOOGLE_SHEETS_BATCH_SIZE,
        "importExisting": bool(integration.import_existing),
        "lastRowKey": props_key,
    }
    config_json = json.dumps(config, ensure_ascii=False, separators=(",", ":"))

    return f'''// SHVYA Google Sheets real-time lead sync
// Generated for: {integration.name}
// OAuth2 library is NOT required. Paste this into Extensions -> Apps Script.

const {config_name} = {config_json};

function {setup_name}() {{
  const cfg = {config_name};
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const sheet = ss.getSheetByName(cfg.sheetName);
  if (!sheet) throw new Error('SHVYA: worksheet "' + cfg.sheetName + '" was not found.');

  ScriptApp.getProjectTriggers().forEach(trigger => {{
    const handler = trigger.getHandlerFunction();
    if (["{edit_name}", "{submit_name}", "{reconcile_name}"].indexOf(handler) !== -1) {{
      ScriptApp.deleteTrigger(trigger);
    }}
  }});

  ScriptApp.newTrigger("{edit_name}").forSpreadsheet(ss).onEdit().create();
  ScriptApp.newTrigger("{submit_name}").forSpreadsheet(ss).onFormSubmit().create();
  ScriptApp.newTrigger("{reconcile_name}").timeBased().everyMinutes(5).create();

  {register_name}();

  const props = PropertiesService.getDocumentProperties();
  if (!cfg.importExisting) {{
    props.setProperty(cfg.lastRowKey, String(Math.max(1, sheet.getLastRow())));
  }} else if (!props.getProperty(cfg.lastRowKey)) {{
    props.setProperty(cfg.lastRowKey, "1");
  }}

  SpreadsheetApp.getActive().toast('SHVYA sync installed. Return to Connect Hub and map the detected columns.', 'SHVYA', 8);
}}

function {register_name}() {{
  const cfg = {config_name};
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const sheet = ss.getSheetByName(cfg.sheetName);
  if (!sheet) throw new Error('SHVYA: worksheet "' + cfg.sheetName + '" was not found.');

  const lastColumn = sheet.getLastColumn();
  const headers = lastColumn > 0
    ? sheet.getRange(1, 1, 1, lastColumn).getDisplayValues()[0].map(v => String(v || '').trim())
    : [];

  return {request_name}({{
    event: 'register',
    spreadsheet_id: ss.getId(),
    spreadsheet_url: ss.getUrl(),
    sheet_id: sheet.getSheetId(),
    sheet_name: sheet.getName(),
    headers: headers
  }});
}}

function {edit_name}(e) {{
  if (!e || !e.range) return;
  const cfg = {config_name};
  const sheet = e.range.getSheet();
  if (sheet.getName() !== cfg.sheetName) return;

  const firstRow = Math.max(2, e.range.getRow());
  const lastRow = Math.max(firstRow, e.range.getLastRow());
  if (lastRow < 2) return;
  {send_name}(sheet, firstRow, lastRow);
}}

function {submit_name}(e) {{
  if (!e || !e.range) return;
  const cfg = {config_name};
  const sheet = e.range.getSheet();
  if (sheet.getName() !== cfg.sheetName) return;
  const row = Math.max(2, e.range.getRow());
  {send_name}(sheet, row, row);
}}

function {reconcile_name}() {{
  const cfg = {config_name};
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const sheet = ss.getSheetByName(cfg.sheetName);
  if (!sheet) return;

  const props = PropertiesService.getDocumentProperties();
  const lastProcessed = Math.max(1, Number(props.getProperty(cfg.lastRowKey) || '1'));
  const lastRow = sheet.getLastRow();
  if (lastRow <= lastProcessed) return;

  {send_name}(sheet, lastProcessed + 1, lastRow);
}}

function {send_name}(sheet, firstRow, lastRow) {{
  const cfg = {config_name};
  const ss = sheet.getParent();
  const lastColumn = sheet.getLastColumn();
  if (lastColumn < 1 || lastRow < 2) return;

  const headers = sheet.getRange(1, 1, 1, lastColumn).getDisplayValues()[0].map(v => String(v || '').trim());
  const props = PropertiesService.getDocumentProperties();

  for (let start = Math.max(2, firstRow); start <= lastRow; start += cfg.batchSize) {{
    const count = Math.min(cfg.batchSize, lastRow - start + 1);
    const values = sheet.getRange(start, 1, count, lastColumn).getDisplayValues();
    const rows = [];

    values.forEach((row, offset) => {{
      const data = {{}};
      headers.forEach((header, index) => {{
        if (header) data[header] = row[index];
      }});
      const rowNumber = start + offset;
      rows.push({{
        row_number: rowNumber,
        source_id: ss.getId() + ':' + sheet.getSheetId() + ':' + rowNumber,
        values: data
      }});
    }});

    {request_name}({{
      event: 'rows',
      spreadsheet_id: ss.getId(),
      spreadsheet_url: ss.getUrl(),
      sheet_id: sheet.getSheetId(),
      sheet_name: sheet.getName(),
      rows: rows
    }});

    const currentCheckpoint = Math.max(1, Number(props.getProperty(cfg.lastRowKey) || '1'));
    const batchEnd = start + count - 1;
    if (start <= currentCheckpoint + 1) {{
      props.setProperty(cfg.lastRowKey, String(Math.max(currentCheckpoint, batchEnd)));
    }}
  }}
}}

function {request_name}(payload) {{
  const cfg = {config_name};
  const response = UrlFetchApp.fetch(cfg.webhookUrl, {{
    method: 'post',
    contentType: 'application/json',
    headers: {{ 'X-Shvya-Sheets-Secret': cfg.secret }},
    payload: JSON.stringify(payload),
    muteHttpExceptions: true
  }});

  const code = response.getResponseCode();
  if (code < 200 || code >= 300) {{
    throw new Error('SHVYA sync failed (HTTP ' + code + '): ' + response.getContentText().slice(0, 500));
  }}

  const text = response.getContentText();
  try {{ return JSON.parse(text); }} catch (err) {{ return text; }}
}}
'''


def _resolve_mapping(integration: GoogleSheetIntegration):
    mapping = integration.mapping if isinstance(integration.mapping, dict) else {}
    attribute_ids = []
    for target in mapping.values():
        if isinstance(target, str) and target.startswith("attribute:"):
            attribute_ids.append(target.split(":", 1)[1])

    attributes = AttributeDefinition.objects.filter(
        organization=integration.organization,
        id__in=attribute_ids,
    )
    attribute_keys = {str(attribute.id): attribute.key for attribute in attributes}
    return mapping, attribute_keys


def _map_row(*, mapping: dict[str, str], attribute_keys: dict[str, str], values: dict[str, Any]):
    core = {"name": "", "phone": "", "email": "", "notes": ""}
    attributes: dict[str, Any] = {}

    for header, target in mapping.items():
        if not target or header not in values:
            continue
        value = values.get(header)
        if target.startswith("core:"):
            field = target.split(":", 1)[1]
            if field in core:
                core[field] = value
        elif target.startswith("attribute:"):
            attribute_id = target.split(":", 1)[1]
            key = attribute_keys.get(attribute_id)
            if key:
                attributes[key] = value

    core["phone"] = normalize_sheet_phone(core["phone"])
    core["name"] = str(core["name"] or "").strip() or "Google Sheets Lead"
    core["email"] = str(core["email"] or "").strip()
    core["notes"] = str(core["notes"] or "").strip()
    return core, attributes


def process_google_sheet_rows(*, integration_id: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Apply one Apps Script batch to CRM with phone-based idempotent upserts."""
    integration = (
        GoogleSheetIntegration.objects.select_related("organization", "pipeline", "stage")
        .filter(id=integration_id)
        .first()
    )
    if integration is None:
        return {"status": "missing", "created": 0, "updated": 0, "skipped": 0, "errors": 0}
    if not integration.is_enabled:
        return {"status": "disabled", "created": 0, "updated": 0, "skipped": len(rows or []), "errors": 0}

    mapping, attribute_keys = _resolve_mapping(integration)
    if "core:phone" not in mapping.values():
        raise ValidationError("Google Sheets mapping must include a Phone field.")

    created = 0
    updated = 0
    skipped = 0
    errors = 0
    error_messages: list[str] = []

    for row in rows[:MAX_ROWS_PER_WEBHOOK]:
        values = row.get("values") if isinstance(row, dict) else None
        if not isinstance(values, dict):
            skipped += 1
            continue

        core, attributes = _map_row(
            mapping=mapping,
            attribute_keys=attribute_keys,
            values=values,
        )
        if not core["phone"]:
            skipped += 1
            continue

        try:
            try:
                _, was_created = upsert_lead(
                    organization=integration.organization,
                    pipeline=integration.pipeline,
                    stage=integration.stage,
                    name=core["name"],
                    phone=core["phone"],
                    email=core["email"],
                    notes=core["notes"],
                    attributes=attributes,
                    lead_source="google_sheets",
                )
            except DuplicateLeadError:
                # Another batch may have created the same phone concurrently.
                _, was_created = upsert_lead(
                    organization=integration.organization,
                    pipeline=integration.pipeline,
                    stage=integration.stage,
                    name=core["name"],
                    phone=core["phone"],
                    email=core["email"],
                    notes=core["notes"],
                    attributes=attributes,
                    lead_source="google_sheets",
                )

            if was_created:
                created += 1
            else:
                updated += 1
        except ValidationError as exc:
            # Invalid phone/email data should not abort valid rows in the same batch.
            errors += 1
            if len(error_messages) < 5:
                row_number = row.get("row_number", "?") if isinstance(row, dict) else "?"
                error_messages.append(f"Row {row_number}: {exc}")

    now = timezone.now()
    last_error = " | ".join(error_messages)
    with transaction.atomic():
        GoogleSheetIntegration.objects.filter(id=integration.id).update(
            created_count=F("created_count") + created,
            updated_count=F("updated_count") + updated,
            skipped_count=F("skipped_count") + skipped,
            error_count=F("error_count") + errors,
            last_synced_at=now,
            last_error=last_error,
            updated_at=now,
        )

    return {
        "status": "processed",
        "created": created,
        "updated": updated,
        "skipped": skipped,
        "errors": errors,
        "last_error": last_error,
    }
