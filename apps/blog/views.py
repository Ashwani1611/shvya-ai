"""Platform-only HTTP boundary for the website editorial workflow."""
import json
from functools import wraps

from django.contrib import messages
from django.contrib.auth.forms import AuthenticationForm
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Count, Q
from django.http import Http404, JsonResponse
from django.middleware.csrf import rotate_token
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from apps.accounts.session_utils import get_session_store, save_session_cookie, set_authenticated_user
from apps.core.ratelimit import ratelimit

from . import services
from .auth import can_edit, can_publish, editorial_required
from .forms import PostForm
from .models import BlogImage, Category, Post, Revision


def private_page(view):
    @wraps(view)
    @never_cache
    def wrapped(request, *args, **kwargs):
        response = view(request, *args, **kwargs)
        response["X-Robots-Tag"] = "noindex, nofollow, noarchive"
        response["Cache-Control"] = "private, no-store"
        return response
    return wrapped


@private_page
@ratelimit(limit=15, window=300, methods=("POST",))
@ratelimit(key_func=lambda request: request.POST.get("username", ""), limit=5, window=300, methods=("POST",))
@require_http_methods(["GET", "POST"])
def login(request):
    target = request.POST.get("next") or request.GET.get("next") or reverse("blog_admin:index")
    if not (url_has_allowed_host_and_scheme(target, allowed_hosts={request.get_host()}, require_https=request.is_secure())
            and target.startswith("/superadmin/blog/")
            and not target.startswith(reverse("blog_admin:login"))):
        target = reverse("blog_admin:index")
    if can_edit(request.user):
        return redirect(target)
    form = AuthenticationForm(request, data=request.POST if request.method == "POST" else None)
    if request.method == "POST" and form.is_valid():
        user = form.get_user()
        if not can_edit(user):
            form.add_error(None, "This login requires a platform blog editor account.")
        else:
            session = get_session_store(request, "superadmin")
            session.cycle_key()
            set_authenticated_user(session, user)
            rotate_token(request)
            response = redirect(target)
            save_session_cookie(request, response, session, "superadmin")
            return response
    return render(request, "blog/admin/login.html", {"form": form, "next": target})


def _json_request(request):
    return (request.headers.get("X-Requested-With") == "XMLHttpRequest"
            or "application/json" in request.headers.get("Accept", "")
            or request.content_type == "application/json")


def _data(request):
    if request.content_type == "application/json":
        try:
            data = json.loads(request.body)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValidationError("Invalid editor request.") from exc
        if not isinstance(data, dict):
            raise ValidationError("Invalid editor request.")
        return data
    return request.POST


def _post_response(post):
    return {
        "ok": True, "version": post.version, "post_id": str(post.id), "status": post.status,
        "edit_url": reverse("blog_admin:edit", kwargs={"post_id": post.id}),
        "autosave_url": reverse("blog_admin:autosave", kwargs={"post_id": post.id}),
        "preview_url": reverse("blog_admin:preview", kwargs={"post_id": post.id}),
        "public_url": post.get_absolute_url() if post.published_snapshot else "",
    }


def _image_data(image):
    return {"id": str(image.id), "url": image.public_url, "public_url": image.public_url,
            "preview_url": image.preview_url, "alt_text": image.alt_text, "caption": image.caption,
            "width": image.width, "height": image.height}


def _editor(request, post=None, form=None, status=200):
    images = list(BlogImage.objects.all()[:100])
    # An older attached cover remains selectable even after the recent-media page rolls over.
    referenced = services.snapshot_image_ids(post.draft) if post else set()
    missing = referenced - {str(image.pk) for image in images}
    if missing:
        images.extend(BlogImage.objects.filter(pk__in=missing))
    return render(request, "blog/admin/editor.html", {
        "form": form if form is not None else PostForm(post=post), "post": post,
        "draft_data": post.draft if post else {}, "images": images,
        "images_data": [_image_data(image) for image in images], "categories": Category.objects.all(),
        "revisions": post.revisions.select_related("created_by")[:30] if post else [],
        "can_publish": can_publish(request.user),
    }, status=status)


def _add_validation_errors(form, error):
    if hasattr(error, "message_dict"):
        for field, errors in error.message_dict.items():
            form.add_error(field if field in form.fields else None, errors)
    else:
        form.add_error(None, error.messages)


@private_page
@editorial_required
@require_GET
def index(request):
    status = request.GET.get("status", "")
    q = request.GET.get("q", "").strip()[:150]
    posts = Post.objects.select_related("updated_by").defer("scheduled_snapshot")
    if status in Post.Status.values:
        posts = posts.filter(status=status)
    if q:
        posts = posts.filter(Q(draft__title__icontains=q) | Q(draft__slug__icontains=q))
    page_obj = Paginator(posts, 20).get_page(request.GET.get("page"))
    counts = {row["status"]: row["total"] for row in Post.objects.values("status").annotate(total=Count("id"))}
    return render(request, "blog/admin/index.html", {
        "page_obj": page_obj, "posts": page_obj, "status": status, "q": q,
        "status_choices": Post.Status.choices, "counts": counts,
        "total_posts": sum(counts.values()), "live_count": services.published_posts().count(),
        "can_publish": can_publish(request.user),
    })


