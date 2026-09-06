"""Small UI wrapper for failed-message diagnostics in the Chats thread."""

from . import views_flat


_FAILURE_SUMMARY_SCRIPT = b"""
<script data-shvya-whatsapp-failure-summary>
(function () {
  document.querySelectorAll('details').forEach(function (details) {
    const summary = details.querySelector(':scope > summary');
    if (!summary || summary.textContent.indexOf('Not sent') === -1) return;

    const heading = details.querySelector('.font-semibold.text-red-700');
    if (!heading) return;

    const diagnostic = heading.textContent.trim();
    if (!diagnostic) return;
    summary.textContent = 'Not sent \\u00b7 ' + diagnostic + ' \\u00b7 View details';
  });
})();
</script>
"""


def _inject_failure_summary(response):
    content_type = response.get("Content-Type", "")
    if response.status_code != 200 or "text/html" not in content_type:
        return response

    content = response.content
    if b"data-shvya-whatsapp-failure-summary" in content:
        return response

    marker = b"</body>"
    if marker not in content:
        return response

    response.content = content.replace(
        marker,
        _FAILURE_SUMMARY_SCRIPT + marker,
        1,
    )
    if response.has_header("Content-Length"):
        response["Content-Length"] = str(len(response.content))
    return response


def whatsapp_chat_detail_view(request, lead_id):
    """Render the normal chat, then expose the exact failure code in its summary."""
    return _inject_failure_summary(
        views_flat.whatsapp_chat_detail_view(request, lead_id)
    )
