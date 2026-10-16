"""Transactional publishing and bounded image handling for the public website."""
import copy
import io
import logging
import re
import uuid
import warnings
from urllib.parse import urlsplit

from boto3.exceptions import S3UploadFailedError
from botocore.exceptions import BotoCoreError, ClientError
from django.conf import settings
from django.core.exceptions import PermissionDenied, SuspiciousFileOperation, ValidationError
from django.core.files.base import ContentFile
from django.db import IntegrityError, InterfaceError, OperationalError, transaction
from django.http import FileResponse, HttpResponse
from django.utils import timezone
from django.utils.html import strip_tags
from django.utils.text import slugify
from PIL import Image, ImageOps, UnidentifiedImageError

from .auth import check_authorized
from .content import safe_link, sanitize_html, snapshot_image_ids
from .models import BlogImage, Category, Post, Revision, SlugRedirect

logger = logging.getLogger(__name__)
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_IMAGE_PIXELS = 25_000_000
STORAGE_ERRORS = (OSError, BotoCoreError, ClientError, S3UploadFailedError)
TEXT_LIMITS = {
    "title": 220, "slug": 180, "excerpt": 500, "author_name": 120,
    "author_bio": 500, "author_url": 500, "seo_title": 220, "meta_description": 500,
    "focus_keyword": 160, "social_title": 220, "social_description": 500, "canonical_url": 500,
}
RESERVED_SLUGS = {"media", "category", "tag", "search", "feed", "sitemap", "admin", "new"}


class ConflictError(ValidationError):
    def __init__(self):
        super().__init__("This article changed in another tab. Reload it before saving to avoid overwriting newer work.")


class CreationConflictError(ConflictError):
    def __init__(self, post=None):
        ValidationError.__init__(self,
            "This article creation request was already used. Open the saved article before making further changes."
        )
        self.recovery_post = post


class ImageUploadUnavailable(Exception):
    """An expected storage/database outage that the editor can safely retry."""


def _image_error_context(error):
    context = {"error_type": type(error).__name__}
    if isinstance(error, ClientError):
        code = error.response.get("Error", {}).get("Code")
        # Keep provider diagnosis useful without recording arbitrary messages,
        # object keys, request metadata or a malicious/unexpected error value.
        context["storage_code"] = code if code in {
            "404", "NoSuchKey", "NotFound", "AccessDenied", "InvalidAccessKeyId",
            "SignatureDoesNotMatch", "NoSuchBucket", "ExpiredToken", "InvalidToken",
            "RequestTimeout", "SlowDown", "ServiceUnavailable", "InternalError",
        } else "OtherClientError"
    return context


def published_posts():
    return Post.objects.filter(published_snapshot__isnull=False).order_by("-published_at", "-created_at")


def _category(value):
    name = (value.get("name", "") if isinstance(value, dict) else str(value or "")).strip()
    if not name:
        return {"name": "", "slug": ""}
    if len(name) > 80:
        raise ValidationError({"category": "Category names must be at most 80 characters."})
    existing = Category.objects.filter(name__iexact=name).first()
    if existing:
        return {"name": existing.name, "slug": existing.slug}
    base = slugify(name)[:90] or "category"
    candidate = base
    suffix = 1
    while Category.objects.filter(slug=candidate).exists():
        suffix += 1
        candidate = f"{base}-{suffix}"
    # A savepoint allows a concurrent category creator to win without breaking
    # the surrounding editorial transaction.
    try:
        with transaction.atomic():
            category = Category.objects.create(name=name, slug=candidate)
    except IntegrityError:
        category = Category.objects.filter(name__iexact=name).first()
        if category is None:
            raise ValidationError({"category": "A similar category was just created. Save again."}) from None
    return {"name": category.name, "slug": category.slug}


