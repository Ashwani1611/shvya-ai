"""Deterministic, non-persisting account handover artifacts from supplied evidence.

The renderer validates structure and tenant binding, not the truth of submitted
claims. It never queries configuration, contacts providers, or executes demos.
"""
from datetime import datetime
from hashlib import sha256
from html import escape
import json
import re
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from apps.integrations.diagnostic_auth import sanitize_text


SECTION_TITLES = {
    "overview": "How the account works",
    "flow": "Lead flow",
    "qualification": "AI qualification",
    "automation": "Triggers and sequences",
    "demo": "Demo script",
    "day_to_day": "What the team does every day",
    "open_questions": "Open questions for the setup team",
    "not_covered": "Not covered in this demo",
}
MAX_INPUT_CHARS = 250000
MAX_OUTPUT_CHARS = 1000000
MAX_CHUNK_CHARS = 20000
NOTICE = (
    "Prepared from caller-supplied evidence. The renderer has not independently "
    "verified configuration or tested a lead. Demo steps are scripts only: "
    "no messages, connections, enrollments, changes or cleanup were executed."
)
ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
STATES = {"ai", "human", "won", "lost", "parked", "unknown"}
TEST_STATES = {"passed", "failed", "not_tested", "unknown"}


class HandoverValidationError(ValueError):
    """Safe errors contain no submitted text or credentials."""


def _fail(message):
    raise HandoverValidationError(message)


def _shape(value, fields):
    if not isinstance(value, dict) or set(value) != set(fields.split()):
        _fail("Handover fields do not match the documented version 1 schema.")


def _array(value, maximum, minimum=1):
    if not isinstance(value, list) or not minimum <= len(value) <= maximum:
        _fail("A handover collection is empty, invalid or exceeds its bound.")
    return value


def _text(value, maximum=8000):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        _fail("Handover text is missing, invalid or exceeds its field bound.")
    if any(ord(char) < 32 and char not in "\n\r\t" for char in value):
        _fail("Handover text contains unsupported control characters.")
    if any(0xD800 <= ord(char) <= 0xDFFF for char in value):
        _fail("Handover text contains invalid Unicode.")
    # The response policy deliberately does not re-sanitize generated artifacts:
    # reject *every* input string that the credential sanitizer would alter.
    if sanitize_text(value, limit=len(value)) != value:
        _fail("Handover text contains credential-like material.")
    if re.search(r"\b(?:data|javascript|vbscript|file)\s*:", value, re.I):
        _fail("Embedded media and executable URL schemes are not supported.")
    for match in re.finditer(r"\b[a-z][a-z0-9+.-]*://[^\s<>\"']+", value, re.I):
        try:
            url = urlsplit(match.group())
        except ValueError:
            _fail("A handover URL is invalid.")
        if url.scheme.lower() not in {"https", "http"} or url.username or url.password or url.query or url.fragment:
            _fail("Handover URLs must be public references without credentials, queries or fragments.")
    return value


def _choice(value, choices):
    if not isinstance(value, str) or value not in choices:
        _fail("A handover enum value is unsupported.")


def _identifier(value):
    _text(value, 64)
    if ID_RE.fullmatch(value) is None:
        _fail("Handover identifiers must be short alphanumeric identifiers.")
    return value


def _timestamp(value):
    _text(value, 64)
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if stamp.tzinfo is None or stamp.utcoffset() is None:
            raise ValueError
    except ValueError:
        _fail("Evidence timestamps require ISO datetimes with explicit offsets.")


def _evidence(value, sources):
    for item in _array(value, 30):
        _identifier(item)
        if item not in sources:
            _fail("A handover evidence reference does not exist in its source ledger.")
    if len(set(value)) != len(value):
        _fail("Evidence references must not be duplicated.")


