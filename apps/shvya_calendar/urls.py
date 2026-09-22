from django.urls import path

from . import views, workspace

app_name = "shvya_calendar"

urlpatterns = [
    path("", views.calendar_index, name="index"),
    path("bookings/", views.calendar_index, name="bookings"),
    path("calendar/", workspace.calendar_workspace, name="calendar"),
    path("calendar/events/", workspace.calendar_events, name="events"),
    path("calendar/bookings/<uuid:booking_id>/", workspace.booking_detail, name="booking_detail"),
    path("calendar/bookings/<uuid:booking_id>/slots/", workspace.booking_slots, name="booking_slots"),
    path("calendar/bookings/<uuid:booking_id>/update/", workspace.booking_update, name="booking_update"),
    path("<uuid:page_id>/", views.calendar_editor, name="editor"),
    path("<uuid:page_id>/save/", views.calendar_editor_save, name="save"),
    path("<uuid:page_id>/status/", views.calendar_status, name="status"),
    path("<uuid:page_id>/preview/", views.calendar_preview, name="preview"),
    path(
        "attachments/<uuid:attachment_id>/download/",
        views.calendar_attachment_download,
        name="attachment_download",
    ),
    path(
        "<uuid:page_id>/reminders/add/",
        views.calendar_reminder_add,
        name="reminder_add",
    ),
    path(
        "<uuid:page_id>/reminders/<uuid:step_id>/delete/",
        views.calendar_reminder_delete,
        name="reminder_delete",
    ),
    path(
        "<uuid:page_id>/blocks/add/",
        views.calendar_block_add,
        name="block_add",
    ),
    path(
        "<uuid:page_id>/blocks/<uuid:block_id>/delete/",
        views.calendar_block_delete,
        name="block_delete",
    ),
    path(
        "call-reminders/<uuid:delivery_id>/complete/",
        views.call_reminder_complete,
        name="call_reminder_complete",
    ),
    path(
        "<uuid:page_id>/google/connect/",
        views.google_connect,
        name="google_connect",
    ),
    path(
        "google/callback/",
        views.google_callback,
        name="google_callback",
    ),
    path(
        "<uuid:page_id>/google/disconnect/",
        views.google_disconnect,
        name="google_disconnect",
    ),
]
