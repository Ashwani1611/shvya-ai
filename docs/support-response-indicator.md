# Help & Support: response-required sidebar indicator

The authenticated organization dashboard highlights Help & Support with a continuous,
gentle blue pulse and a ticket-count badge when Shvya-Ops is waiting for a response.
This is not an unread notification: viewing a ticket, clicking the link, changing pages,
refreshing, and opening another tab do not acknowledge the response.

## Authoritative rule

`apps/support/attention.py` reuses the existing committed Ticket/TicketMessage history.
For each accessible, non-merged, non-closed ticket, find the latest non-internal message
from an organization customer or Shvya-Ops. A latest Shvya-Ops reply requires action.
There is no duplicated boolean column, read flag, localStorage dismissal, migration,
Celery task or second mutation path to keep in sync.

A public reply from any organization member who may access that ticket clears its
indicator for the other authorized members. Portal and verified-email replies both
use the existing service. A subsequent staff public reply requires action again.
Closed tickets do not need a response, including custom statuses with Closed behavior.
If reopened, their existing conversation is re-evaluated. Staff internal notes and
anonymous shared-link replies do not acknowledge a response. A permitted shared-link
closure still closes the ticket under the existing policy.

Multiple replies on one ticket count as one. With multiple pending tickets, handling
one reduces the count, but the indicator stays on until all accessible tickets are
answered or closed. Merged source tickets are excluded; the primary's consolidated
public conversation determines its state. Email notification switches/SMTP failures
have no bearing on this UI feature.

## Access and delivery

The GET-only `support-client:attention` route at
`/dashboard/support-portal/attention/` returns only `{"count": N}`. It derives the
organization from the active dashboard identity. No client-supplied organization ID,
ticket ID, message body, staff note, user identity or email address is returned.

Existing `visible_tickets()` rules apply to both the initial server-rendered bootstrap
and polling. Default company-wide access shares the indicator across the organization.
When an organization enables own-ticket restrictions, hidden tickets do not contribute
to that restricted user's badge; organization administrators retain their existing access.

The template tag is included only for an authenticated dashboard-area user. The
endpoint retains area authentication, inactive-organization checks and no-store/private
headers. Existing CSRF protection on all ticket mutations remains unchanged.

Open visible pages refresh the count every 15 seconds, also refreshing when focused,
returned from page history, reconnected, or after successful HTMX requests. Hidden
pages pause scheduled polling. Fetches have a 10-second timeout, cannot overlap, and
back off to at most 120 seconds on errors. An offline/server error does not dismiss the
last verified indicator. Session expiry or access revocation removes stale UI and stops
polling until a fresh authenticated page load.

## UI and accessibility

The existing sidebar link, keyboard behavior, tooltip and navigation destination are
preserved. Collapsed sidebars show a blue dot; expanded sidebars show a capped `99+`
badge with the full count in its accessible label. Sidebar reconstruction does not
duplicate badges. No response-needed state is stored in browser storage.

Reduced-motion preference replaces the animation with a steady blue highlight and
badge, without clearing the underlying action requirement. The text does not flash
or disappear. Screen-reader status text updates only when the count changes.

## Checks and release

Run the normal PostgreSQL-backed CI, including:

```
pytest apps/support/tests/test_attention.py apps/support/tests/test_attention_browser.py
python manage.py makemigrations --check --dry-run --settings=config.settings.testing
ruff check .
```

The browser tests use real CSS/JavaScript with an isolated, intercepted loopback test
origin. They do not contact production, submit customer tickets, or send email. Manual
staging acceptance: keep an organization's dashboard open, send a public staff reply,
wait up to 15 seconds, view the ticket without responding, then reply as another allowed
organization user or close it. Repeat with two tickets and with a restricted user.

Promote through staging and CI before main. This feature changes no deployment YAML,
mail credentials, authentication architecture, model fields or database schema.