def _bounded_json(document):
    # Check node/depth/scalar budgets before serialization; this also rejects
    # cyclic/non-JSON direct callers without relying on Python recursion limits.
    pending = [(document, 0)]
    nodes = chars = 0
    while pending:
        value, depth = pending.pop()
        nodes += 1
        if nodes > 25000 or depth > 12:
            _fail("Handover document exceeds its structural bounds.")
        if isinstance(value, dict):
            for key, child in value.items():
                if not isinstance(key, str):
                    _fail("Handover keys must be strings.")
                chars += len(key)
                pending.append((child, depth + 1))
        elif isinstance(value, list):
            pending.extend((child, depth + 1) for child in value)
        elif isinstance(value, str):
            chars += len(value)
        elif type(value) is not int:
            _fail("Handover values must use the documented JSON types.")
        if chars > MAX_INPUT_CHARS:
            _fail("Handover document exceeds its input size bound.")
    if len(json.dumps(document, ensure_ascii=False, separators=(",", ":"))) > MAX_INPUT_CHARS:
        _fail("Handover document exceeds its input size bound.")


def _block(block, sources):
    if not isinstance(block, dict):
        _fail("Each handover block must be an object.")
    kind = block.get("type")
    fields = {
        "paragraph": "type text evidence",
        "list": "type title items evidence",
        "table": "type title columns rows evidence",
        "journey": "type title pipeline stages transitions evidence",
        "demo": "type title prerequisites fixture steps evidence",
        "gap": "type title severity observation impact decision evidence",
        "cadence": "type title channel start stop end schedule pause delay_basis steps evidence",
    }
    if not isinstance(kind, str) or kind not in fields:
        _fail("Unsupported handover block type.")
    _shape(block, fields[kind])
    _evidence(block["evidence"], sources)
    if kind != "paragraph":
        _text(block["title"], 240)
    if kind == "paragraph":
        _text(block["text"])
    elif kind == "list":
        for item in _array(block["items"], 100):
            _text(item)
    elif kind == "table":
        for column in _array(block["columns"], 12):
            _text(column, 240)
        for row in _array(block["rows"], 200):
            _shape(row, "cells evidence")
            if len(_array(row["cells"], 12)) != len(block["columns"]):
                _fail("Handover table rows must match the column count.")
            for cell in row["cells"]:
                _text(cell, 4000)
            _evidence(row["evidence"], sources)
    elif kind == "journey":
        _text(block["pipeline"], 240)
        identifiers = set()
        for stage in _array(block["stages"], 100):
            _shape(stage, "id label state")
            _identifier(stage["id"])
            _text(stage["label"], 240)
            _choice(stage["state"], STATES)
            if stage["id"] in identifiers:
                _fail("Journey stage IDs must be unique and states must be supported.")
            identifiers.add(stage["id"])
        for transition in _array(block["transitions"], 200, minimum=0):
            _shape(transition, "from to kind condition evidence")
            _identifier(transition["from"])
            _identifier(transition["to"])
            if transition["from"] not in identifiers or transition["to"] not in identifiers:
                _fail("Journey transitions must reference stages in the same journey.")
            _choice(transition["kind"], {"automatic", "human", "requested"})
            _text(transition["condition"], 1000)
            _evidence(transition["evidence"], sources)
    elif kind == "demo":
        _text(block["prerequisites"])
        _text(block["fixture"], 1000)
        for step in _array(block["steps"], 100):
            _shape(step, "kind action expect dashboard why say evidence")
            _choice(step["kind"], {"send", "do"})
            for field in ("action", "expect", "dashboard", "why", "say"):
                _text(step[field], 4000)
            _evidence(step["evidence"], sources)
    elif kind == "gap":
        _choice(block["severity"], {"blocker", "warning", "question"})
        for field in ("observation", "impact", "decision"):
            _text(block[field])
    elif kind == "cadence":
        for field in ("channel", "start", "stop", "end", "schedule", "pause"):
            _text(block[field], 4000)
        if block["delay_basis"] != "after_previous":
            _fail("Normalize cadence delays to after_previous before rendering.")
        total = 0
        for step in _array(block["steps"], 100):
            _shape(step, "delay message evidence")
            _shape(step["delay"], "value unit")
            value, unit = step["delay"]["value"], step["delay"]["unit"]
            _choice(unit, {"minutes", "hours", "days"})
            if type(value) is not int or not 0 <= value <= 525600:
                _fail("Cadence delays require bounded non-negative integer minutes, hours or days.")
            total += value * {"minutes": 1, "hours": 60, "days": 1440}[unit]
            if total > 5256000:
                _fail("Cumulative cadence duration exceeds ten years.")
            _text(step["message"], 4000)
            _evidence(step["evidence"], sources)


