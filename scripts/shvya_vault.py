#!/usr/bin/env python3
"""Small, dependency-free client for SHVYA's organization-scoped Vault API."""

import argparse
from datetime import date
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import UUID


SECTIONS = (
    "website", "brochures", "media", "offerings", "basics", "faqs", "team",
    "qualification", "handoff", "blacklist", "rules", "proof", "offers",
    "scripts", "other",
)
ORIGINS = ("fireflies", "whatsapp", "ops_chat", "other")
MAX_JSON_BYTES = 64 * 1024
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
MAX_FILE_BYTES = 50 * 1024 * 1024
API_TIMEOUT = 30
DOWNLOAD_TIMEOUT = 120


class VaultError(Exception):
    pass


class NoRedirects(HTTPRedirectHandler):
    """Never forward a Bearer token (or a signed download) through a redirect."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def origin(url):
    try:
        parsed = urlsplit(url)
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError as exc:
        raise VaultError("The Vault URL is invalid.") from exc
    return parsed.scheme, parsed.hostname, port


class VaultClient:
    def __init__(self):
        self.token = os.environ.get("VAULT_TOKEN", "")
        if not re.fullmatch(r"sv_[A-Za-z0-9_-]{40,100}", self.token):
            raise VaultError("Set VAULT_TOKEN to a current sv_ token from the SHVYA administrator.")
        base = os.environ.get("VAULT_URL", "https://shvya-ai.com").rstrip("/")
        try:
            parsed = urlsplit(base)
        except ValueError:
            raise VaultError("VAULT_URL must be a valid server origin.") from None
        if (not parsed.hostname or parsed.username or parsed.password or parsed.query
                or parsed.fragment or parsed.path not in {"", "/"}):
            raise VaultError("VAULT_URL must be the server origin, without a path or credentials.")
        if parsed.scheme != "https" and not (
            parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        ):
            raise VaultError("VAULT_URL must use HTTPS; HTTP is supported only for local development.")
        self.base = base
        self.origin = origin(base)
        self.opener = build_opener(NoRedirects())

    def _open(self, request, timeout):
        try:
            return self.opener.open(request, timeout=timeout)
        except HTTPError as exc:
            code = exc.code
            exc.close()
            if code == 401:
                raise VaultError("Vault token expired or revoked. Ask for a fresh token; do not retry.") from None
            if 300 <= code < 400:
                raise VaultError("Vault redirected the request. Set VAULT_URL to its canonical origin; redirects are blocked.") from None
            if code == 409:
                raise VaultError("Vault conflict: review the existing client content before changing this item.") from None
            if code in {403, 404} and "/api/agent/" not in urlsplit(request.full_url).path:
                raise VaultError("File access expired or was revoked. Run pull again for fresh download links.") from None
            raise VaultError(f"Vault returned HTTP {code}. Check access and submitted fields.") from None
        except (URLError, TimeoutError, OSError):
            # Exceptions can contain complete signed URLs; never print them.
            raise VaultError("Vault unreachable or request timed out. Check the server and connection.") from None

    def request(self, endpoint, data=None, markdown=False):
        body = None if data is None else json.dumps(data, ensure_ascii=False).encode("utf-8")
        if body is not None and len(body) > MAX_JSON_BYTES:
            raise VaultError("Request body exceeds the API's 64 KiB limit.")
        headers = {"Authorization": "Bearer " + self.token,
                   "Accept": "text/markdown" if markdown else "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = Request(self.base + "/vault/api/agent/" + endpoint, data=body,
                          headers=headers, method="GET" if body is None else "POST")
        try:
            with self._open(request, API_TIMEOUT) as response:
                payload = response.read(MAX_RESPONSE_BYTES + 1)
        except (URLError, TimeoutError, OSError):
            raise VaultError("Vault response was interrupted. Check the connection and try again.") from None
        if len(payload) > MAX_RESPONSE_BYTES:
            raise VaultError("Vault response is too large to process safely.")
        try:
            result = payload.decode("utf-8")
            if markdown:
                return result
            result = json.loads(result)
            if not isinstance(result, dict):
                raise ValueError
            return result
        except (UnicodeError, ValueError):
            raise VaultError("Vault returned an invalid response.") from None

    def download(self, url, destination, expected_size=None):
        try:
            target = urljoin(self.base + "/", url)
            parsed = urlsplit(target)
        except ValueError:
            raise VaultError("Vault returned an invalid download URL.") from None
        if parsed.username or parsed.password or parsed.fragment or origin(target) != self.origin:
            raise VaultError("Refusing a file download outside the configured Vault origin.")
        if not parsed.path.startswith("/vault/files/"):
            raise VaultError("Refusing a download outside the protected Vault file route.")
        # The short-lived signed URL authorizes this request. No Bearer header is sent.
        request = Request(target, headers={"Accept": "application/octet-stream"})
        temporary = None
        try:
            with self._open(request, DOWNLOAD_TIMEOUT) as response:
                with tempfile.NamedTemporaryFile(dir=destination.parent, prefix=".vault-", delete=False) as output:
                    temporary = Path(output.name)
                    total = 0
                    while True:
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > MAX_FILE_BYTES:
                            raise VaultError("Download exceeds the 50 MiB file limit.")
                        output.write(chunk)
            if expected_size is not None and total != expected_size:
                raise VaultError("Downloaded file size does not match the Vault manifest. Run pull again.")
            os.replace(temporary, destination)
        except (URLError, TimeoutError, OSError):
            raise VaultError("File download failed. Check the connection and writable output directory.") from None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


def clean_display(value):
    """Do not let remote names print terminal escape sequences."""
    return "".join(c if c.isprintable() else " " for c in str(value))


def directory(parent, name):
    child = parent / name
    if child.is_symlink():
        raise VaultError("Refusing a symbolic link inside the output directory.")
    child.mkdir(mode=0o700, exist_ok=True)
    if not child.is_dir() or child.resolve().parent != parent.resolve():
        raise VaultError("Unsafe output directory.")
    return child


def write_private(path, content):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".vault-", delete=False) as output:
            temporary = Path(output.name)
            output.write(content.encode("utf-8"))
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def without_download_secrets(value):
    """Keep useful metadata, never persist ephemeral signed download credentials."""
    if isinstance(value, dict):
        return {key: without_download_secrets(item) for key, item in value.items()
                if key not in {"download_url", "audio_url"}}
    if isinstance(value, list):
        return [without_download_secrets(item) for item in value]
    return value


def status(client):
    workspace = client.request("workspace")
    print(f"{clean_display(workspace.get('name', 'Vault'))}: {clean_display(workspace.get('status', 'unknown'))}")
    print(f"Sections marked done: {workspace.get('completed_sections', 0)}/15; open questions: {workspace.get('open_questions', 0)}")
    for section in workspace.get("sections", []):
        marker = {"filled": "x", "dont_have": "-", "empty": " "}.get(section.get("state"), "?")
        print(f"[{marker}] {clean_display(section.get('key', ''))}: {section.get('entry_count', 0)} entries")


def pull(client, out):
    root = Path(out).expanduser().resolve()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    workspace = client.request("workspace")
    markdown = client.request("export.md", markdown=True)
    files = directory(root, "files")
    downloaded = skipped = 0
    local_files = {}
    for entry in workspace.get("entries", []):
        if not isinstance(entry, dict):
            raise VaultError("Vault returned invalid entry metadata.")
        urls = [(kind, entry.get(key)) for kind, key in (("file", "download_url"), ("audio", "audio_url")) if entry.get(key)]
        if not urls:
            continue
        section = entry.get("section")
        if section not in SECTIONS:
            raise VaultError("Vault returned an unknown section for a file.")
        try:
            entry_id = str(UUID(entry["id"]))
        except (KeyError, TypeError, ValueError, AttributeError):
            raise VaultError("Vault returned an invalid file entry ID.") from None
        name = re.sub(r"[^A-Za-z0-9._-]+", "_", str(entry.get("file_name") or "attachment"))[:140].strip(".") or "attachment"
        size = entry.get("size", entry.get("file_size"))
        if size is not None and (type(size) is not int or size < 0 or size > MAX_FILE_BYTES):
            raise VaultError("Vault returned an invalid file size.")
        folder = directory(files, section)
        for kind, url in urls:
            if not isinstance(url, str):
                raise VaultError("Vault returned an invalid download URL.")
            destination = folder / f"{entry_id}-{kind}-{name}"
            if destination.is_symlink():
                raise VaultError("Refusing a symbolic link at the download destination.")
            if destination.is_file() and size is not None and destination.stat().st_size == size:
                skipped += 1
            else:
                client.download(url, destination, expected_size=size)
                downloaded += 1
            local_files[f"{entry_id}:{kind}"] = str(destination.relative_to(root))
    manifest = without_download_secrets(workspace)
    manifest["local_files"] = local_files
    write_private(root / "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    write_private(root / "export.md", markdown)
    print(f"Pulled {clean_display(workspace.get('name', 'Vault'))}: export.md, manifest.json, {downloaded} files downloaded, {skipped} already present.")


def iso_date(value):
    try:
        if date.fromisoformat(value).isoformat() != value:
            raise ValueError
        return value
    except ValueError:
        raise argparse.ArgumentTypeError("Use a date in YYYY-MM-DD format.") from None


def text_input(value=None, filename=None, stdin=False):
    if filename:
        with Path(filename).open(encoding="utf-8") as source:
            value = source.read(MAX_JSON_BYTES + 1)
    elif value is None and stdin and not sys.stdin.isatty():
        value = sys.stdin.read(MAX_JSON_BYTES + 1)
    if value is None or not value.strip():
        raise VaultError("Provide non-empty text using the command option, a UTF-8 file, or note's standard input.")
    if len(value.encode("utf-8")) > MAX_JSON_BYTES:
        raise VaultError("Input text exceeds the API's 64 KiB request limit.")
    return value.strip()


def parser():
    result = argparse.ArgumentParser(description=__doc__, epilog="Authentication: VAULT_TOKEN=sv_…; optional VAULT_URL=https://shvya-ai.com. Tokens are never printed or saved.")
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("status", help="Show section states and outstanding questions")
    pull_parser = commands.add_parser("pull", help="Export Markdown, metadata and protected attachments")
    pull_parser.add_argument("--out", default="./vault", help="Private local output directory (default: ./vault)")
    note = commands.add_parser("note", help="Create/update one sourced note; pipe text when no body/file is supplied")
    note.add_argument("--section", required=True, choices=SECTIONS)
    note.add_argument("--origin", required=True, choices=ORIGINS)
    note.add_argument("--date", type=iso_date)
    note.add_argument("--external-id")
    note_text = note.add_mutually_exclusive_group()
    note_text.add_argument("--body")
    note_text.add_argument("--file", help="Read the note body from a UTF-8 file")
    ask = commands.add_parser("ask", help="Post/update one question for a missing fact")
    ask.add_argument("--section", default="other", choices=SECTIONS)
    ask.add_argument("--text", required=True)
    ask.add_argument("--external-id")
    call = commands.add_parser("call", help="Record/update a call and its provenance")
    call.add_argument("--title", required=True)
    call.add_argument("--date", required=True, type=iso_date)
    call.add_argument("--url")
    call.add_argument("--duration", type=int, help="Duration in minutes (0–10080)")
    call.add_argument("--attendee", action="append", default=[])
    call.add_argument("--external-id")
    call.add_argument("--hide-recording", action="store_true")
    summary = call.add_mutually_exclusive_group()
    summary.add_argument("--summary")
    summary.add_argument("--summary-file")
    return result


def main(argv=None):
    arguments = parser().parse_args(argv)
    try:
        client = VaultClient()
        if arguments.command == "status":
            status(client)
            return 0
        if arguments.command == "pull":
            pull(client, arguments.out)
            return 0
        if arguments.command == "note":
            data = {"section": arguments.section, "origin": arguments.origin,
                    "body": text_input(arguments.body, arguments.file, stdin=True)}
            if arguments.date:
                data["source_date"] = arguments.date
            endpoint = "entries"
        elif arguments.command == "ask":
            data = {"section": arguments.section, "text": text_input(arguments.text)}
            endpoint = "questions"
        else:
            if arguments.duration is not None and not 0 <= arguments.duration <= 10080:
                raise VaultError("Duration must be between 0 and 10080 minutes.")
            data = {"title": arguments.title, "date": arguments.date,
                    "attendees": arguments.attendee, "share_recording": not arguments.hide_recording}
            if arguments.url:
                data["url"] = arguments.url
            if arguments.duration is not None:
                data["duration_min"] = arguments.duration
            if arguments.summary is not None or arguments.summary_file:
                data["summary"] = text_input(arguments.summary, arguments.summary_file)
            endpoint = "calls"
        if arguments.external_id:
            data["external_id"] = arguments.external_id
        response = client.request(endpoint, data)
        verb = "Updated" if response.get("updated") else "Created"
        print(f"{verb} {arguments.command} {clean_display(response.get('id', ''))}.")
        return 0
    except VaultError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except (OSError, UnicodeError):
        print("Unable to read or write the requested local files.", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Cancelled.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
