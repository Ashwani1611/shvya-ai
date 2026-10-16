"""Small, positive HTML allowlist for the editorial rich-text format.

Input is parsed and emitted as new escaped markup. No original tag or attribute
is copied verbatim, and images must point to owned blog media IDs.
"""
import re
from html import escape
from html.parser import HTMLParser
from urllib.parse import urlsplit
from uuid import UUID

from django.core.exceptions import ValidationError

ALLOWED_TAGS = {
    "p", "h2", "h3", "h4", "strong", "em", "u", "s", "ul", "ol", "li", "blockquote",
    "hr", "br", "a", "img", "figure", "figcaption", "table", "thead", "tbody", "tfoot",
    "tr", "th", "td", "pre", "code", "div", "span", "sup", "sub",
}
VOID_TAGS = {"br", "hr", "img"}
DROP_CONTENT = {"script", "style", "iframe", "object", "embed", "svg", "math", "template",
                "form", "input", "button", "textarea", "select", "noscript"}
IMAGE_PATH = re.compile(r"^/(?:superadmin/)?blog/media/([0-9a-fA-F-]{36})/?$")
MAX_HTML_LENGTH = 300_000


def image_id_from_url(value):
    try:
        parts = urlsplit(str(value or ""))
    except ValueError:
        return None
    if parts.scheme or parts.netloc or parts.fragment:
        return None
    match = IMAGE_PATH.fullmatch(parts.path)
    if not match:
        return None
    try:
        return str(UUID(match.group(1)))
    except ValueError:
        return None


def safe_link(value):
    value = str(value or "").strip()
    if not value or any(ord(c) < 32 for c in value) or "\\" in value:
        return ""
    try:
        parts = urlsplit(value)
    except ValueError:
        return ""
    if parts.scheme in {"http", "https"} and parts.hostname and not parts.username and not parts.password:
        return value
    if not parts.scheme and not parts.netloc and value.startswith(("/", "#")) and not value.startswith("//"):
        return value
    return ""


class _Sanitizer(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.stack = []
        self.blocked = []

    def handle_starttag(self, tag, attrs):
        tag = {"b": "strong", "i": "em", "strike": "s"}.get(tag, tag)
        if self.blocked:
            if tag not in VOID_TAGS and tag not in {"input", "embed"}:
                self.blocked.append(tag)
            return
        if tag in DROP_CONTENT:
            if tag not in {"input", "embed"}:
                self.blocked.append(tag)
            return
        if tag not in ALLOWED_TAGS:
            return
        attrs = dict(attrs)
        clean = {}
        if tag == "a":
            href = safe_link(attrs.get("href"))
            if href:
                clean["href"] = href
                clean["rel"] = "noopener noreferrer"
        if tag == "img":
            image_id = image_id_from_url(attrs.get("src"))
            if not image_id:
                return
            clean.update(src=f"/blog/media/{image_id}/", alt=str(attrs.get("alt") or "")[:300],
                         loading="lazy", decoding="async")
            for key in ("width", "height"):
                if str(attrs.get(key, "")).isdigit():
                    clean[key] = str(min(max(int(attrs[key]), 1), 4000))
        if tag in {"th", "td"}:
            for key in ("colspan", "rowspan"):
                if str(attrs.get(key, "")).isdigit():
                    clean[key] = str(min(max(int(attrs[key]), 1), 20))
        if tag in {"div", "p", "aside", "blockquote"} and attrs.get("class") in {"callout", "callout-note"}:
            clean["class"] = attrs["class"]
        rendered = "".join(f' {key}="{escape(value, quote=True)}"' for key, value in clean.items())
        self.parts.append(f"<{tag}{rendered}>")
        if tag not in VOID_TAGS:
            self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID_TAGS and tag not in {"input", "embed"}:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        tag = {"b": "strong", "i": "em", "strike": "s"}.get(tag, tag)
        if self.blocked:
            if tag in self.blocked:
                self.blocked = self.blocked[:self.blocked.index(tag)]
            return
        if tag in self.stack:
            while self.stack:
                current = self.stack.pop()
                self.parts.append(f"</{current}>")
                if current == tag:
                    break

    def handle_data(self, data):
        if not self.blocked:
            self.parts.append(escape(data, quote=False))

    def result(self):
        return "".join(self.parts + [f"</{tag}>" for tag in reversed(self.stack)])


def sanitize_html(value):
    value = str(value or "")
    if len(value) > MAX_HTML_LENGTH:
        raise ValidationError({"body_html": "Article body must be no larger than 300,000 characters."})
    parser = _Sanitizer()
    try:
        parser.feed(value)
        parser.close()
    except (ValueError, RecursionError) as exc:
        raise ValidationError({"body_html": "The article contains invalid HTML."}) from exc
    return parser.result()


class _ImageCollector(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = set()

    def handle_starttag(self, tag, attrs):
        if tag == "img":
            image_id = image_id_from_url(dict(attrs).get("src"))
            if image_id:
                self.ids.add(image_id)


def snapshot_image_ids(snapshot):
    collector = _ImageCollector()
    collector.feed(str(snapshot.get("body_html") or ""))
    ids = collector.ids
    for key in ("cover_image", "social_image"):
        value = snapshot.get(key)
        if value:
            try:
                ids.add(str(UUID(str(value))))
            except (TypeError, ValueError, AttributeError) as exc:
                raise ValidationError({key: "Choose an uploaded blog image."}) from exc
    return ids
