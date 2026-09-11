from django.test import RequestFactory
from django.urls import resolve, reverse

from apps.core.context_processors import sidebar_nav


def test_touchpoints_route_renders_updated_page_and_navigation():
    url = reverse("crm-auto-follow-ups-touchpoints")
    assert url == "/dashboard/auto-follow-ups/touchpoints/"
    request = RequestFactory().get(url)
    match = resolve(url)
    response = match.func(request, **match.kwargs)
    assert response.status_code == 200
    html = response.content.decode()
    assert "Touchpoints" in html
    assert "Workflows" not in html
    assert url in html

    parent = next(
        item for item in sidebar_nav(request)["nav_items"]
        if item["label"] == "Auto Follow-ups"
    )
    assert parent["is_active"]
    assert parent["path_prefix"] == "/dashboard/auto-follow-ups/"
    sequences, touchpoints = parent["children"]
    assert touchpoints["is_active"]
    assert not sequences["is_active"]
    assert reverse(sequences["url_name"]) == "/dashboard/auto-follow-ups/sequences/"


def test_old_workflows_bookmark_redirects_and_preserves_query_string():
    url = "/dashboard/auto-follow-ups/workflows/"
    request = RequestFactory().get(url + "?source=bookmark")
    match = resolve(url)
    response = match.func(request, **match.kwargs)
    assert response.status_code == 301
    assert response["Location"] == (
        "/dashboard/auto-follow-ups/touchpoints/?source=bookmark"
    )