def validate_document(document, *, organization_id, organization_name):
    _bounded_json(document)
    _shape(document, "schema_version organization_id environment evidence_date timezone readiness sources sections")
    if type(document["schema_version"]) is not int or document["schema_version"] != 1:
        _fail("Only handover schema version 1 is supported.")
    _text(document["organization_id"], 64)
    if document["organization_id"] != organization_id:
        _fail("Handover organization does not match the active context.")
    _text(organization_name, 240)
    _text(document["environment"], 80)
    _timestamp(document["evidence_date"])
    _text(document["timezone"], 100)
    try:
        ZoneInfo(document["timezone"])
    except (ZoneInfoNotFoundError, ValueError):
        _fail("Handover timezone must be an IANA timezone.")
    sources = set()
    for source in _array(document["sources"], 200):
        _shape(source, "id tool reference observed_at status note")
        _identifier(source["id"])
        if source["id"] in sources:
            _fail("Source ledger identifiers must be unique.")
        sources.add(source["id"])
        for field in ("tool", "reference", "note"):
            _text(source[field], 2000)
        _timestamp(source["observed_at"])
        _choice(source["status"], {"complete", "partial", "unavailable", "conflict"})
    readiness = document["readiness"]
    _shape(readiness, "configuration engine_test live_delivery evidence note")
    _choice(readiness["configuration"], {"configured", "partial", "not_configured", "unknown"})
    _choice(readiness["engine_test"], TEST_STATES)
    _choice(readiness["live_delivery"], TEST_STATES)
    _text(readiness["note"], 4000)
    _evidence(readiness["evidence"], sources)
    _shape(document["sections"], " ".join(SECTION_TITLES))
    count = 0
    for name in SECTION_TITLES:
        blocks = _array(document["sections"][name], 100)
        count += len(blocks)
        if count > 300:
            _fail("Handover contains too many blocks.")
        for block in blocks:
            _block(block, sources)


def _html(value):
    return escape(str(value), quote=True).replace("\n", "<br>")


def _md(value):
    # All submitted text is literal, not caller-authored Markdown or HTML.
    text = escape(str(value), quote=False)
    return re.sub(r"([\\\x60*_{}\[\]()#+.!|~-])", r"\\\1", text).replace("\n", "<br>")


def _refs(ids, html=False):
    if html:
        return " ".join(f'<a href="#source-{item}">{item}</a>' for item in ids)
    return ", ".join(ids)


def _duration(minutes):
    days, remainder = divmod(minutes, 1440)
    hours, mins = divmod(remainder, 60)
    parts = [f"{value} {unit}" for value, unit in ((days, "days"), (hours, "hours"), (mins, "minutes")) if value]
    return " ".join(parts) if parts else "immediately"


def _cadence_rows(block):
    total = 0
    rows = []
    for index, step in enumerate(block["steps"], 1):
        delay = step["delay"]
        minutes = delay["value"] * {"minutes": 1, "hours": 60, "days": 1440}[delay["unit"]]
        total += minutes
        rows.append({"cells": [str(index), _duration(minutes), _duration(total), step["message"]], "evidence": step["evidence"]})
    return rows


def _table(columns, rows, html):
    if html:
        head = "".join(f"<th scope=\"col\">{_html(column)}</th>" for column in columns + ["Evidence"])
        body = "".join("<tr>" + "".join(f"<td>{_html(cell)}</td>" for cell in row["cells"]) + f'<td>{_refs(row["evidence"], True)}</td></tr>' for row in rows)
        return f'<div class="table-scroll"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'
    header = "| " + " | ".join(_md(column) for column in columns + ["Evidence"]) + " |"
    lines = [header, "| " + " | ".join("---" for _ in columns + ["Evidence"]) + " |"]
    lines.extend("| " + " | ".join([*(_md(cell) for cell in row["cells"]), _refs(row["evidence"])]) + " |" for row in rows)
    return "\n".join(lines) + "\n"


