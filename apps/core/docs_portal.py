"""Public, tenant-neutral SHVYA documentation catalogue and safe renderer.

Only curated static JSON articles are published. Internal engineering runbooks,
provider payloads, tokens and private organization data must not be loaded here.
"""
from __future__ import annotations

import html
import json
import re
from pathlib import Path

from django.http import Http404
from django.utils.safestring import mark_safe
from django.utils.text import slugify


ARTICLES_DIR = Path(__file__).with_name("docs_articles")
LEGACY_TOPICS = {
    "overview": ("getting-started", "introduction"),
    "quickstart": ("getting-started", "quickstart"),
    "cadence": ("ai-automation", "cadence"),
    "playbooks": ("ai-automation", "ai-brain"),
    "workflows": ("ai-automation", "workflows"),
    "integrations": ("connect-hub", "overview"),
    "measurement": ("ai-automation", "insights"),
    "troubleshooting": ("troubleshooting", "first-checks"),
}
_LINK = re.compile(r"\[([^\]\n]{1,160})\]\(((?:https?://|/)[^\s)\n]{1,500})\)")
_CODE = re.compile(r"\x60([^\x60\n]+)\x60")
_BOLD = re.compile(r"\*\*([^*\n]+)\*\*")
_NUMBERED = re.compile(r"^\d{1,3}\.\s+(.+)$")


def _inline(source: str) -> str:
    """Escape source text first; allow only explicit HTTP(S) and local links."""
    parts = []
    last = 0
    for match in _LINK.finditer(source):
        parts.append(html.escape(source[last:match.start()]))
        title = html.escape(match.group(1))
        url = html.escape(match.group(2), quote=True)
        parts.append(f'<a href="{url}">{title}</a>')
        last = match.end()
    parts.append(html.escape(source[last:]))
    output = "".join(parts)
    output = _CODE.sub(lambda m: f"<code>{m.group(1)}</code>", output)
    output = _BOLD.sub(lambda m: f"<strong>{m.group(1)}</strong>", output)
    return output


def render_article(markdown: str):
    """Render a small, deliberately restricted Markdown subset from trusted files.

    The supported shapes are headings, paragraphs, links, bold, inline code,
    ordered/unordered lists, quotes and fenced preformatted blocks. Raw HTML is
    always escaped; arbitrary Markdown extensions are not interpreted.
    """
    blocks = []
    toc = []
    pending = []
    items = []
    list_kind = None
    in_code = False
    code_lines = []
    used_ids = set()

    def flush_paragraph():
        if pending:
            blocks.append("<p>" + _inline(" ".join(pending)) + "</p>")
            pending.clear()

    def flush_list():
        nonlocal list_kind
        if items:
            tag = list_kind or "ul"
            blocks.append(f"<{tag}>" + "".join(f"<li>{_inline(item)}</li>" for item in items) + f"</{tag}>")
            items.clear()
        list_kind = None

    for raw in markdown.strip().splitlines():
        line = raw.strip()
        if line.startswith(chr(96) * 3):
            flush_paragraph()
            flush_list()
            if in_code:
                blocks.append("<pre><code>" + html.escape("\n".join(code_lines)) + "</code></pre>")
                code_lines = []
                in_code = False
            else:
                in_code = True
            continue
        if in_code:
            code_lines.append(raw)
            continue
        if not line:
            flush_paragraph()
            flush_list()
            continue
        if line.startswith("## ") or line.startswith("### "):
            flush_paragraph()
            flush_list()
            level = 3 if line.startswith("### ") else 2
            title = line[level + 1:].strip()
            anchor = slugify(title) or "section"
            root = anchor
            suffix = 2
            while anchor in used_ids:
                anchor = f"{root}-{suffix}"
                suffix += 1
            used_ids.add(anchor)
            toc.append({"id": anchor, "title": title, "level": level})
            blocks.append(f'<h{level} id="{anchor}">{_inline(title)}</h{level}>')
            continue
        if line.startswith("- "):
            flush_paragraph()
            if list_kind != "ul":
                flush_list()
                list_kind = "ul"
            items.append(line[2:].strip())
            continue
        number = _NUMBERED.match(line)
        if number:
            flush_paragraph()
            if list_kind != "ol":
                flush_list()
                list_kind = "ol"
            items.append(number.group(1))
            continue
        if line.startswith("> "):
            flush_paragraph()
            flush_list()
            blocks.append('<aside class="kb-note">' + _inline(line[2:]) + "</aside>")
            continue
        flush_list()
        pending.append(line)

    flush_paragraph()
    flush_list()
    if in_code:
        blocks.append("<pre><code>" + html.escape("\n".join(code_lines)) + "</code></pre>")
    return mark_safe("\n".join(blocks)), toc


