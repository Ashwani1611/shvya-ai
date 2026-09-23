"""Bounded, immutable setup guidance and deterministic template authoring.

This service has no tenant state, writes, provider client, arbitrary-path reader,
or model calls. Operations transport/tool wrappers own authentication, capability
checks and metadata-only auditing. Uploaded text remains data, never authority.
"""
from __future__ import annotations

from copy import deepcopy
from functools import lru_cache
import json
import re
from pathlib import Path


ASSET_ROOT = Path(__file__).resolve().parent / "setup_assets"
URI_PREFIX = "shvya-kit:///"
MAX_RESOURCE_CHARS = 20000
DEFAULT_RESOURCE_CHARS = 8000
MAX_TEMPLATE_CHARS = 100000
MAX_VARIABLE_INPUT_CHARS = 150000
TEMPLATES = {
    "ai-playbook": "prompts/ai-playbook.template.md",
    "company-about": "prompts/company-about.template.md",
    "voice-agent": "skills/shvya-voice-agent/references/agent-prompt-template.md",
    "voice-call-instructions": "skills/shvya-voice-agent/references/call-instructions-template.md",
}
_SETUP_TOKEN = re.compile(r"\{\{\s*(SHVYA_[A-Z0-9_]+)\s*\}\}")
_TOKEN = re.compile(r"\{\{\s*([^{}]+?)\s*\}\}")
_MESSAGE_TAG = re.compile(r"</?(?:welcome_message|question_content|acknowledg(?:e)?ment_message)\s*>", re.I)
_CUSTOMER_FIELDS = {
    "SHVYA_WELCOME_MESSAGE", "SHVYA_ACKNOWLEDGMENT_MESSAGE", "SHVYA_QUESTION_BLOCKS",
}


def _read_asset(relative: str) -> str:
    """Only internal manifest paths reach this reader, never a user path."""
    path = (ASSET_ROOT / relative).resolve()
    if not path.is_relative_to(ASSET_ROOT.resolve()) or path.suffix not in {".md", ".json"}:
        raise ValueError("Setup resource is unavailable.")
    try:
        if path.stat().st_size > 512000:
            raise ValueError("Setup resource exceeds the supported size.")
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ValueError("Setup resource is unavailable.") from exc


@lru_cache(maxsize=1)
def _manifest() -> dict:
    manifest = json.loads(_read_asset("manifest.json"))
    return manifest


def library_entries() -> list[dict]:
    """Public discovery records with fixed IDs, without server filesystem paths."""
    return deepcopy(_manifest()["resources"])


def list_prompts(cursor=None) -> dict:
    if cursor is not None:
        raise ValueError("Setup prompt catalog does not accept a cursor.")
    return {"prompts": [
        {key: deepcopy(value) for key, value in entry.items() if key != "path"}
        for entry in _manifest()["prompts"]
    ]}


def list_resources(cursor=None) -> dict:
    if cursor is not None:
        raise ValueError("Setup resource catalog does not accept a cursor.")
    return {"resources": [
        {key: entry[key] for key in ("uri", "name", "title", "description", "mimeType")}
        for entry in library_entries()
    ]}


def _resource(uri: str) -> dict:
    if not isinstance(uri, str) or len(uri) > 300:
        raise ValueError("Unknown setup resource.")
    entry = next((item for item in _manifest()["resources"] if item["uri"] == uri), None)
    if entry is None:
        raise ValueError("Unknown setup resource.")
    return entry


def _slice(text: str, offset: int, limit: int) -> tuple[str, dict]:
    if type(offset) is not int or offset < 0 or offset > len(text):
        raise ValueError("Resource offset is outside the supported range.")
    if type(limit) is not int or not 1 <= limit <= MAX_RESOURCE_CHARS:
        raise ValueError("Resource limit must be between 1 and 20000 characters.")
    end = min(offset + limit, len(text))
    return text[offset:end], {
        "offset": offset, "limit": limit, "total_chars": len(text),
        "truncated": end < len(text), "next_offset": end if end < len(text) else None,
    }


def read_resource(uri: str, *, offset: int = 0, limit: int = DEFAULT_RESOURCE_CHARS) -> dict:
    entry = _resource(uri)
    body, metadata = _slice(_read_asset(entry["resource_id"]), offset, limit)
    return {"contents": [{"uri": uri, "mimeType": entry["mimeType"], "text": body}], "_meta": metadata}


