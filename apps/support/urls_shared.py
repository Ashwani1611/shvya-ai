from django.urls import path
from . import views
app_name = "support-shared"
urlpatterns = [
    path("t/<str:token>/", views.shared_ticket, name="ticket"),
    path("t/<str:token>/reply/", views.shared_reply, name="reply"),
    path("t/<str:token>/update/", views.shared_update, name="update"),
    path("t/<str:token>/state/", views.shared_state, name="state"),
    path("t/<str:token>/files/<uuid:attachment_id>/", views.shared_attachment, name="attachment"),
]
