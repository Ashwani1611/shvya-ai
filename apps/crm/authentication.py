from django.utils import timezone

from rest_framework.authentication import BaseAuthentication
from rest_framework.exceptions import AuthenticationFailed

from apps.organizations.models import APIKey


class APIKeyPrincipal:

    is_authenticated = True
    is_anonymous = False

    def __init__(self, api_key):
        self.api_key = api_key
        self.organization = api_key.organization

    def __str__(self):
        return f"SHVYA API Key: {self.api_key.name}"


class SHVYAAPIKeyAuthentication(
    BaseAuthentication
):

    keyword = "X-SHVYA-API-KEY"

    def authenticate(self, request):

        raw_key = request.headers.get(
            self.keyword
        )

        if not raw_key:
            raise AuthenticationFailed(
                "X-SHVYA-API-KEY header is required."
            )

        prefix = raw_key[:16]

        api_key = (
            APIKey.objects
            .select_related("organization")
            .filter(
                key_prefix=prefix,
                is_active=True,
            )
            .first()
        )

        if api_key is None:
            raise AuthenticationFailed(
                "Invalid SHVYA API key."
            )

        if (
            api_key.expires_at
            and api_key.expires_at <= timezone.now()
        ):
            raise AuthenticationFailed(
                "SHVYA API key has expired."
            )

        if not api_key.verify(raw_key):
            raise AuthenticationFailed(
                "Invalid SHVYA API key."
            )

        api_key.last_used_at = timezone.now()

        api_key.save(
            update_fields=[
                "last_used_at",
            ]
        )

        return (
            APIKeyPrincipal(api_key),
            api_key,
        )

    def authenticate_header(
        self,
        request,
    ):
        return self.keyword