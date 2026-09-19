from django.urls import path

from apps.integrations.views.mcp import (
    diagnostic_mcp,
    diagnostic_oauth_authorize,
    diagnostic_oauth_register,
    diagnostic_oauth_resource_metadata,
    diagnostic_oauth_server_metadata,
    diagnostic_oauth_token,
)


urlpatterns = [
    path(
        ".well-known/oauth-protected-resource",
        diagnostic_oauth_resource_metadata,
        name="shvya-diagnostic-oauth-resource-metadata",
    ),
    path(
        ".well-known/oauth-authorization-server",
        diagnostic_oauth_server_metadata,
        name="shvya-diagnostic-oauth-server-metadata",
    ),
    path(
        "oauth/register",
        diagnostic_oauth_register,
        name="shvya-diagnostic-oauth-register",
    ),
    path(
        "oauth/authorize",
        diagnostic_oauth_authorize,
        name="shvya-diagnostic-oauth-authorize",
    ),
    path(
        "oauth/token",
        diagnostic_oauth_token,
        name="shvya-diagnostic-oauth-token",
    ),
    path(
        "mcp/",
        diagnostic_mcp,
        name="shvya-diagnostic-mcp",
    ),
]
