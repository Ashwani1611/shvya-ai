from django.urls import path
from . import views
app_name = "support-staff"
urlpatterns = [
    path("", views.staff_list, name="list"),
    path("bulk/", views.bulk, name="bulk"),
    path("export/", views.export, name="export"),
    path("configuration/<slug:section>/", views.configuration, name="configuration"),
    path("configuration/<slug:section>/<int:object_id>/", views.configuration, name="configuration-edit"),
    path("attachments/<uuid:attachment_id>/", views.staff_attachment, name="attachment"),
    path("tickets/<uuid:ticket_id>/", views.staff_detail, name="detail"),
    path("tickets/<uuid:ticket_id>/reply/", views.staff_reply, name="reply"),
    path("tickets/<uuid:ticket_id>/update/", views.staff_update, name="update"),
    path("tickets/<uuid:ticket_id>/state/", views.staff_state, name="state"),
    path("tickets/<uuid:ticket_id>/typing/", views.typing, name="typing"),
    path("tickets/<uuid:ticket_id>/share/", views.staff_share, name="share"),
    path("tickets/<uuid:ticket_id>/share/<uuid:grant_id>/revoke/", views.staff_revoke, name="revoke"),
    path("tickets/<uuid:ticket_id>/merge/", views.merge, name="merge"),
    path("tickets/<uuid:ticket_id>/work/", views.work, name="work"),
    path("tickets/<uuid:ticket_id>/work/<int:item_id>/complete/", views.work_complete, name="work-complete"),
]