def normalize_snapshot(data):
    snapshot = {}
    for name, limit in TEXT_LIMITS.items():
        value = str(data.get(name) or "").strip()
        if len(value) > limit:
            raise ValidationError({name: f"Use no more than {limit} characters."})
        snapshot[name] = value
    if not snapshot["title"]:
        raise ValidationError({"title": "Add an article title."})
    snapshot["slug"] = snapshot["slug"] or slugify(snapshot["title"])[:180]
    if (not re.fullmatch(r"[a-z0-9]+(?:[-_][a-z0-9]+)*", snapshot["slug"])
            or snapshot["slug"] in RESERVED_SLUGS):
        raise ValidationError({"slug": "Use a unique URL with lowercase letters, numbers and hyphens."})
    snapshot["body_html"] = sanitize_html(data.get("body_html", ""))
    snapshot["category"] = _category(data.get("category"))
    tags = data.get("tags") or []
    if isinstance(tags, str):
        tags = tags.split(",")
    if not isinstance(tags, (list, tuple)) or len(tags) > 20:
        raise ValidationError({"tags": "Use up to 20 comma-separated tags."})
    snapshot["tags"] = list(dict.fromkeys(str(tag).strip() for tag in tags if str(tag).strip()))
    if any(len(tag) > 60 for tag in snapshot["tags"]):
        raise ValidationError({"tags": "Each tag must be no more than 60 characters."})
    for key in ("featured", "noindex"):
        snapshot[key] = data.get(key) in (True, "true", "True", "1", "on", 1)
    for key in ("cover_image", "social_image"):
        value = data.get(key)
        try:
            snapshot[key] = str(uuid.UUID(str(value))) if value else ""
        except (ValueError, TypeError, AttributeError) as exc:
            raise ValidationError({key: "Choose an uploaded blog image."}) from exc
    ids = snapshot_image_ids(snapshot)
    if ids and BlogImage.objects.filter(id__in=ids).count() != len(ids):
        raise ValidationError({"body_html": "An image no longer exists. Upload it again before saving."})
    if snapshot["author_url"]:
        url = safe_link(snapshot["author_url"])
        if not url or not url.startswith(("https://", "http://")):
            raise ValidationError({"author_url": "Use a complete HTTP or HTTPS author profile URL."})
    if snapshot["canonical_url"]:
        origin = urlsplit(getattr(settings, "BLOG_PUBLIC_ORIGIN", "https://shvya-ai.com"))
        try:
            url = urlsplit(snapshot["canonical_url"])
        except ValueError as exc:
            raise ValidationError({"canonical_url": "Enter a valid canonical URL."}) from exc
        if (url.scheme != origin.scheme or url.netloc != origin.netloc
                or url.query or url.fragment or url.username or url.password):
            raise ValidationError({"canonical_url": "Canonical URLs must use the public website origin without query strings or fragments."})
    return snapshot


def _lock(post, expected_version):
    locked = Post.objects.select_for_update().get(pk=post.pk)
    try:
        expected = int(expected_version)
    except (TypeError, ValueError) as exc:
        raise ConflictError() from exc
    if locked.version != expected:
        raise ConflictError()
    return locked


def _record(post, actor, reason, snapshot=None):
    post.updated_by = actor
    post.save()
    Revision.objects.create(post=post, snapshot=copy.deepcopy(snapshot if snapshot is not None else post.draft),
                            version=post.version, reason=reason, created_by=actor)
    return post


def _clear_schedule(post):
    post.scheduled_at = None
    post.scheduled_snapshot = None
    post.scheduled_by = None
    post.scheduling_error = ""


@transaction.atomic
def create_post_once(data, actor, creation_id=None):
    """Reuse the browser's creation key after a lost response, never overwrite it."""
    check_authorized(actor)
    snapshot = normalize_snapshot(data)
    if creation_id:
        try:
            creation_id = uuid.UUID(str(creation_id))
        except (ValueError, TypeError, AttributeError) as exc:
            raise ValidationError("The draft creation key is invalid. Open a new article editor.") from exc
        # get_or_create has an inner savepoint for concurrent UUID insert races;
        # its recovery lookup also retains this queryset's row lock.
        post, created = Post.objects.select_for_update().get_or_create(
            pk=creation_id, defaults={"draft": snapshot, "created_by": actor, "updated_by": actor},
        )
        if not created:
            if post.created_by_id != actor.pk:
                raise CreationConflictError()
            if post.draft != snapshot:
                raise CreationConflictError(post)
            return post, False
    else:
        post = Post.objects.create(draft=snapshot, created_by=actor, updated_by=actor)
    Revision.objects.create(post=post, snapshot=copy.deepcopy(post.draft), version=post.version,
                            reason="created", created_by=actor)
    return post, True


