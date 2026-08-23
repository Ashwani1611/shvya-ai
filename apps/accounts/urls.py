from django.urls import path

from .views import (
    one_time_login_view,
    crm_logout_view,
    crm_forgot_password_view,
    crm_password_reset_sent_view,
    crm_password_reset_view,
    crm_signup_view,
    crm_password_reset_complete_view,
)


urlpatterns = [

    # =========================================================
    # ONE-TIME LOGIN
    # =========================================================

    path(
        "one-time-login/",
        one_time_login_view,
        name="one-time-login",
    ),

    # =========================================================
    # CRM LOGOUT
    # =========================================================

    path(
        "logout/",
        crm_logout_view,
        name="crm-logout",
    ),

    # =========================================================
    # CRM FORGOT PASSWORD
    # =========================================================

    path(
        "dashboard/forgot-password/",
        crm_forgot_password_view,
        name="crm-forgot-password",
    ),

    path(
        "dashboard/forgot-password/sent/",
        crm_password_reset_sent_view,
        name="crm-password-reset-sent",
    ),

    # =========================================================
    # CRM PASSWORD RESET
    # =========================================================

    path(
        "dashboard/reset-password/<uidb64>/<token>/",
        crm_password_reset_view,
        name="crm-password-reset",
    ),

    path(
        "dashboard/reset-password/complete/",
        crm_password_reset_complete_view,
        name="crm-password-reset-complete",
    ),
    
    # =========================================================
    # CRM SIGNUP
    # =========================================================

    path(
        "signup/",
        crm_signup_view,
        name="crm-signup",
    ),
]