def _mermaid_label(value):
    # Mermaid entity encoding prevents quote/directive/fence injection; IDs are
    # generated numeric aliases, never caller-controlled Mermaid syntax.
    return "".join(char if char.isalnum() or char == " " else f"#{ord(char)};" for char in value.replace("\n", " "))


def _render_block(block, html):
    kind = block["type"]
    if kind == "paragraph":
        body = f'<p>{_html(block["text"])}</p>' if html else _md(block["text"]) + "\n"
    else:
        title = _html(block["title"]) if html else _md(block["title"])
        body = f"<h3>{title}</h3>" if html else f"### {title}\n\n"
        if kind == "list":
            body += "<ul>" + "".join(f"<li>{_html(item)}</li>" for item in block["items"]) + "</ul>" if html else "\n".join("- " + _md(item) for item in block["items"]) + "\n"
        elif kind == "table":
            body += _table(block["columns"], block["rows"], html)
        elif kind == "journey":
            legend = "Automatic = solid; human = dashed; requested = dotted, not configured. State labels describe the supplied stage configuration, not effective channel readiness."
            body += f"<p>Pipeline: {_html(block['pipeline'])}</p><p>{legend}</p>" if html else f"Pipeline: {_md(block['pipeline'])}\n\n{legend}\n\n"
            stages = {stage["id"]: stage for stage in block["stages"]}
            if html:
                body += '<ul class="stages">' + "".join(f'<li class="stage {stage["state"]}">{_html(stage["label"])} <small>({_html(stage["state"])})</small></li>' for stage in block["stages"]) + "</ul>"
                for edge in block["transitions"]:
                    body += f'<div class="transition {edge["kind"]}"><span>{_html(stages[edge["from"]]["label"])}</span><span aria-label="to">&#8594;</span><span>{_html(stages[edge["to"]]["label"])}</span><p>{_html(edge["kind"])}: {_html(edge["condition"])} | Evidence: {_refs(edge["evidence"], True)}</p></div>'
            else:
                aliases = {stage["id"]: f"s{index}" for index, stage in enumerate(block["stages"])}
                lines = ["\x60\x60\x60mermaid", "flowchart TD"]
                lines.extend(f'  {aliases[stage["id"]]}["{_mermaid_label(stage["label"])} ({stage["state"]})"]' for stage in block["stages"])
                for index, edge in enumerate(block["transitions"]):
                    connector = "-->" if edge["kind"] == "automatic" else "-.->"
                    label = _mermaid_label(edge["kind"] + ": " + edge["condition"] + " [" + ", ".join(edge["evidence"]) + "]")
                    lines.append(f'  {aliases[edge["from"]]} {connector}|"{label}"| {aliases[edge["to"]]}')
                    if edge["kind"] == "human":
                        lines.append(f"  linkStyle {index} stroke-dasharray:6 4")
                    elif edge["kind"] == "requested":
                        lines.append(f"  linkStyle {index} stroke-dasharray:2 4")
                lines.append("\x60\x60\x60")
                body += "\n".join(lines) + "\n\n"
            rows = [{"cells": [stages[edge["from"]]["label"], stages[edge["to"]]["label"], edge["kind"], edge["condition"]], "evidence": edge["evidence"]} for edge in block["transitions"]]
            if rows:
                body += _table(["From", "To", "Transition", "Condition"], rows, html)
            else:
                body += "<p>No transitions supplied; no stage-order arrows inferred.</p>" if html else "No transitions supplied; no stage-order arrows inferred.\n"
        elif kind == "demo":
            fields = [("Prerequisites", block["prerequisites"]), ("Fixture plan (authorization separate)", block["fixture"])]
            for label, value in fields:
                body += f"<p><strong>{label}:</strong> {_html(value)}</p>" if html else f"**{label}:** {_md(value)}\n\n"
            for index, step in enumerate(block["steps"], 1):
                body += f'<article class="demo-step"><h4>Step {index} — script only</h4><dl>' if html else f"#### Step {index} — script only\n\n"
                for label, value in [(step["kind"].title(), step["action"]), ("Expect (configured)", step["expect"]), ("Dashboard check", step["dashboard"]), ("Why", step["why"]), ("Say", step["say"])]:
                    body += f"<dt>{label}</dt><dd>{_html(value)}</dd>" if html else f"**{label}:** {_md(value)}\n\n"
                body += f'<dt>Evidence</dt><dd>{_refs(step["evidence"], True)}</dd></dl></article>' if html else f'Evidence: {_refs(step["evidence"])}\n\n'
        elif kind == "gap":
            for label, value in [("Severity", block["severity"]), ("Observation", block["observation"]), ("Impact", block["impact"]), ("Decision needed", block["decision"])]:
                body += f"<p><strong>{label}:</strong> {_html(value)}</p>" if html else f"**{label}:** {_md(value)}\n\n"
        elif kind == "cadence":
            for field in ("channel", "start", "stop", "end", "schedule", "pause"):
                body += f"<p><strong>{field.title()}:</strong> {_html(block[field])}</p>" if html else f"**{field.title()}:** {_md(block[field])}\n\n"
            note = "Offsets below are cumulative configured delays after the previous step, not delivery promises. Sending windows, pauses and runtime conditions can defer them."
            body += f"<p>{note}</p>" if html else note + "\n\n"
            body += _table(["Step", "Delay after previous", "Cumulative offset", "Message / attachment summary"], _cadence_rows(block), html)
    return body + (f'<p class="evidence">Evidence: {_refs(block["evidence"], True)}</p>' if html else f'\nEvidence: {_refs(block["evidence"])}\n\n')


