# SHVYA Call Intelligence — Android companion

Internal Android client for SHVYA employees. It is distributed from the authenticated SHVYA dashboard rather than Google Play.

## Architecture

- Native Android phone-state receiver.
- `READ_CALL_LOG` reconciliation for incoming, outgoing, missed and rejected SIM calls.
- Room local database for durable call evidence.
- Room outbox with idempotent event UUIDs.
- WorkManager network sync and periodic reconciliation.
- JWT login through SHVYA's existing `/api/v1/auth/token/` contract.
- Refresh-token rotation through `/api/v1/auth/token/refresh/`.
- Android Keystore AES-GCM storage for access and refresh tokens.
- Server-authoritative organization, user, pipeline and lead matching.

## Build

Requirements: JDK 17, Android SDK 36, Android Build Tools 36 and Gradle 9.4.1.

```bash
gradle :app:assembleRelease -PSHVYA_BASE_URL=https://dashboard.shvya-ai.com/
```

For staging:

```bash
gradle :app:assembleDebug -PSHVYA_BASE_URL=https://staging.shvya-ai.com/
```

Sign the internal release APK and deploy it to `CALL_INTELLIGENCE_APK_PATH`, or host it at the private URL configured by `CALL_INTELLIGENCE_APK_URL`.

## Required permissions

Phone state, Call Log, Phone Numbers, Contacts, Notifications on Android 13+, and ignore battery optimizations (recommended).

The client never sends an organization, pipeline, lead or user identifier as authoritative call ownership. The authenticated SHVYA backend resolves those relationships.