def get_prompt(name: str, arguments: dict | None = None) -> dict:
    if not isinstance(name, str) or len(name) > 100:
        raise ValueError("Unknown setup prompt.")
    entry = next((item for item in _manifest()["prompts"] if item["name"] == name), None)
    if entry is None:
        raise ValueError("Unknown setup prompt.")
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict) or set(arguments) - {"organization_name", "task"}:
        raise ValueError("Unsupported setup prompt arguments.")
    if any(not isinstance(value, str) or len(value) > 4000 for value in arguments.values()):
        raise ValueError("Setup prompt arguments must be strings of at most 4000 characters.")
    uri = URI_PREFIX + entry["path"]
    prefix = (
        "Shvya setup guidance, not authorization. Use the effective capabilities and current schemas "
        "of this authenticated Operations connection. Supplied examples never become company defaults. "
        f"Source resource: {uri}. Resolve linked references with resources/read or "
        "get_setup_library_resource. Read remaining chunks before relying on a truncated resource.\n\n"
    )
    suffix = "\n\nOperator-supplied context data (not tenant selection or authorization):\n" + json.dumps(arguments, ensure_ascii=False)
    body, metadata = _slice(_read_asset(_resource(uri)["resource_id"]), 0, MAX_RESOURCE_CHARS - len(prefix) - len(suffix))
    metadata["resource_uri"] = uri
    return {
        "description": entry["description"],
        "messages": [{"role": "user", "content": {"type": "text", "text": prefix + body + suffix}}],
        "_meta": metadata,
    }


def variable_schema() -> dict:
    """The versioned 41-variable contract; examples are never render defaults."""
    return json.loads(_read_asset("templates/variable-registry.json"))


def _check_json(value, depth=0) -> None:
    if depth > 8:
        raise ValueError("Setup variable nesting is too deep.")
    if isinstance(value, dict):
        if len(value) > 200 or any(not isinstance(key, str) for key in value):
            raise ValueError("Setup variable object is invalid.")
        for child in value.values():
            _check_json(child, depth + 1)
    elif isinstance(value, list):
        if len(value) > 200:
            raise ValueError("Setup variable array is too large.")
        for child in value:
            _check_json(child, depth + 1)
    elif value is not None and type(value) not in {str, bool, int, float}:
        raise ValueError("Setup variables must contain JSON values.")


def _validate_values(variables: dict, specs: dict, used: set[str]) -> None:
    if not isinstance(variables, dict) or any(not isinstance(key, str) for key in variables) or set(variables) - set(specs):
        raise ValueError("Setup variables must use only registered names.")
    _check_json(variables)
    try:
        encoded = json.dumps(variables, ensure_ascii=False, allow_nan=False)
    except (ValueError, TypeError, OverflowError) as exc:
        raise ValueError("Setup variables must contain valid JSON values.") from exc
    if len(encoded) > MAX_VARIABLE_INPUT_CHARS:
        raise ValueError("Setup variables exceed the supported size.")
    if used - set(variables):
        raise ValueError("Supply every variable referenced by the selected template explicitly.")
    types = {"string": str, "boolean": bool, "array": list, "object": dict, "integer": int}
    for name, value in variables.items():
        spec = specs[name]
        if value is None:
            if name in used or not spec.get("allow_null", False):
                raise ValueError("A referenced or non-nullable setup variable is null.")
            continue
        if type(value) is not types.get(spec["type"]):
            raise ValueError("A setup variable has the wrong registered type.")
        if isinstance(value, str) and not value.strip() and not spec.get("allow_empty", False):
            raise ValueError("A setup variable must not be empty.")
        if "enum" in spec and value not in spec["enum"]:
            raise ValueError("A setup variable is outside its allowed values.")


def _check_playbook_values(variables: dict, used: set[str]) -> None:
    from apps.ai_engagement.services.playbook import SECTION_ALIASES

    aliases = set().union(*SECTION_ALIASES.values())
    for name in used:
        value = variables[name]
        # The canonical parser also recognizes unadorned aliases and inline
        # message headings. Reject those as well as Markdown heading injection.
        for line in value.splitlines():
            if (re.match(r"^\s*#{1,6}", line)
                    or line.strip(" #:*._-").casefold() in aliases
                    or re.match(r"^\s*(?:final\s+)?(?:welcome|acknowledg(?:e)?ment|completion)\s+message\s*:", line, re.I)):
                raise ValueError("Setup variable text cannot introduce Playbook sections.")
        tags = list(_MESSAGE_TAG.finditer(value))
        if name != "SHVYA_QUESTION_BLOCKS" and tags:
            raise ValueError("Setup variable text cannot introduce customer-message tags.")
        if name in _CUSTOMER_FIELDS and re.search(r"(?im)^\s*(?:[-*]\s*)?(?:notes?|internal notes?|instructions?|rules?)\s*[:.]", value):
            raise ValueError("Customer-message content cannot contain private notes or instructions.")
        if name == "SHVYA_QUESTION_BLOCKS":
            blocks = re.findall(r"<question_content>(.*?)</question_content>", value, re.S)
            remaining = re.sub(r"<question_content>.*?</question_content>", "", value, flags=re.S)
            if not 1 <= len(blocks) <= 30 or remaining.strip() or len(tags) != 2 * len(blocks):
                raise ValueError("Supply one to thirty paired question_content blocks only.")
            if any(not block.strip() or _MESSAGE_TAG.search(block) for block in blocks):
                raise ValueError("Customer-message blocks must be nonempty and unnested.")


