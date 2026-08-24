"""
Shared constants for the CRM app.
"""

# Session area key used with apps.accounts.session_utils.get_session_store()
# to namespace the CRM's session cookie separately from Django admin and
# superadmin sessions. Must match a key in SHVYA_SESSION_COOKIES.
CRM_SESSION_AREA = "dashboard"
