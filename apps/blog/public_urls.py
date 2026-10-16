from django.urls import path

from . import public, tracking


app_name = "blog"

urlpatterns = [
    path("", public.index, name="index"),
    path("category/<slug:slug>/", public.category, name="category"),
    path("media/<uuid:image_id>/", public.media, name="media"),
    path("views/<uuid:post_id>/", tracking.record_view, name="record_view"),
    path("<slug:slug>/", public.detail, name="detail"),
]
