from django.urls import path

from . import views

app_name = "blog_admin"
urlpatterns = [
    path("", views.index, name="index"),
    path("login/", views.login, name="login"),
    path("new/", views.new, name="new"),
    path("images/upload/", views.upload_image, name="upload_image"),
    path("media/<uuid:image_id>/", views.media, name="media"),
    path("<uuid:post_id>/", views.edit, name="edit"),
    path("<uuid:post_id>/autosave/", views.autosave, name="autosave"),
    path("<uuid:post_id>/preview/", views.preview, name="preview"),
    path("<uuid:post_id>/revisions/<int:revision_id>/restore/", views.restore, name="restore"),
]
