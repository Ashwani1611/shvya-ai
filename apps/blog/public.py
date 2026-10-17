"""Public blog rendering routes. Draft JSON is never a public read source."""

from urllib.parse import urlencode
from uuid import UUID
from xml.etree.ElementTree import Element, SubElement, tostring

from django.core.paginator import EmptyPage, PageNotAnInteger, Paginator
from django.db.models import Q
from django.http import Http404, HttpResponse, HttpResponsePermanentRedirect
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils.cache import patch_vary_headers
from django.views.decorators.http import require_safe

from . import tracking
from .models import BlogImage, SlugRedirect
from .seo import (
    absolute_url, article_body, article_seo, article_url, canonical_url,
    image_data, reading_minutes,
)
from .services import media_response, published_posts, snapshot_image_ids
from apps.core.docs_portal import DOC_ORDER


PAGE_SIZE = 9
SITEMAP_PAGE_SIZE = 45000
CORE_SITEMAP_NAMES = (
    "home", "features", "pricing", "services", "product_suite", "docs",
    "book_call", "cookie_policy", "refund_policy",
)


def _image_lookup(snapshots):
    ids = set()
    for snapshot in snapshots:
        ids.update(snapshot_image_ids(snapshot))
    return {str(image.id): image for image in BlogImage.objects.filter(id__in=ids)}


def _card(post, images):
    snapshot = post.published_snapshot
    return {
        **snapshot,
        "url": reverse("blog:detail", kwargs={"slug": post.published_slug}),
        "cover": image_data(images.get(str(snapshot.get("cover_image", "")))),
        "published_at": post.published_at,
        "reading_minutes": reading_minutes(snapshot.get("body_html", "")),
    }


def _cards(posts):
    posts = list(posts)
    images = _image_lookup(post.published_snapshot for post in posts)
    return [_card(post, images) for post in posts]


def build_article_context(post, snapshot, preview=False, request=None):
    """Shared public/private renderer; previews explicitly opt into draft JSON."""
    images = _image_lookup([snapshot])
    cover = image_data(images.get(str(snapshot.get("cover_image", ""))), preview)
    social = image_data(images.get(str(snapshot.get("social_image", ""))), preview) or cover
    body_html, toc = article_body(snapshot, images, preview)
    category = snapshot.get("category") or {}
    context = {
        "article": snapshot,
        "article_body": body_html,
        "cover": cover,
        "toc": toc,
        "reading_minutes": reading_minutes(snapshot.get("body_html", "")),
        "view_count": post.view_count,
        "view_url": reverse("blog:record_view", kwargs={"post_id": post.pk}),
        "published_at": post.published_at,
        "modified_at": post.live_updated_at,
        "preview": preview,
        "seo": article_seo(post, snapshot, cover, social, preview=preview),
        "share_url": article_url(snapshot["slug"]),
        "demo_url": f"{reverse('book_call')}?{urlencode({'interest': 'Blog: ' + snapshot.get('title', '')[:130]})}",
        "related": [],
    }
    if not preview:
        related = published_posts().exclude(pk=post.pk)
        if category.get("slug"):
            related = related.filter(published_snapshot__category__slug=category["slug"])
        context["related"] = _cards(related.order_by("-published_at", "-pk")[:3])
    return context


def _page_response(request, template, context):
    response = render(request, template, context)
    # Revalidate before reuse so unpublishing and publication changes take effect.
    response["Cache-Control"] = "public, max-age=0, must-revalidate"
    if getattr(request, "crm_user", None) or getattr(getattr(request, "user", None), "is_authenticated", False):
        response["Cache-Control"] = "private, no-store"
    patch_vary_headers(response, ["Cookie"])
    if context["seo"]["robots"].startswith("noindex"):
        response["X-Robots-Tag"] = context["seo"]["robots"]
    return response


def _listing(request, category_slug=""):
    all_posts = published_posts()
    posts = all_posts
    category_name = ""
    if category_slug:
        posts = posts.filter(published_snapshot__category__slug=category_slug)
        category_snapshot = posts.values_list("published_snapshot__category", flat=True).first()
        if not category_snapshot:
            raise Http404("Category not found")
        category_name = category_snapshot["name"]
    query = request.GET.get("q", "").strip()[:150]
    if query:
        posts = posts.filter(
            Q(published_snapshot__title__icontains=query)
            | Q(published_snapshot__excerpt__icontains=query)
            | Q(published_snapshot__body_html__icontains=query)
        )
    featured_post = None
    if not category_slug and not query:
        featured_post = posts.filter(published_snapshot__featured=True).order_by("-published_at", "-pk").first()
        if featured_post:
            posts = posts.exclude(pk=featured_post.pk)
    paginator = Paginator(posts.order_by("-published_at", "-pk"), PAGE_SIZE)
    try:
        page = paginator.page(request.GET.get("page", "1"))
    except (EmptyPage, PageNotAnInteger):
        raise Http404("Page not found") from None
    base_path = reverse("blog:category", kwargs={"slug": category_slug}) if category_slug else reverse("blog:index")
    canonical = absolute_url(base_path)
    if page.number > 1:
        canonical += f"?page={page.number}"
    has_filters = any(key != "page" for key in request.GET)
    title = f"{category_name} | SHVYA AI Blog" if category_name else "SHVYA AI Blog | Sales, WhatsApp & AI Automation"
    if page.number > 1:
        title += f" | Page {page.number}"
    if query:
        title = f"Search: {query} | SHVYA AI Blog"
    categories = {}
    for category in all_posts.order_by().values_list("published_snapshot__category", flat=True).distinct():
        if category and category.get("slug"):
            categories[category["slug"]] = category["name"]
    query_params = {"q": query} if "q" in request.GET else {}

    def page_link(number):
        return f"{base_path}?{urlencode({**query_params, 'page': number})}"

    context = {
        "posts": _cards(page.object_list),
        "featured": _cards([featured_post])[0] if featured_post and page.number == 1 else None,
        "page_obj": page,
        "pagination": [
            {"number": number, "url": page_link(number) if number != paginator.ELLIPSIS else "",
             "current": number == page.number, "ellipsis": number == paginator.ELLIPSIS}
            for number in paginator.get_elided_page_range(page.number, on_each_side=1, on_ends=1)
        ],
        "previous_url": page_link(page.previous_page_number()) if page.has_previous() else "",
        "next_url": page_link(page.next_page_number()) if page.has_next() else "",
        "categories": [{"slug": slug, "name": name} for slug, name in sorted(categories.items(), key=lambda item: item[1])],
        "category_slug": category_slug,
        "category_name": category_name,
        "query": query,
        "search_action": base_path,
        "seo": {
            "title": title,
            "description": f"Practical {category_name.lower()} guides from SHVYA AI." if category_name else (
                "Practical guides to WhatsApp automation, CRM, AI qualification and better sales follow-up from SHVYA AI."
            ),
            "canonical": canonical,
            "robots": "noindex, follow" if has_filters else "index, follow",
            "type": "website",
        },
    }
    return _page_response(request, "blog/public/index.html", context)