def create_post(data, actor, creation_id=None):
    return create_post_once(data, actor, creation_id)[0]


@transaction.atomic
def save_draft(post, data, actor, expected_version):
    check_authorized(actor)
    post = _lock(post, expected_version)
    snapshot = normalize_snapshot(data)
    if snapshot == post.draft:
        return post
    post.draft = snapshot
    post.version += 1
    if post.status in {Post.Status.SCHEDULED, Post.Status.ARCHIVED}:
        post.status = Post.Status.PUBLISHED if post.published_snapshot else Post.Status.DRAFT
    _clear_schedule(post)
    return _record(post, actor, "draft saved")


def _validate_publication(post, snapshot):
    # Revalidate restored/scheduled content, including all referenced media.
    snapshot = normalize_snapshot(snapshot)
    errors = {}
    if not strip_tags(snapshot["body_html"]).strip():
        errors["body_html"] = "Add article content before publishing."
    if not snapshot["author_name"]:
        errors["author_name"] = "Add the author name before publishing."
    slug = snapshot["slug"]
    if Post.objects.exclude(pk=post.pk).filter(published_slug=slug).exists():
        errors["slug"] = "Another published article uses this URL. Choose a different slug."
    if SlugRedirect.objects.exclude(post=post).filter(old_slug=slug).exists():
        errors["slug"] = "This URL belongs to an earlier article. Choose a different slug."
    if errors:
        raise ValidationError(errors)
    return snapshot


def _publish_locked(post, snapshot, actor):
    snapshot = _validate_publication(post, snapshot)
    now = timezone.now()
    previous_slug = post.published_slug
    if previous_slug and previous_slug != snapshot["slug"]:
        SlugRedirect.objects.get_or_create(old_slug=previous_slug, defaults={"post": post})
    SlugRedirect.objects.filter(old_slug=snapshot["slug"], post=post).delete()
    post.published_at = post.published_at or now
    snapshot["published_at"] = post.published_at.isoformat()
    snapshot["published_modified_at"] = now.isoformat()
    post.published_snapshot = snapshot
    post.published_slug = snapshot["slug"]
    post.live_updated_at = now
    post.status = Post.Status.PUBLISHED
    _clear_schedule(post)
    post.version += 1
    _record(post, actor, "published", snapshot=snapshot)
    post.published_images.set(BlogImage.objects.filter(id__in=snapshot_image_ids(snapshot)))
    return post


@transaction.atomic
def publish_post(post, actor, expected_version):
    check_authorized(actor, publish=True)
    post = _lock(post, expected_version)
    try:
        with transaction.atomic():
            return _publish_locked(post, post.draft, actor)
    except IntegrityError as exc:
        raise ValidationError({"slug": "That URL was just reserved by another publication. Choose a different slug."}) from exc


@transaction.atomic
def schedule_post(post, actor, expected_version, scheduled_at):
    check_authorized(actor, publish=True)
    post = _lock(post, expected_version)
    if not scheduled_at or timezone.is_naive(scheduled_at) or scheduled_at <= timezone.now():
        raise ValidationError({"schedule_at": "Choose a future date and time in IST."})
    post.scheduled_snapshot = _validate_publication(post, post.draft)
    post.scheduled_at = scheduled_at
    post.scheduled_by = actor
    post.scheduling_error = ""
    post.status = Post.Status.SCHEDULED
    post.version += 1
    return _record(post, actor, "scheduled")


