from django.urls import path

from . import views

app_name = "shvya_calendar_public"

urlpatterns = [
    path(
        "<uuid:public_id>/<slug:slug>/",
        views.public_page,
        name="page",
    ),
    path(
        "<uuid:public_id>/<slug:slug>/submit/",
        views.public_submit,
        name="submit",
    ),
    path(
        "<uuid:public_id>/<slug:slug>/schedule/<uuid:submission_id>/",
        views.public_schedule,
        name="schedule",
    ),
    path(
        "booking/<uuid:booking_id>/<str:cancel_token>/",
        views.public_confirmation,
        name="confirmation",
    ),
    path(
        "booking/<uuid:booking_id>/<str:cancel_token>/cancel/",
        views.public_cancel,
        name="cancel",
    ),
]
