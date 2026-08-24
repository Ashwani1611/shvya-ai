import hashlib
import secrets
import uuid

from django.conf import settings
from django.contrib.auth.models import (
    AbstractBaseUser,
    BaseUserManager,
    PermissionsMixin,
)
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

from apps.organizations.models import Organization


class UserManager(BaseUserManager):

    def create_user(
        self,
        email,
        organization=None,
        password=None,
        **extra_fields,
    ):
        if not email:
            raise ValueError("Users must have an email address.")

        role = extra_fields.get(
            "role",
            User.Role.AGENT,
        )

        if role != User.Role.SUPERADMIN and organization is None:
            raise ValueError(
                "Organization is required for non-superadmin users."
            )

        email = self.normalize_email(email)

        user = self.model(
            email=email,
            organization=organization,
            **extra_fields,
        )

        user.set_password(password)

        user.save(using=self._db)

        return user

    def create_superuser(
        self,
        email,
        password=None,
        **extra_fields,
    ):
        extra_fields.setdefault(
            "is_staff",
            True,
        )

        extra_fields.setdefault(
            "is_superuser",
            True,
        )

        extra_fields.setdefault(
            "role",
            User.Role.SUPERADMIN,
        )

        return self.create_user(
            email=email,
            organization=None,
            password=password,
            **extra_fields,
        )


class User(
    AbstractBaseUser,
    PermissionsMixin,
):

    class Role(models.TextChoices):
        SUPERADMIN = (
            "superadmin",
            "Superadmin",
        )

        ADMIN = (
            "admin",
            "Admin",
        )

        AGENT = (
            "agent",
            "Agent",
        )

    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )

    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="users",
        null=True,
        blank=True,
    )

    name = models.CharField(
        max_length=150,
    )

    email = models.EmailField(
        unique=True,
    )

    phone = models.CharField(
        max_length=32,
        blank=True,
    )

    role = models.CharField(
        max_length=20,
        choices=Role.choices,
        default=Role.AGENT,
    )

    is_active = models.BooleanField(
        default=True,
    )

    is_staff = models.BooleanField(
        default=False,
    )

    last_login_at = models.DateTimeField(
        null=True,
        blank=True,
    )

    created_at = models.DateTimeField(
        auto_now_add=True,
    )

    updated_at = models.DateTimeField(
        auto_now=True,
    )

    objects = UserManager()

    USERNAME_FIELD = "email"

    REQUIRED_FIELDS = []

    class Meta:
        ordering = ["-created_at"]

    def clean(self):
        super().clean()

        if (
            self.role == self.Role.SUPERADMIN
            and self.organization_id is not None
        ):
            raise ValidationError(
                "Superadmin cannot belong to a client organization."
            )

        if (
            self.role != self.Role.SUPERADMIN
            and self.organization_id is None
        ):
            raise ValidationError(
                "Non-superadmin users must belong to an organization."
            )

    def __str__(self):
        return self.email


class OneTimeLoginToken(models.Model):

    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="one_time_login_tokens",
    )

    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="one_time_login_tokens",
    )

    token_hash = models.CharField(
        max_length=128,
        unique=True,
    )

    expires_at = models.DateTimeField()

    used_at = models.DateTimeField(
        null=True,
        blank=True,
    )

    created_at = models.DateTimeField(
        auto_now_add=True,
    )

    class Meta:
        ordering = ["-created_at"]

    @classmethod
    def create_token(
        cls,
        user,
        organization,
        expires_at,
    ):
        raw_token = secrets.token_urlsafe(48)

        token_hash = hashlib.sha256(
            raw_token.encode("utf-8")
        ).hexdigest()

        token = cls.objects.create(
            user=user,
            organization=organization,
            token_hash=token_hash,
            expires_at=expires_at,
        )

        return token, raw_token

    def is_valid(self):
        return (
            self.used_at is None
            and self.expires_at > timezone.now()
            and self.user.is_active
        )

    def __str__(self):
        return f"{self.user.email} → one-time login"