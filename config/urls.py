from django.contrib import admin
from django.urls import include, path

from apps.superadmin.views import admin_global_search

from rest_framework_simplejwt.views import (
    TokenObtainPairView,
    TokenRefreshView,
)


urlpatterns = [
    # =========================================================
    # SHVYA Admin — Global Search
    #
    # IMPORTANT:
    # This route must appear before Django's admin/ URL so
    # /admin/search/ is handled by SHVYA's search endpoint.
    # =========================================================

    path(
        "admin/search/",
        admin_global_search,
        name="admin-global-search",
    ),

    # =========================================================
    # Django Admin
    # =========================================================

    path(
        "admin/",
        admin.site.urls,
    ),

    # =========================================================
    # JWT Authentication
    # =========================================================

    path(
        "api/v1/auth/token/",
        TokenObtainPairView.as_view(),
        name="token_obtain_pair",
    ),

    path(
        "api/v1/auth/token/refresh/",
        TokenRefreshView.as_view(),
        name="token_refresh",
    ),

    # =========================================================
    # CRM API
    # =========================================================

    path(
        "api/v1/leads/",
        include("apps.crm.urls"),
    ),

    # =========================================================
    # Accounts
    #
    # Includes:
    #
    # /one-time-login/
    # /logout/
    #
    # URL names:
    #
    # one-time-login
    # crm-logout
    # =========================================================

    path(
        "",
        include("apps.accounts.urls"),
    ),

    # =========================================================
    # CRM Web Dashboard
    # =========================================================

    path(
        "dashboard/",
        include("apps.crm.web_urls"),
    ),

    # =========================================================
    # Super Admin Console
    # =========================================================

    path(
        "superadmin/",
        include("apps.superadmin.urls"),
    ),
]