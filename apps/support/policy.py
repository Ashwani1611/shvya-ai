"""Pure ticket policies, shared by browser, mail, worker and service paths."""
from __future__ import annotations

import hashlib
import hmac
import re
from datetime import datetime, timedelta
from pathlib import PurePath
from urllib.parse import urlsplit

STATES = ("open", "in_progress", "answered", "on_hold", "closed")
DEFAULT_EXTENSIONS = (
    "jpg,jpeg,png,gif,webp,heic,pdf,txt,csv,doc,docx,xls,xlsx,ppt,pptx,"
    "odt,ods,rtf,mp3,m4a,wav,ogg,opus,webm,mp4,mov,zip,rar,7z,log,json"
)
BLOCKED_EXTENSIONS = frozenset((
    "exe", "com", "msi", "bat", "cmd", "ps1", "sh", "bash", "php", "phtml",
    "py", "js", "html", "htm", "svg", "scr", "vbs", "jar", "dll", "lnk", "hta",
))
REFERENCE_RE = re.compile(r"\bSHV-[A-F0-9]{12}\b", re.I)


def platform_staff(user) -> bool:
    return bool(user and getattr(user, "is_authenticated", False)
                and getattr(user, "is_active", False)
                and getattr(user, "is_staff", False)
                and getattr(user, "is_superuser", False)
                and getattr(user, "organization_id", None) is None)


def after_reply(current_behavior: str, *, staff: bool, selected: str | None = None) -> str:
    """Names are cosmetic; stable behavior codes determine transitions."""
    if current_behavior not in STATES:
        raise ValueError("Unknown status behavior.")
    if staff:
        if selected is not None and selected not in STATES:
            raise ValueError("Unknown reply status.")
        return selected or "answered"
    return "in_progress" if current_behavior == "in_progress" else "open"


def may_autoclose(behavior: str, last_activity: datetime, now: datetime, hours: int) -> bool:
    return bool(hours > 0 and behavior in STATES and behavior not in ("in_progress", "on_hold", "closed")
                and last_activity <= now - timedelta(hours=hours))


def may_read_customer(*, actor_org, ticket_org, actor_id, requester_id,
                      own_only: bool, organization_admin: bool) -> bool:
    return bool(actor_org and str(actor_org) == str(ticket_org)
                and (not own_only or organization_admin or str(actor_id) == str(requester_id)))


def safe_filename(name: str) -> str:
    name = PurePath(str(name).replace("\\", "/")).name
    name = re.sub(r"[\x00-\x1f\x7f]", "", name).strip(" .")
    if not name:
        raise ValueError("The attachment must have a filename.")
    return name[-180:]


def validate_file(name: str, size: int, allowed: str, max_bytes: int) -> str:
    name = safe_filename(name)
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    allowed_set = {x.strip().lower().lstrip(".") for x in allowed.split(",") if x.strip()}
    # Block executable suffixes even if an administrator accidentally includes them.
    if ext in BLOCKED_EXTENSIONS or ext not in allowed_set:
        raise ValueError(f"The .{ext or '(none)'} file type is not permitted.")
    if not isinstance(size, int) or size < 1 or size > max_bytes:
        raise ValueError("Attachment is empty or exceeds the configured file limit.")
    return name


def normalized_email(address: str) -> str:
    address = str(address).strip().lower()
    if len(address) > 254 or not re.fullmatch(r"[^\s<>@,;]+@[^\s<>@,;]+\.[^\s<>@,;]+", address):
        raise ValueError("A single valid sender email is required.")
    return address


def safe_source_path(value: str) -> str:
    # Never snapshot query strings (which might contain tokens), fragments or other sites.
    parsed = urlsplit(value or "")
    path = parsed.path if not parsed.netloc and not parsed.scheme else ""
    if not path.startswith("/dashboard/") or "\\" in path or any(ord(c) < 32 for c in path):
        return "/dashboard/support-portal/"
    return path[:300]


def digest_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def valid_token(raw: str, digest: str) -> bool:
    return bool(len(raw) >= 32 and hmac.compare_digest(digest_token(raw), digest))


def safe_csv(value) -> str:
    text = str(value if value is not None else "")
    risky = text.startswith(("\t", "\r", "\n")) or text.lstrip().startswith(("=", "+", "-", "@"))
    return "'" + text if risky else text


def valid_knowledge_url(value: str) -> bool:
    try:
        url = urlsplit(value)
        return url.scheme == "https" and bool(url.hostname) and not url.username and not url.password
    except ValueError:
        return False


def html_email_text(value: str) -> str:
    """Convert an email HTML alternative to inert text, not browser-safe HTML."""
    from html.parser import HTMLParser

    class TextParser(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.parts = []
            self.ignored = 0
        def handle_starttag(self, tag, attrs):
            if tag in ('script', 'style', 'head'):
                self.ignored += 1
            if not self.ignored and tag in ('p', 'div', 'br', 'li', 'tr'):
                self.parts.append('\n')
        def handle_endtag(self, tag):
            if tag in ('script', 'style', 'head') and self.ignored:
                self.ignored -= 1
            if not self.ignored and tag in ('p', 'div', 'li', 'tr'):
                self.parts.append('\n')
        def handle_data(self, data):
            if not self.ignored:
                self.parts.append(data)
    parser = TextParser()
    parser.feed(value)
    parser.close()
    return '\n'.join(line.strip() for line in ''.join(parser.parts).splitlines() if line.strip())
