"""First-party article views, deduplicated per browser/article/IST day."""
import re
import uuid
from datetime import timedelta
from zoneinfo import ZoneInfo

from django.core import signing
from django.db import transaction
from django.db.models import F
from django.http import JsonResponse
from django.middleware.csrf import get_token
from django.utils import timezone
from django.utils.crypto import salted_hmac
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_POST

from apps.core.ratelimit import ratelimit

from .models import ArticleView, Post
from .services import published_posts

COOKIE = "shvya_blog_visitor"
COOKIE_SALT = "blog.visitor.v1"
PAGE_SALT = "blog.landing.v1"
MAX_AGE = 86400
BOTS = re.compile(r"bot|crawler|spider|slurp|preview|headless|lighthouse|facebookexternalhit|curl|wget|python-requests", re.I)


def eligible(request):
    agent = request.headers.get("User-Agent", "")[:1000]
    return bool(agent and not BOTS.search(agent)
                and request.headers.get("DNT") != "1"
                and request.headers.get("Sec-GPC") != "1")


def visitor_id(request):
    value = request.get_signed_cookie(COOKIE, default="", salt=COOKIE_SALT, max_age=MAX_AGE)
    try:
        return str(uuid.UUID(value))
    except (ValueError, TypeError, AttributeError):
        return ""


def prepare_tracking(request, post, context):
    """Return a new visitor ID only when a cookie needs issuing on this landing."""
    if request.method != "GET" or not eligible(request):
        return ""
    visitor = visitor_id(request)
    new_visitor = "" if visitor else str(uuid.uuid4())
    visitor = visitor or new_visitor
    context["view_token"] = signing.dumps({"post": str(post.pk), "visitor": visitor}, salt=PAGE_SALT)
    context["view_csrf"] = get_token(request)
    return new_visitor


def tracking_day():
    return timezone.now().astimezone(ZoneInfo("Asia/Kolkata")).date()


def cookie_lifetime():
    now = timezone.now().astimezone(ZoneInfo("Asia/Kolkata"))
    midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(1, int((midnight - now).total_seconds()))


@never_cache
@require_POST
@csrf_protect
@ratelimit(limit=120, window=60, methods=("POST",))
def record_view(request, post_id):
    # Only currently live articles have counters. Never expose draft existence.
    post = published_posts().filter(pk=post_id).only("pk", "view_count").first()
    if post is None:
        return JsonResponse({"detail": "Article not found."}, status=404)
    if not eligible(request):
        return JsonResponse({"views": post.view_count, "counted": False})
    visitor = visitor_id(request)
    try:
        token = signing.loads(request.POST.get("token", ""), salt=PAGE_SALT, max_age=3600)
    except signing.BadSignature:
        return JsonResponse({"detail": "Refresh the article to record a view."}, status=400)
    if not visitor or token != {"post": str(post.pk), "visitor": visitor}:
        return JsonResponse({"detail": "A valid article visit is required."}, status=400)
    day = tracking_day()
    digest = salted_hmac("blog.daily-visitor", f"{post.pk}:{day}:{visitor}", algorithm="sha256").hexdigest()
    with transaction.atomic():
        # Serialize against publication/unpublication as well as concurrent visits.
        current = published_posts().select_for_update().filter(pk=post.pk).only("pk", "view_count").first()
        if current is None:
            return JsonResponse({"detail": "Article not found."}, status=404)
        _, created = ArticleView.objects.get_or_create(post=current, day=day, visitor_digest=digest)
        if created:
            Post.objects.filter(pk=current.pk).update(view_count=F("view_count") + 1)
            current.view_count += 1
    return JsonResponse({"views": current.view_count, "counted": created})


def prune_view_receipts():
    # Keep a small retry/debug buffer; no IP, UA, URL parameters or raw visitor IDs.
    return ArticleView.objects.filter(day__lt=tracking_day() - timedelta(days=7)).delete()[0]
