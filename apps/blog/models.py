"""Platform-owned editorial records; no customer organization can own a blog."""
import uuid

from django.conf import settings
from django.db import models
from django.urls import reverse

from .storage import blog_image_storage


class Category(models.Model):
    name = models.CharField(max_length=80, unique=True)
    slug = models.SlugField(max_length=100, unique=True)

    class Meta:
        ordering = ["name"]
        verbose_name_plural = "categories"

    def __str__(self):
        return self.name


class Post(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        REVIEW = "review", "In review"
        SCHEDULED = "scheduled", "Scheduled"
        PUBLISHED = "published", "Published"
        ARCHIVED = "archived", "Archived"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    draft = models.JSONField(default=dict)
    # Public views MUST read this snapshot, never editorial fields above.
    published_snapshot = models.JSONField(null=True, blank=True)
    published_slug = models.SlugField(max_length=180, unique=True, null=True, blank=True)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.DRAFT, db_index=True)
    version = models.PositiveIntegerField(default=1)
    scheduled_at = models.DateTimeField(null=True, blank=True, db_index=True)
    scheduled_snapshot = models.JSONField(null=True, blank=True)
    scheduled_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                     on_delete=models.SET_NULL, related_name="scheduled_blog_posts")
    scheduling_error = models.CharField(max_length=300, blank=True)
    published_at = models.DateTimeField(null=True, blank=True, db_index=True)
    live_updated_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL,
                                  related_name="created_blog_posts")
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL,
                                  related_name="updated_blog_posts")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]
        permissions = [("publish_post", "Can publish and unpublish website articles")]
        constraints = [models.CheckConstraint(
            condition=(models.Q(published_snapshot__isnull=True, published_slug__isnull=True)
                       | models.Q(published_snapshot__isnull=False, published_slug__isnull=False)),
            name="blog_live_snapshot_has_slug",
        )]

    @property
    def title(self):
        return self.draft.get("title") or "Untitled article"

    @property
    def slug(self):
        return self.draft.get("slug", "")

    @property
    def has_unpublished_changes(self):
        if self.published_snapshot is None:
            return True
        published = {k: v for k, v in self.published_snapshot.items()
                     if k not in {"published_modified_at", "published_at"}}
        return self.draft != published

    def get_absolute_url(self):
        return reverse("blog:detail", kwargs={"slug": self.published_slug or self.slug})

    def __str__(self):
        return self.title


class BlogImage(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    file = models.FileField(storage=blog_image_storage, upload_to="blog-images/")
    thumbnail_file = models.FileField(storage=blog_image_storage, upload_to="blog-images/", blank=True)
    width = models.PositiveIntegerField()
    height = models.PositiveIntegerField()
    thumbnail_width = models.PositiveIntegerField(default=0)
    thumbnail_height = models.PositiveIntegerField(default=0)
    alt_text = models.CharField(max_length=300, blank=True)
    caption = models.CharField(max_length=500, blank=True)
    live_posts = models.ManyToManyField(Post, related_name="published_images", blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL,
                                  related_name="blog_images")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    @property
    def public_url(self):
        return reverse("blog:media", kwargs={"image_id": self.id})

    @property
    def preview_url(self):
        return reverse("blog_admin:media", kwargs={"image_id": self.id})

    def __str__(self):
        return self.alt_text or f"Image {str(self.id)[:8]}"


class Revision(models.Model):
    post = models.ForeignKey(Post, on_delete=models.CASCADE, related_name="revisions")
    snapshot = models.JSONField()
    version = models.PositiveIntegerField()
    reason = models.CharField(max_length=40)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL,
                                  related_name="blog_revisions")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-version", "-id"]
        constraints = [models.UniqueConstraint(fields=["post", "version"], name="blog_revision_version_unique")]


class SlugRedirect(models.Model):
    old_slug = models.SlugField(max_length=180, unique=True)
    post = models.ForeignKey(Post, on_delete=models.CASCADE, related_name="slug_redirects")
    created_at = models.DateTimeField(auto_now_add=True)
