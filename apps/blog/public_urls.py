from django.urls import path

from . import public


app_name = "blog"

urlpatterns = [
    path("", public.index, name="index"),
    path("category/<slug:slug>/", public.category, name="category"),
    path("media/<uuid:image_id>/", public.media, name="media"),
    path("<slug:slug>/", public.detail, name="detail"),
]