@transaction.atomic
def change_status(post, status, actor, expected_version):
    if status not in {Post.Status.DRAFT, Post.Status.REVIEW, Post.Status.ARCHIVED}:
        raise ValidationError("Unsupported article status.")
    check_authorized(actor, publish=status in {Post.Status.DRAFT, Post.Status.ARCHIVED})
    post = _lock(post, expected_version)
    if status in {Post.Status.DRAFT, Post.Status.ARCHIVED}:
        if post.published_slug:
            SlugRedirect.objects.get_or_create(old_slug=post.published_slug, defaults={"post": post})
        post.published_snapshot = None
        post.published_slug = None
        post.live_updated_at = None
        post.published_images.clear()
    post.status = status
    _clear_schedule(post)
    post.version += 1
    return _record(post, actor, status)


@transaction.atomic
def restore_revision(post, revision, actor, expected_version):
    check_authorized(actor)
    post = _lock(post, expected_version)
    if revision.post_id != post.pk:
        raise ValidationError("This revision does not belong to this article.")
    post.draft = normalize_snapshot(revision.snapshot)
    post.status = Post.Status.PUBLISHED if post.published_snapshot else Post.Status.DRAFT
    _clear_schedule(post)
    post.version += 1
    return _record(post, actor, "revision restored")


def publish_due_posts(limit=100):
    ids = list(Post.objects.filter(status=Post.Status.SCHEDULED, scheduled_at__lte=timezone.now())
               .order_by("scheduled_at").values_list("id", flat=True)[:min(max(limit, 1), 500)])
    published = 0
    for post_id in ids:
        with transaction.atomic():
            post = Post.objects.select_for_update(of=("self",)).select_related("scheduled_by").get(id=post_id)
            if post.status != Post.Status.SCHEDULED or not post.scheduled_at or post.scheduled_at > timezone.now():
                continue
            actor = post.scheduled_by
            try:
                with transaction.atomic():
                    check_authorized(actor, publish=True)
                    if not post.scheduled_snapshot:
                        raise ValidationError("Scheduled content is missing. Review and schedule this article again.")
                    _publish_locked(post, post.scheduled_snapshot, actor)
            except (ValidationError, PermissionDenied, IntegrityError) as exc:
                # The nested savepoint rolls back any partially applied publication.
                post.refresh_from_db()
                post.status = Post.Status.REVIEW
                post.scheduled_at = None
                post.scheduled_snapshot = None
                post.scheduled_by = None
                post.scheduling_error = ("Publishing permission was revoked. A publisher must schedule this article again."
                                         if isinstance(exc, PermissionDenied) else
                                         "Scheduled publishing needs attention. Review the URL, content and images, then schedule again.")
                post.version += 1
                _record(post, actor, "schedule blocked")
                logger.warning("Blog scheduled publication blocked", extra={"post_id": str(post.pk), "error_type": type(exc).__name__})
            else:
                published += 1
    return published


