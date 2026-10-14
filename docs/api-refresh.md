# API Operations: safe opt-in refresh

Superadmin -> API provides an optional 60-second page refresh. It is off by
default. Only the on/off preference is saved in sessionStorage; filters and
customer content are not copied into browser storage. The existing URL is
reloaded unchanged, including organisation, range, search, outcome and page.
This is not a WebSocket stream, provider health check or automatic retry.

A responsive control bar reports whether refresh is off, scheduled, paused
for editing, paused for unapplied changes, hidden, offline or navigating.
Automatic reload is blocked when any named editable field in the API workspace
has changed from its server-rendered default, including after focus leaves it
and when browsers restore earlier edits. Script-only changes are rechecked at
the timer boundary. Applying filters loads a fresh server-rendered view; resetting
or reverting edits allows the timer to resume. Hidden tabs and offline windows
do not keep a refresh timer running. Returning to a visible online view starts
a fresh interval; timer firing remains subject to browser scheduling.

Manual Refresh asks before discarding unapplied changes. Detail and investigation
pages without the opt-in control never automatically reload. A pagehide cancels
the pending timer; pageshow restores scheduling after Back/Forward navigation.
No provider call, message resend, API outcome mutation, schema migration or new
permission is added by these UI changes. Existing Superadmin access applies.

## Regression checks

Run the dependency-free JavaScript regressions directly:

```sh
node --test apps/superadmin/tests/js/api-operations.test.cjs
```

The existing Superadmin pytest target runs the same Node tests through
`test_api_refresh.py` and verifies the real Django page renders one accessible
control and the refresh stylesheet. Node.js is required, not silently skipped.
Browser-fixture checks can validate focus, dirty values and responsive controls,
but they do not establish successful authenticated production access or real
provider delivery. No raw request bodies, messages or credentials are required.