def _save(request, post=None, *, autosave=False):
    try:
        data = _data(request)
    except ValidationError as exc:
        return JsonResponse({"ok": False, "detail": " ".join(exc.messages)}, status=400)
    form = PostForm(data=data, post=post)
    action = "save" if autosave else data.get("action", "save")
    if action not in {"save", "review", "publish", "schedule", "unpublish", "archive"}:
        form.is_valid()
        form.add_error(None, "Unsupported editor action.")
    if form.is_valid():
        try:
            # One atomic boundary means an invalid publish/schedule cannot silently
            # save a different draft while returning an old optimistic-lock version.
            with transaction.atomic():
                saved = (services.save_draft(post, form.cleaned_data, request.user, form.cleaned_data["version"])
                         if post else services.create_post(form.cleaned_data, request.user))
                if action == "publish":
                    saved = services.publish_post(saved, request.user, saved.version)
                elif action == "schedule":
                    saved = services.schedule_post(saved, request.user, saved.version, form.cleaned_data["schedule_at"])
                elif action in {"review", "unpublish", "archive"}:
                    target = {"review": Post.Status.REVIEW, "unpublish": Post.Status.DRAFT,
                              "archive": Post.Status.ARCHIVED}[action]
                    saved = services.change_status(saved, target, request.user, saved.version)
        except ValidationError as exc:
            _add_validation_errors(form, exc)
            code = 409 if isinstance(exc, services.ConflictError) else 400
        else:
            if autosave or _json_request(request):
                return JsonResponse(_post_response(saved), status=201 if post is None else 200)
            messages.success(request, {"save": "Draft saved.", "review": "Article sent for review.",
                                       "publish": "Article is live.", "schedule": "Article scheduled (IST).",
                                       "unpublish": "Article unpublished.", "archive": "Article archived."}[action])
            return redirect("blog_admin:edit", post_id=saved.id)
    else:
        code = 400
    if autosave or _json_request(request):
        return JsonResponse({"ok": False, "errors": form.errors.get_json_data(),
                             "detail": " ".join(form.non_field_errors()) or "Review the highlighted fields."}, status=code)
    return _editor(request, post, form, status=code)


@private_page
@editorial_required
@require_http_methods(["GET", "POST"])
def new(request):
    return _save(request) if request.method == "POST" else _editor(request)


@private_page
@editorial_required
@require_http_methods(["GET", "POST"])
def edit(request, post_id):
    post = get_object_or_404(Post, pk=post_id)
    return _save(request, post) if request.method == "POST" else _editor(request, post)


@private_page
@editorial_required
@require_POST
def autosave(request, post_id):
    return _save(request, get_object_or_404(Post, pk=post_id), autosave=True)


@private_page
@editorial_required
@require_GET
def preview(request, post_id):
    from .public import build_article_context

    post = get_object_or_404(Post, pk=post_id)
    return render(request, "blog/public/article.html", build_article_context(post, post.draft, preview=True, request=request))


@private_page
@editorial_required
@require_POST
@ratelimit(limit=60, window=300, methods=("POST",))
def upload_image(request):
    file = request.FILES.get("image")
    if file is None:
        return JsonResponse({"ok": False, "detail": "Choose an image to upload."}, status=400)
    try:
        image = services.upload_image(file, request.user, request.POST.get("alt_text", ""), request.POST.get("caption", ""))
    except ValidationError as exc:
        return JsonResponse({"ok": False, "detail": " ".join(exc.messages)}, status=400)
    return JsonResponse({"ok": True, **_image_data(image)}, status=201)


@private_page
@editorial_required
@require_GET
def media(request, image_id):
    image = get_object_or_404(BlogImage, pk=image_id)
    try:
        return services.media_response(image, private=True, width=request.GET.get("w"))
    except FileNotFoundError as exc:
        raise Http404("Image not found.") from exc


@private_page
@editorial_required
@require_POST
def restore(request, post_id, revision_id):
    post = get_object_or_404(Post, pk=post_id)
    revision = get_object_or_404(Revision, pk=revision_id, post=post)
    try:
        data = _data(request)
        post = services.restore_revision(post, revision, request.user, data.get("version"))
    except ValidationError as exc:
        if _json_request(request):
            return JsonResponse({"ok": False, "detail": " ".join(exc.messages)}, status=409 if isinstance(exc, services.ConflictError) else 400)
        messages.error(request, " ".join(exc.messages))
    else:
        if _json_request(request):
            return JsonResponse(_post_response(post))
        messages.success(request, "Revision restored to the draft. Publish changes when ready.")
    return redirect("blog_admin:edit", post_id=post.id)
