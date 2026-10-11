# Android download publication

The production deploy runs `publish_android_release` after collectstatic and public
HTTPS readiness. It hashes both the checked-in APK and the publicly served bytes
before activating the canonical `CallAppRelease` record used by Call Intelligence's
Download for Android button. Failed verification leaves the existing release active.
Publication is idempotent, preserves release history, refuses conflicting checksums
for the same version code, and does not downgrade a newer active release.

The bundled v1.6 Preview (version code 10) is the previously verified APK from the
Android authentication/UI update. SHA-256:
`a9e271d9637214db2a91048f56691ebb8cb3ffc3b9c58653c1823e06007c2437`.
It uses package `com.shvya.callintelligence.preview` and installs alongside v1.5
because the original signing key was unavailable. It is not a mandatory in-place
update. Its installation note appears next to the dashboard download button.

To publish a subsequent release, commit the signed binary under a new immutable
`static/downloads/` filename, update `apps/telephony/releases.py`, and promote through
the normal CI-gated production deploy. Never overwrite an existing APK filename.
