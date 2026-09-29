# Organisation-owned Google Calendar with optional SHVYA hosting

## Release change

Google account selection for new events is now organisation-first with **explicit
organisation opt-in** for SHVYA-managed fallback. One shared OAuth client ID/secret
identifies the SHVYA application; each host authorises its own Google account.

This policy is the additional activation requirement for the platform integration
documented in `docs/google-meet-platform-setup.md`. Global `PLATFORM_ENABLED=true`
alone no longer permits new shared-organiser events for every organisation.

## Behaviour

| Situation | Result |
| --- | --- |
| Active host Google connection exists in the same organisation | Use the host connection. |
| No host Google connection; no explicit organisation consent | SHVYA booking remains valid. Google status is not connected; no link is invented. |
| No host connection; organisation opts in; backend platform setup enabled | Request a SHVYA-managed event/link for Google Meet bookings. |
| Host connection exists but refresh/API access fails | Report/retry the original account error. Do not switch to the platform account. |
| Existing Google event, then preference/host changes | Preserve the original organiser, event ID and calendar binding. |
| Organisation opts out while a new booking is queued without an event ID | Worker re-reads the current preference before selecting the platform organiser. |
| Pending booking already has a persisted platform event identity | Treat it as an existing event, including recovery after a lost HTTP response. |

A preference update does not cancel, move or delete Google events. Already
accepted/in-flight provider work is not recalled. To stop the platform integration
entirely, resolve existing meetings before disabling backend credentials.

## Organisation settings

Open a booking page in SHVYA Calendar and select **Google settings**. The new route
is named `shvya_calendar:google_settings` (`google-settings/` beneath the existing
SHVYA Calendar mount).

The organisation administrator selects either:

1. **Use my organisation's Google account (recommended).** Default when there is
   no consent record. Select a host in the booking page and use the existing
   **Connect Google Calendar** flow while signed in as that host.
2. **Use my organisation's account, with SHVYA-managed fallback.** The host's
   connected account remains primary. The fallback also requires valid backend
   Google credentials. The administrator may save this preference before backend
   setup is finished; the screen does not claim that links are working.

The selected preference applies across that organisation's booking pages. The
stored value is `Organization.settings.calendar_google.allow_platform_fallback`,
with literal JSON boolean `true` required. Missing/malformed/string values fail
closed. An update preserves other settings and records the actor and update time.
The save operation derives the tenant from the authenticated user, checks the
current administrator role, and uses row locking for the settings update.

## Privacy and hosting

SHVYA's existing calendar feed, detail, availability and update access checks
remain organisation-scoped. This change does not create a global calendar feed,
share the central Google calendar, or grant cross-tenant permissions.

The central Google account can still contain multiple tenants' platform-hosted
events. Never share that account's login or whole-calendar access with tenants.
A tenant-owned Google connection is a separate Google grant, not a copy of the
platform refresh token. A unique Meet link does not grant organiser or co-host
rights to tenant staff. No automatic public meeting admission is enabled.

The booking detail panel identifies the persisted Google organiser as either
**Organisation Google account**, **SHVYA-managed Google Meet**, or **Not assigned
yet**. It does not expose the central account's email or any credentials. The
organisation settings screen lists only that organisation's active connections.

## Recovery and existing bookings

Future `not_connected` Google Meet bookings without Google event IDs are eligible
for central recovery only for active organisations that explicitly opted in.
The JSON consent filter is applied before the bounded 100-booking selection and
again before changing sync status. Worker execution re-checks consent. Existing
host-owned events are never transferred to SHVYA credentials.

No schema migration or mass update of customer records is required. Be aware of
the intentional default change: existing organisations without consent will not
get new central-account meeting links until an organisation administrator opts
in. Existing platform-owned events remain maintainable. Booked at mapping,
validation, reminders and organisation-owned Google connections are preserved.

## Backend setup

For organisation-owned Google connections, configure the existing shared values:

```dotenv
GOOGLE_CALENDAR_CLIENT_ID=YOUR_WEB_OAUTH_CLIENT_ID
GOOGLE_CALENDAR_CLIENT_SECRET=YOUR_WEB_OAUTH_CLIENT_SECRET
```

Enable the Google Calendar API and configure the production callback returned by
`python manage.py check_google_calendar`. Each host signs in with Google and
consents; organisations do not need to create their own Google Cloud projects.
Do not replace an OAuth client already used by existing refresh tokens casually.

Only for optional SHVYA-managed hosting, also configure the existing values:

```dotenv
GOOGLE_CALENDAR_PLATFORM_ENABLED=true
GOOGLE_CALENDAR_PLATFORM_ACCOUNT_EMAIL=YOUR_DEDICATED_SHVYA_GOOGLE_ACCOUNT
GOOGLE_CALENDAR_PLATFORM_REFRESH_TOKEN=YOUR_OFFLINE_REFRESH_TOKEN
GOOGLE_CALENDAR_PLATFORM_CALENDAR_ID=YOUR_DEDICATED_SHVYA_GOOGLE_CALENDAR
```

Do not put real secrets into chat, source control, screenshots or test reports.
This patch does not supply Google credentials or modify a production environment.

## Validation before release

Use the repository's isolated testing environment with its PostgreSQL/Redis
services. Do not point tests at production databases or queues. Run:

```bash
ruff check apps/shvya_calendar/google_policy.py \
  apps/shvya_calendar/google_settings.py \
  apps/shvya_calendar/platform_google.py \
  apps/shvya_calendar/tasks.py \
  apps/shvya_calendar/templatetags/calendar_google.py \
  apps/shvya_calendar/test_google_policy.py \
  apps/shvya_calendar/test_platform_google.py
python manage.py makemigrations --check --dry-run --settings=config.settings.testing
python manage.py check --settings=config.settings.testing
pytest apps/shvya_calendar/test_google_policy.py \
  apps/shvya_calendar/test_attribute_sync.py \
  apps/shvya_calendar/test_platform_google.py \
  apps/shvya_calendar/test_platform_google_edges.py \
  apps/shvya_calendar/test_workspace.py
```

The new Django tests cover routing, explicit consent, actor/tenant validation,
settings preservation, recovery, CSRF, booking-view isolation and saved organiser
labels. Existing platform fixtures now explicitly opt in. All providers are
simulated in this suite; passing it is not proof of real Google connectivity.

After CI and Security succeed and deployment is approved through the normal
release workflow, the existing `check_google_calendar --live` command can verify
central credentials without creating an event. A real Meet creation test still
requires an authorised test account and isolated test bookings. No live test was
performed while preparing this patch.

## Google references

- OAuth web-server flow: https://developers.google.com/identity/protocols/oauth2/web-server
- Calendar event conferences: https://developers.google.com/workspace/calendar/api/v3/reference/events
- Sharing and calendar access: https://developers.google.com/workspace/calendar/api/concepts/sharing
