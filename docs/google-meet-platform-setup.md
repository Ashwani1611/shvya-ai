# SHVYA-managed Google Meet and Booked at

## Behaviour

Every organisation keeps its fixed CRM `booked_at` date/time attribute. A SHVYA
Calendar booking maps into the attribute; a valid manual edit creates or
reschedules the booking in that pipeline's published calendar. Availability,
capacity and tenant checks still apply. A lead with multiple active bookings, or
multiple published calendars in its pipeline, must choose the booking in Calendar
first. This change does not silently choose a different organisation's calendar.

Google Meet has two organiser modes:

1. A host's connected organisation Google account is used for a NEW event when
   available. The account remains organisation-scoped.
2. When the host has no Google connection, optional platform credentials create
   a SHVYA-owned event and unique Meet link. No per-organisation Google login is
   needed for this mode. Select Google Meet in Calendar & Meeting.

Existing Google events never fall back to a different provider. Platform event
IDs use the deterministic `5a` + booking UUID hex namespace. The chosen concrete
calendar ID is persisted before the request, including for retries after a lost
HTTP response. Connecting an organisation account later does not move an existing
platform meeting. Do not change the configured platform calendar while it owns
active events; configure its original calendar to manage those events.

Platform events are private, do not reveal other guests, do not grant guests
editing/inviting rights, and disable the organiser's default event reminders.
Each event carries private organisation/booking IDs checked during recovery.
The shared platform calendar is NOT used as a free/busy calendar for other
organisations. SHVYA page availability and a connected host's own Google conflicts
continue to govern slots.

Meeting links are attached to SHVYA bookings. Google invitation emails are sent
only when the page's Invite lead to Google event option is enabled and the lead
has an email address. SHVYA WhatsApp/email/call reminders retain their own controls.

## Required Google setup (once for SHVYA)

A simple API key cannot create these authenticated events. Use OAuth 2.0 and a
dedicated SHVYA-owned Google account with Google Meet enabled. This implementation
uses the Google Calendar API to create the event and its conference; it does not
require a separate Meet REST API key or a service-account JSON key.

1. In a SHVYA Google Cloud project, enable Google Calendar API.
2. Configure the OAuth consent screen and create an OAuth client of type Web
   application. Save the client ID/secret only in backend secret storage.
3. Register the production callback URI. Run `python manage.py check_google_calendar`
   to obtain `organization_oauth_callback_path`, then prepend the canonical
   `https://dashboard.shvya-ai.com` origin. Keep staging callbacks separate.
4. Authorise a DEDICATED SHVYA organiser account for these exact scopes:
   `openid email https://www.googleapis.com/auth/calendar`, with offline access.
   Each organisation can still use the normal SHVYA Connect Google Calendar flow
   instead; do not repurpose a customer's token as the platform credential.
5. To obtain the dedicated account's refresh token without a custom script, use
   Google's OAuth Playground at https://developers.google.com/oauthplayground/.
   In its settings select Use your own OAuth credentials and enter this client
   ID/secret. Register `https://developers.google.com/oauthplayground` as an
   authorised redirect URI for that client. Authorise the scopes above using the
   dedicated SHVYA account, then exchange the code for tokens. Store the REFRESH
   token directly in the protected server .env; do not paste it into chat, GitHub,
   screenshots or logs. Do not use Playground's shared default client credentials.
6. For long-lived production use, configure the OAuth application's publishing
   status and complete any Google verification required for its users/scopes.
   Testing-mode authorisations and revoked/expired tokens are not a reliable
   production credential. Reauthorisation may still be required by Google.

## Backend environment

See `docs/google-meet.env.example`. Set these in the production .env:

```dotenv
GOOGLE_CALENDAR_CLIENT_ID=YOUR_WEB_OAUTH_CLIENT_ID
GOOGLE_CALENDAR_CLIENT_SECRET=YOUR_WEB_OAUTH_CLIENT_SECRET
GOOGLE_CALENDAR_PLATFORM_ENABLED=true
GOOGLE_CALENDAR_PLATFORM_ACCOUNT_EMAIL=YOUR_DEDICATED_SHVYA_GOOGLE_EMAIL
GOOGLE_CALENDAR_PLATFORM_REFRESH_TOKEN=YOUR_OFFLINE_REFRESH_TOKEN
GOOGLE_CALENDAR_PLATFORM_CALENDAR_ID=YOUR_DEDICATED_SHVYA_GOOGLE_EMAIL
```

The calendar ID can instead be a dedicated secondary calendar on which this
account has write access and Google Meet support. Do not make that calendar
public or share calendar-wide access with tenants. Empty/primary resolves to the
explicit platform account email. The server verifies the refreshed token's
Google email against ACCOUNT_EMAIL before using it. Access tokens are cached
only in process memory; the diagnostic output never prints tokens or secrets.

The new PLATFORM_* variables can be Django settings or server .env values via
python-decouple. The existing CLIENT_ID/CLIENT_SECRET remain the shared SHVYA
OAuth application settings. Supply the same .env to web and every Python worker.

## Validate and activate

From the production checkout after deploying this code, validate new .env values
in a fresh one-off container before replacing live workers:

```bash
cd /opt/shvya-ai
docker compose run --rm --no-deps web python manage.py check_google_calendar --live
```

This command only refreshes authorisation and reads account/calendar metadata.
It creates no leads, bookings, events or notifications. It fails clearly if the
Google owner differs, calendar access is not writable, or Meet is unsupported.

After a successful check, recreate the Python services so they load the new env:

```bash
docker compose up -d --no-deps --force-recreate web ws worker ai_realtime_worker hosted_ai_worker campaign_worker ingestion_worker automation_worker beat
```

No staging restart is required. Existing future `not_connected` Meet bookings
without Google events are picked up in bounded batches by the recovery task after
activation. FAILED bookings are not indiscriminately replayed: inspect the saved
error and retry the specific booking after resolving it. Keep the normal Celery
workers and Beat running; manual attribute edits enqueue Google sync after commit.

## Verification and limits

Automated tests in `test_platform_google.py` use an isolated test database and a
SIMULATED Google provider. They cover manual/public mapping, rescheduling, unique
identities/links, crash recovery, private metadata, provider stickiness, disabled
invitations, exact-event cleanup and credential owner checks. Passing these tests
is not proof that a real Google account has been connected or a live meeting
created. Run the live read-only check after supplying real credentials.

In platform mode SHVYA's Google account, NOT the tenant, is the Google organiser.
Room admission, organiser privileges, simultaneous-meeting limits and duration
remain subject to that account's Google plan/policies. Link generation does not
make tenant staff co-hosts or bypass Google's waiting room. Organisations needing
independent host control should connect their own Google accounts. Never change
meeting access to public automatically. Google quotas also apply; provider 429/
transient failures use bounded retries rather than fabricated links.

References:
- https://developers.google.com/identity/protocols/oauth2/web-server
- https://developers.google.com/workspace/calendar/api/v3/reference/events/insert
- https://developers.google.com/workspace/calendar/api/v3/reference/events
- https://developers.google.com/workspace/guides/create-credentials