CSS = """body{margin:0;background:#f5f6f8;color:#18212d;font:16px/1.6 system-ui,sans-serif}main{max-width:1120px;margin:auto;padding:32px}h1{font-size:2.3rem;line-height:1.2}h2{margin-top:2.6rem;border-top:1px solid #dce1e8;padding-top:1.4rem}h3{margin-top:1.8rem}a{color:#174ea6}nav{display:flex;flex-wrap:wrap;gap:8px 20px;padding:16px 0}.notice,.readiness,.demo-step{background:white;border:1px solid #dce1e8;border-radius:10px;padding:18px;margin:18px 0}.notice{border-left:5px solid #e39b22}.table-scroll{overflow-x:auto}table{border-collapse:collapse;width:100%;background:white;margin:14px 0}th,td{text-align:left;vertical-align:top;padding:10px;border:1px solid #dce1e8;overflow-wrap:anywhere}th{background:#eaf0f8}p,li,dd{overflow-wrap:anywhere}dt{font-weight:700}dd{margin:0 0 12px}.evidence,small{font-size:.86rem;color:#4b586b}.stages{display:flex;flex-wrap:wrap;gap:8px;padding:0;list-style:none}.stage{border:1px solid #8996a8;padding:8px 12px;border-radius:6px}.ai{background:#dbeaff}.human{background:#ffe8c4}.won{background:#d7f0dd}.lost{background:#ffe0e0}.parked{background:#ebddff}.transition{border:2px solid #8996a8;padding:12px;margin:10px 0;display:flex;gap:14px;flex-wrap:wrap}.transition p{flex-basis:100%;margin:0}.transition.human{border-style:dashed}.transition.requested{border-style:dotted}section{scroll-margin-top:16px}@media(max-width:640px){main{padding:18px}h1{font-size:1.8rem}}@media print{body{background:white;font-size:10pt}main{max-width:none;padding:0}nav{display:none}h2,h3,h4{break-after:avoid}tr,.demo-step,.transition{break-inside:avoid}.table-scroll{overflow:visible}a{color:inherit}th{background:#eee}}"""