def upload_image(file, actor, alt_text="", caption=""):
    check_authorized(actor)
    if len(alt_text) > 300 or len(caption) > 500:
        raise ValidationError("Use at most 300 characters for alt text and 500 for the caption.")
    if getattr(file, "size", 0) > MAX_UPLOAD_BYTES:
        raise ValidationError("Images must be 10 MB or smaller.")
    try:
        file.seek(0)
        data = file.read(MAX_UPLOAD_BYTES + 1)
    except OSError as exc:
        raise ValidationError("The uploaded file could not be read. Choose the image again and retry.") from exc
    if len(data) > MAX_UPLOAD_BYTES:
        raise ValidationError("Images must be 10 MB or smaller.")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            # verify() catches damaged PNG chunks that load() alone can accept.
            with Image.open(io.BytesIO(data), formats=["JPEG", "PNG", "WEBP"]) as source:
                if source.width * source.height > MAX_IMAGE_PIXELS:
                    raise ValidationError("Images must be no larger than 25 megapixels.")
                if getattr(source, "n_frames", 1) != 1:
                    raise ValidationError("Animated images are not supported. Upload a still image.")
                source.verify()
            with Image.open(io.BytesIO(data), formats=["JPEG", "PNG", "WEBP"]) as source:
                source.load()
                processed = ImageOps.exif_transpose(source)
                mode = "RGBA" if "A" in processed.getbands() or "transparency" in processed.info else "RGB"
                processed = processed.convert(mode)
                processed.thumbnail((2000, 2000), Image.Resampling.LANCZOS)
                large = io.BytesIO()
                processed.save(large, "WEBP", quality=85, method=4)
                thumbnail = processed.copy()
                thumbnail.thumbnail((640, 640), Image.Resampling.LANCZOS)
                small = io.BytesIO()
                thumbnail.save(small, "WEBP", quality=82, method=4)
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError,
            Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ValidationError("This file is damaged or unsupported. Upload a valid JPEG, PNG or WebP image.") from exc
    image = BlogImage(width=processed.width, height=processed.height,
                      thumbnail_width=thumbnail.width, thumbnail_height=thumbnail.height,
                      alt_text=alt_text.strip(), caption=caption.strip(), created_by=actor)
    try:
        image.file.save(f"{image.id}.webp", ContentFile(large.getvalue()), save=False)
        image.thumbnail_file.save(f"{image.id}-640.webp", ContentFile(small.getvalue()), save=False)
        image.save()
    except Exception as exc:
        # Cleanup must not replace the original failure, or prevent a second
        # already-saved object from being removed. Never log provider messages:
        # they can contain bucket paths, endpoints or credential material.
        for stored in (image.file, image.thumbnail_file):
            if stored.name:
                try:
                    stored.delete(save=False)
                except Exception as cleanup_error:
                    logger.error("Blog image upload cleanup failed", extra={
                        "image_id": str(image.pk), "variant": stored.field.name,
                        **_image_error_context(cleanup_error),
                    })
        if isinstance(exc, (*STORAGE_ERRORS, OperationalError, InterfaceError)):
            logger.error("Blog image upload unavailable", extra={
                "image_id": str(image.pk), **_image_error_context(exc),
            })
            raise ImageUploadUnavailable(
                "The image could not be saved because storage is temporarily unavailable. "
                "Your article is unchanged. Please retry the upload."
            ) from exc
        raise
    return image


def media_response(image, *, private=False, width=None):
    file = image.thumbnail_file if str(width) == "640" and image.thumbnail_file else image.file
    try:
        if not file.name:
            raise FileNotFoundError("Image file is missing.")
        response = FileResponse(file.open("rb"), content_type="image/webp", as_attachment=False)
    except (*STORAGE_ERRORS, SuspiciousFileOperation) as exc:
        missing = isinstance(exc, (FileNotFoundError, SuspiciousFileOperation))
        if isinstance(exc, ClientError):
            missing = exc.response.get("Error", {}).get("Code") in {"404", "NoSuchKey", "NotFound"}
        logger.warning("Blog image unavailable", extra={
            "image_id": str(image.pk), "variant": file.field.name,
            **_image_error_context(exc), "missing_or_invalid": missing,
        })
        if missing and file.field.name == "thumbnail_file":
            # A lost derived variant should not break an otherwise healthy image.
            # Authorization has already been checked before either object is read.
            return media_response(image, private=private)
        response = HttpResponse(
            "Image not available." if missing else "Image storage is temporarily unavailable. Please retry shortly.",
            status=404 if missing else 503, content_type="text/plain; charset=utf-8",
        )
        if not missing:
            response["Retry-After"] = "60"
    # Revalidation matters when an article is unpublished; private bytes are never cached.
    response["Cache-Control"] = "private, no-store" if private or response.status_code != 200 else "public, max-age=0, must-revalidate"
    response["X-Content-Type-Options"] = "nosniff"
    response["Content-Security-Policy"] = "default-src 'none'; sandbox"
    if private:
        response["X-Robots-Tag"] = "noindex, nofollow, noarchive"
    return response