@require_safe
def index(request):
    return _listing(request)


@require_safe
def category(request, slug):
    return _listing(request, category_slug=slug)


@require_safe
def detail(request, slug):
    post = published_posts().filter(published_slug=slug).first()
    if post is None:
        previous = SlugRedirect.objects.select_related("post").filter(
            old_slug=slug, post__published_snapshot__isnull=False,
        ).first()
        if previous and previous.post.published_slug:
            return HttpResponsePermanentRedirect(reverse("blog:detail", kwargs={"slug": previous.post.published_slug}))
        raise Http404("Article not found")
    context = build_article_context(post, post.published_snapshot, request=request)
    if request.GET:
        context["seo"]["robots"] = "noindex, follow"
    visitor = tracking.prepare_tracking(request, post, context)
    response = _page_response(request, "blog/public/article.html", context)
    if context.get("view_token"):
        # The signed landing token is browser-specific and must never be shared.
        response["Cache-Control"] = "private, no-store"
    if visitor:
        response.set_signed_cookie(
            tracking.COOKIE, visitor, salt=tracking.COOKIE_SALT,
            max_age=tracking.cookie_lifetime(), path="/blog/", secure=request.is_secure(),
            httponly=True, samesite="Lax",
        )
    return response


@require_safe
def media(request, image_id):
    # A UUID is not a publication permission. Only references in live snapshots
    # make an image public; draft-only assets always remain behind staff auth.
    image = get_object_or_404(
        BlogImage.objects.filter(live_posts__published_snapshot__isnull=False).distinct(),
        pk=UUID(str(image_id)),
    )
    return media_response(image, width=request.GET.get("w"))


def _xml_response(root):
    response = HttpResponse(
        tostring(root, encoding="utf-8", xml_declaration=True), content_type="application/xml; charset=utf-8",
    )
    response["Cache-Control"] = "public, max-age=0, must-revalidate"
    return response


@require_safe
def sitemap(request):
    posts = published_posts().exclude(published_snapshot__noindex=True).order_by("pk")
    paginator = Paginator(posts, SITEMAP_PAGE_SIZE)
    namespace = {"xmlns": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    if "page" not in request.GET and paginator.num_pages > 1:
        root = Element("sitemapindex", namespace)
        for page in paginator.page_range:
            SubElement(SubElement(root, "sitemap"), "loc").text = absolute_url(f"/sitemap.xml?page={page}")
        return _xml_response(root)
    try:
        page = paginator.page(request.GET.get("page", "1"))
    except (EmptyPage, PageNotAnInteger):
        raise Http404("Sitemap page not found") from None
    root = Element("urlset", namespace)
    if page.number == 1:
        for name in (*CORE_SITEMAP_NAMES, "blog:index"):
            SubElement(SubElement(root, "url"), "loc").text = absolute_url(reverse(name))
        # Use the same curated public catalogue as the documentation renderer.
        # Never enumerate operational routes or internal engineering documents.
        for article in DOC_ORDER:
            SubElement(SubElement(root, "url"), "loc").text = absolute_url(article["url"])
        categories = posts.order_by().values_list("published_snapshot__category", flat=True).distinct()
        for slug in sorted({item["slug"] for item in categories if item and item.get("slug")}):
            path = reverse("blog:category", kwargs={"slug": slug})
            SubElement(SubElement(root, "url"), "loc").text = absolute_url(path)
    for post in page.object_list.iterator(chunk_size=500):
        snapshot = post.published_snapshot
        location = article_url(post.published_slug)
        if canonical_url(snapshot) != location:
            continue
        node = SubElement(root, "url")
        SubElement(node, "loc").text = location
        if post.live_updated_at:
            SubElement(node, "lastmod").text = post.live_updated_at.isoformat()
    return _xml_response(root)


@require_safe
def robots(request):
    response = HttpResponse(
        "User-agent: *\nAllow: /\nDisallow: /superadmin/\nDisallow: /admin/\n"
        "Disallow: /dashboard/\nDisallow: /api/\nDisallow: /vault/\n"
        "Disallow: /contact-us\nDisallow: /privacy-policy\nDisallow: /terms-conditions\n"
        "Disallow: /faq\nDisallow: /*?search\nDisallow: /*&search\n"
        f"Sitemap: {absolute_url('/sitemap.xml')}\n",
        content_type="text/plain; charset=utf-8",
    )
    response["Cache-Control"] = "public, max-age=0, must-revalidate"
    return response