def _load():
    groups = []
    lookup = {}
    ordered = []
    for filepath in sorted(ARTICLES_DIR.glob("*.json")):
        record = json.loads(filepath.read_text(encoding="utf-8"))
        group_slug = record["slug"]
        group = {
            "slug": group_slug,
            "title": record["title"],
            "description": record["description"],
            "pages": [],
        }
        if not re.fullmatch(r"[a-z0-9-]+", group_slug):
            raise ValueError(f"Invalid documentation group: {group_slug}")
        for page in record["pages"]:
            slug = page["slug"]
            key = (group_slug, slug)
            if key in lookup or not re.fullmatch(r"[a-z0-9-]+", slug):
                raise ValueError(f"Duplicate/invalid documentation slug: {key}")
            item = {
                "slug": slug,
                "group_slug": group_slug,
                "group_title": group["title"],
                "title": page["title"],
                "summary": page["summary"],
                "body": page["body"],
                "url": f"/docs/{group_slug}/{slug}/",
            }
            lookup[key] = item
            ordered.append(item)
            group["pages"].append(item)
        group["count"] = len(group["pages"])
        groups.append(group)
    # Prefix ordering is explicit; page ordering is declared within each file.
    sequence = [
        "getting-started", "crm-leads", "messaging-channels",
        "ai-automation", "connect-hub", "sales-calendar",
        "operations", "troubleshooting",
    ]
    groups.sort(key=lambda g: sequence.index(g["slug"]) if g["slug"] in sequence else len(sequence))
    ordered = [page for group in groups for page in group["pages"]]
    return groups, lookup, ordered


DOC_GROUPS, DOC_LOOKUP, DOC_ORDER = _load()


def docs_context(*, category=None, slug=None, legacy_topic=None, query=""):
    query = (query or "").strip()[:120]
    requested = bool(category or slug or legacy_topic)
    if not requested and not query:
        return {
            "docs_groups": DOC_GROUPS,
            "docs_page_count": len(DOC_ORDER),
            "docs_home": True,
            "docs_query": "",
            "docs_popular": [
                DOC_LOOKUP[key] for key in [
                    ("getting-started", "quickstart"),
                    ("messaging-channels", "whatsapp-api"),
                    ("ai-automation", "ai-brain"),
                    ("connect-hub", "overview"),
                    ("troubleshooting", "first-checks"),
                    ("operations", "support-tickets"),
                ] if key in DOC_LOOKUP
            ],
        }
    if query and not requested:
        words = query.casefold().split()
        matches = [
            page for page in DOC_ORDER
            if all(word in " ".join(
                (page["title"], page["summary"], page["body"], page["group_title"])
            ).casefold() for word in words)
        ]
        return {
            "docs_groups": DOC_GROUPS,
            "docs_page_count": len(DOC_ORDER),
            "docs_search": True,
            "docs_query": query,
            "docs_results": matches,
        }
    if legacy_topic and not category:
        category, slug = LEGACY_TOPICS.get(legacy_topic, (None, None))
    selected = DOC_LOOKUP.get((category, slug))
    if not selected:
        raise Http404("Documentation article not found")
    rendered, toc = render_article(selected["body"])
    index = DOC_ORDER.index(selected)
    selected_group = next(g for g in DOC_GROUPS if g["slug"] == category)
    return {
        "docs_groups": DOC_GROUPS,
        "docs_page_count": len(DOC_ORDER),
        "docs_query": query,
        "docs_page": selected,
        "docs_html": rendered,
        "docs_toc": toc,
        "docs_group": selected_group,
        "docs_previous": DOC_ORDER[index - 1] if index else None,
        "docs_next": DOC_ORDER[index + 1] if index + 1 < len(DOC_ORDER) else None,
        "docs_related": [p for p in selected_group["pages"] if p is not selected][:4],
    }