def _render(document, organization_name, html):
    title = organization_name + ": how the account works"
    snapshot = f'Evidence snapshot: {document["evidence_date"]} | {document["timezone"]} | {document["environment"]} | Organization {document["organization_id"]}'
    if html:
        body = f"<h1>{_html(title)}</h1><p>{_html(snapshot)}</p><p class=\"notice\">{NOTICE}</p>"
        body += '<nav aria-label="Handover sections">' + "".join(f'<a href="#{name}">{label}</a>' for name, label in SECTION_TITLES.items()) + '<a href="#sources">Source ledger</a></nav>'
    else:
        body = f"# {_md(title)}\n\n{_md(snapshot)}\n\n{NOTICE}\n\n"
        body += " | ".join(f"[{label}](#{name.replace('_', '-')})" for name, label in SECTION_TITLES.items()) + " | [Source ledger](#source-ledger)\n\n"
    readiness = document["readiness"]
    rows = [{"cells": [label, readiness[key]], "evidence": readiness["evidence"]} for label, key in [("Configuration (reported)", "configuration"), ("Engine test (reported)", "engine_test"), ("Live delivery (reported)", "live_delivery")]]
    body += "<div class=\"readiness\"><h2>Readiness reported by supplied evidence</h2>" if html else "## Readiness reported by supplied evidence\n\n"
    body += _table(["Dimension", "Status"], rows, html)
    body += f'<p>{_html(readiness["note"])}</p></div>' if html else _md(readiness["note"]) + "\n\n"
    for name, title in SECTION_TITLES.items():
        body += f'<section id="{name}"><h2>{title}</h2>' if html else f'<a id="{name.replace("_", "-")}"></a>\n\n## {title}\n\n'
        body += "".join(_render_block(block, html) for block in document["sections"][name])
        if html:
            body += "</section>"
    body += '<section id="sources"><h2>Source ledger</h2>' if html else "## Source ledger\n\n"
    for source in document["sources"]:
        title = source["id"] + " — " + source["status"]
        text = f'{source["tool"]} | {source["reference"]} | observed {source["observed_at"]}'
        body += f'<article id="source-{source["id"]}"><h3>{_html(title)}</h3><p>{_html(text)}</p><p>{_html(source["note"])}</p></article>' if html else f'### {_md(title)}\n\n{_md(text)}\n\n{_md(source["note"])}\n\n'
    if not html:
        return body
    csp = "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'"
    return '<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><meta http-equiv="Content-Security-Policy" content="' + escape(csp, quote=True) + '"><title>' + _html(organization_name + ": account handover") + "</title><style>" + CSS + "</style></head><body><main>" + body + "</section></main></body></html>"


def render_handover(document, *, organization_id, organization_name, format="html", offset=0, limit=20000):
    """Return a lossless character slice; join slices before saving or displaying."""
    _choice(format, {"html", "markdown"})
    if type(offset) is not int or not 0 <= offset <= MAX_OUTPUT_CHARS:
        _fail("Handover offset is outside its supported range.")
    if type(limit) is not int or not 1 <= limit <= MAX_CHUNK_CHARS:
        _fail("Handover chunk limit must be between 1 and 20000.")
    validate_document(document, organization_id=organization_id, organization_name=organization_name)
    rendered = _render(document, organization_name, format == "html")
    if len(rendered) > MAX_OUTPUT_CHARS:
        _fail("Rendered handover exceeds the output size bound; reduce the document.")
    if offset > len(rendered):
        _fail("Handover offset exceeds the rendered artifact length.")
    chunk = rendered[offset:offset + limit]
    end = offset + len(chunk)
    canonical = json.dumps({"organization_name": organization_name, "document": document}, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return {
        "organization_id": organization_id,
        "schema_version": 1,
        "format": format,
        "mime_type": "text/html" if format == "html" else "text/markdown",
        "suggested_filename": "account-handover-" + organization_id + (".html" if format == "html" else ".md"),
        "text": chunk,
        "offset": offset,
        "returned_chars": len(chunk),
        "total_chars": len(rendered),
        "next_offset": end if end < len(rendered) else None,
        "truncated": end < len(rendered),
        "document_sha256": sha256(canonical.encode("utf-8")).hexdigest(),
        "artifact_sha256": sha256(rendered.encode("utf-8")).hexdigest(),
        "chunk_sha256": sha256(chunk.encode("utf-8")).hexdigest(),
        "validation": {
            "tenant_bound": True,
            "evidence_references_checked": True,
            "evidence_independently_verified": False,
            "script_only": True,
            "persisted": False,
        },
    }