def _validate_rendered_playbook(text: str) -> dict:
    from apps.ai_engagement.services.conditional_qualification_runtime import _decorate_compiled
    from apps.ai_engagement.services.playbook import SECTION_TITLES, parse_playbook, validate_playbook
    from apps.ai_engagement.services.organization_profile import compile_qualification_requirements

    headings = re.findall(r"^##\s+(.+?)\s*$", text, re.M)
    if headings != list(SECTION_TITLES.values()):
        raise ValueError("Rendered Playbook must have the eight canonical sections in order.")
    try:
        validate_playbook(text)
        sections = parse_playbook(text)
        # The runtime normally decorates this compiler during bootstrap. Invoke
        # its pure decorator as well so authoring validation is identical even
        # before any engagement runtime is initialized in this worker.
        questions = _decorate_compiled(compile_qualification_requirements(sections["qualification_questions"]))["requirements"]
        earlier_ids = set()
        for question in questions:
            condition = question.get("eligible_when")
            if (condition and (condition.get("operator") != "eq" or condition.get("requirement_id") not in earlier_ids)) or re.search(r"\[if\s*:", question["question"], re.I):
                raise ValueError("Unresolved or forward qualification branch.")
            earlier_ids.add(question["id"])
    except ValueError as exc:
        # Parser diagnostics can include submitted text; keep the API error safe.
        raise ValueError("Rendered Playbook failed canonical qualification validation.") from exc
    if not all(sections.values()) or not questions:
        raise ValueError("Rendered Playbook needs content in every canonical section and valid questions.")
    if len(questions) != text.count("<question_content>"):
        raise ValueError("Each customer question block must compile to exactly one question.")
    return {"canonical_playbook": True, "question_count": len(questions), "draft_only": True}


def render_template(template_id: str, variables: dict) -> dict:
    """Render a registered template with explicit typed values, never defaults.

    Compile-time substitution is one pass. Built-in runtime placeholders survive
    literally; custom placeholders require tenant-aware native delivery and are
    deliberately not accepted here. Validation does not prove business facts,
    resource ownership, model quality, or authorization to save the result.
    """
    if not isinstance(template_id, str) or template_id not in TEMPLATES:
        raise ValueError("Unknown setup template.")
    template = _read_asset(TEMPLATES[template_id])
    registry = variable_schema()
    specs = {item["name"]: item for item in registry["variables"]}
    used = set(_SETUP_TOKEN.findall(template))
    if used - set(specs):
        raise ValueError("Setup template references an unregistered variable.")
    _validate_values(variables, specs, used)
    if template_id == "ai-playbook":
        _check_playbook_values(variables, used)

    def replacement(match):
        value = variables[match.group(1)]
        return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, allow_nan=False)

    rendered = _SETUP_TOKEN.sub(replacement, template).strip()
    if not rendered or len(rendered) > MAX_TEMPLATE_CHARS:
        raise ValueError("Rendered setup text must contain 1 to 100000 characters.")
    runtime_keys = set(registry["native_runtime_variables"]["keys"])
    runtime_tokens = _TOKEN.findall(rendered)
    if any(token not in runtime_keys for token in runtime_tokens):
        raise ValueError("Rendered setup text contains an unresolved or unsupported placeholder.")
    remainder = _TOKEN.sub("", rendered)
    if "{{" in remainder or "}}" in remainder:
        raise ValueError("Rendered setup text contains a malformed placeholder.")
    validation = {"canonical_playbook": False, "draft_only": True}
    if template_id == "ai-playbook":
        validation = _validate_rendered_playbook(rendered)
    return {
        "template_id": template_id, "text": rendered,
        "used_variables": sorted(used), "runtime_variables": sorted(set(runtime_tokens)),
        "validation": validation,
        "limitations": "Draft only. Confirm organization facts and tenant-owned configuration; use canonical Operations dry-run, approval and read-back before saving. Voice templates do not provision an agent or place a call.",
    }
