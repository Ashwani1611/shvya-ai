"""Presentation and structured data for the published marketing blog."""

import json
import math
from urllib.parse import urlsplit

from bs4 import BeautifulSoup
from django.conf import settings
from django.urls import reverse
from django.utils.html import strip_tags
from django.utils.text import slugify


def public_origin():
    """Never derive canonical URLs from a request's untrusted Host header."""
    return settings.BLOG_PUBLIC_ORIGIN.rstrip("/")


def absolute_url(path):
    return f"{public_origin()}/{path.lstrip('/')}"


def article_url(slug):
    return absolute_url(reverse("blog:detail", kwargs={"slug": slug}))


def canonical_url(snapshot):
    own_url = article_url(snapshot["slug"])
    proposed = snapshot.get("canonical_url", "")
    try:
        parsed = urlsplit(proposed)
    except ValueError:
        return own_url
    # Validation also happens on save; keep this boundary defensive against
    # historic or manually imported values.
    if proposed and f"{parsed.scheme}://{parsed.netloc}" == public_origin():
        return proposed
    return own_url


def reading_minutes(body_html):
    return max(1, math.ceil(len(strip_tags(body_html).split()) / 220))


def image_data(image, preview=False):
    if image is None:
        return None
    url = image.preview_url if preview else image.public_url
    return {
        "url": url,
        "thumbnail_url": f"{url}?w=640" if image.thumbnail_file else url,
        "width": image.width,
        "height": image.height,
        "alt": image.alt_text,
        "caption": image.caption,
    }


def article_body(snapshot, images, preview=False):
    """Enrich already-sanitized saved content with anchors and image sizing."""
    soup = BeautifulSoup(snapshot.get("body_html", ""), "html.parser")
    toc = []
    used_ids = set()
    for heading in soup.find_all(["h2", "h3"]):
        label = heading.get_text(" ", strip=True)
        base = slugify(label)[:100] or "section"
        anchor = f"article-{base}"
        count = 2
        while anchor in used_ids:
            anchor = f"article-{base}-{count}"
            count += 1
        used_ids.add(anchor)
        heading["id"] = anchor
        toc.append({"id": anchor, "label": label, "level": heading.name})
    for node in soup.find_all("img"):
        # Only references validated by the publishing service are renderable.
        path = urlsplit(node.get("src", "")).path
        image_id = path.rstrip("/").rsplit("/", 1)[-1]
        image = images.get(image_id)
        if image is None:
            node.decompose()
            continue
        data = image_data(image, preview)
        node["src"] = data["url"]
        node["width"] = image.width
        node["height"] = image.height
        node["loading"] = "lazy"
        node["decoding"] = "async"
        if not node.get("alt"):
            node["alt"] = image.alt_text
        if image.thumbnail_file and image.thumbnail_width < image.width:
            node["srcset"] = (
                f"{data['thumbnail_url']} {image.thumbnail_width}w, "
                f"{data['url']} {image.width}w"
            )
            node["sizes"] = "(max-width: 800px) 92vw, 760px"
    return str(soup), toc


def json_ld(data):
    """JSON in an executable HTML context must not be able to close its tag."""
    return (
        json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        .replace("&", "\\u0026")
        .replace("<", "\\u003C")
        .replace(">", "\\u003E")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def listing_schema(title, description, canonical, category_name=""):
    """Describe the public collection without inventing ratings or products."""
    breadcrumbs = [("Home", absolute_url("/")), ("Blog", absolute_url(reverse("blog:index")))]
    if category_name:
        breadcrumbs.append((category_name, canonical.split("?", 1)[0]))
    return json_ld({
        "@context": "https://schema.org",
        "@graph": [
            {"@type": "CollectionPage", "@id": canonical + "#webpage", "url": canonical,
             "name": title, "description": description, "inLanguage": "en"},
            {"@type": "BreadcrumbList", "itemListElement": [
                {"@type": "ListItem", "position": position, "name": name, "item": url}
                for position, (name, url) in enumerate(breadcrumbs, 1)
            ]},
        ],
    })


def article_seo(post, snapshot, cover, social_image, *, preview=False):
    canonical = canonical_url(snapshot)
    title = snapshot.get("seo_title") or f"{snapshot['title']} | SHVYA AI"
    description = snapshot.get("meta_description") or snapshot.get("excerpt", "")
    seo = {
        "title": title,
        "description": description,
        "canonical": canonical,
        "robots": "noindex, nofollow" if preview else (
            "noindex, follow" if snapshot.get("noindex") else "index, follow, max-image-preview:large"
        ),
        "type": "article",
        "social_title": snapshot.get("social_title") or title,
        "social_description": snapshot.get("social_description") or description,
        "image": absolute_url(social_image["url"]) if social_image else "",
        "image_alt": social_image["alt"] if social_image else "",
        "schema": "",
    }
    if preview:
        return seo
    author_name = snapshot.get("author_name") or "SHVYA AI"
    author = {
        "@type": "Organization" if author_name.lower() in {"shvya ai", "shvya editorial team"} else "Person",
        "name": author_name,
    }
    if snapshot.get("author_url"):
        author["url"] = snapshot["author_url"]
    article = {
        "@type": "BlogPosting",
        "@id": f"{canonical}#article",
        "mainEntityOfPage": {"@type": "WebPage", "@id": canonical},
        "headline": snapshot["title"],
        "description": description,
        "author": author,
        "publisher": {"@type": "Organization", "name": "SHVYA AI", "url": public_origin()},
        "inLanguage": "en",
    }
    if post.published_at:
        article["datePublished"] = post.published_at.isoformat()
    if post.live_updated_at:
        article["dateModified"] = post.live_updated_at.isoformat()
    if cover:
        article["image"] = [absolute_url(cover["url"])]
    breadcrumbs = [
        {"@type": "ListItem", "position": 1, "name": "Home", "item": absolute_url(reverse("home"))},
        {"@type": "ListItem", "position": 2, "name": "Blog", "item": absolute_url(reverse("blog:index"))},
        {"@type": "ListItem", "position": 3, "name": snapshot["title"], "item": canonical},
    ]
    seo["schema"] = json_ld({
        "@context": "https://schema.org",
        "@graph": [article, {"@type": "BreadcrumbList", "itemListElement": breadcrumbs}],
    })
    return seo
