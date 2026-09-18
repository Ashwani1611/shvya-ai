from django.urls import path
from . import views
app_name = "support-client"
urlpatterns = [
    path("", views.customer_list, name="list"),
    path("create/", views.create, name="create"),
    path("attachments/<uuid:attachment_id>/", views.customer_attachment, name="attachment"),
    path("tickets/<uuid:ticket_id>/", views.customer_detail, name="detail"),
    path("tickets/<uuid:ticket_id>/reply/", views.customer_reply, name="reply"),
    path("tickets/<uuid:ticket_id>/update/", views.customer_update, name="update"),
    path("tickets/<uuid:ticket_id>/state/", views.customer_state, name="state"),
    path("tickets/<uuid:ticket_id>/share/", views.customer_share, name="share"),
    path("tickets/<uuid:ticket_id>/share/<uuid:grant_id>/revoke/", views.customer_revoke, name="revoke"),
]
