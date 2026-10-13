# SHVYA AI deployment environments

> **Implementation baseline:** refreshed on 2026-10-12 against `main` commit `24f2a1d5f75d`. Runtime workflow files and server configuration remain authoritative.

## Source branches and environments

| Source | Environment | Compose / workflow | Status |
| --- | --- | --- | --- |
| `main` | Production | `docker-compose.yml` + `deploy.yml` | Current automated release path |
| `main` validated SHA | Secondary production target | `deploy-main-secondary.yml` | Optional configured production deployment |
| Focused feature/fix/docs branch | Development / PR review | CI on PR to `main` | Current change-development path |
| Explicit/manual non-production checkout | Isolated validation stack | `docker-compose.staging.yml` | Available stack definition; not an automatic branch promotion path |

The current `main` tree has no `deploy-staging.yml` workflow. Any separately operated staging host must therefore be treated as an explicitly managed non-production environment, not as an assumed GitHub promotion branch.

## Environment isolation

Production and any non-production stack must keep separate:

- PostgreSQL databases and backups
- Redis/Celery/channel-layer state
- WhatsApp linked-device session volumes
- media/static state where mutable
- Django/OAuth/JWT/provider secrets
- webhook tokens and callback registrations
- Meta, OpenAI, SMTP, payment and marketplace test credentials

Never point a test stack at production PostgreSQL, Redis or live WhatsApp/Instagram credentials. If production data is copied for debugging, anonymize personal/customer data first.

## Production deployment behavior

`deploy.yml` and the secondary deployment workflow accept only a SHA associated with the validated `main` release path. The primary deployment performs its configured backup/migration/readiness sequence and must keep schema-dependent web/workers on a compatible release.

Health endpoints:

- `/health/live/` — Django process liveness.
- `/health/ready/` — database/Redis readiness.

## Optional staging Compose

`docker-compose.staging.yml` remains useful for isolated parity checks and manual validation. If it is operated:

- use `.env.staging` derived from `.env.staging.example`, never production `.env`;
- retain outbound-message guards and explicit test recipients;
- keep its Compose project, ports, database, Redis, sessions and media distinct;
- keep public access protected and do not expose internal Hosted WhatsApp gateway ports;
- document the actual host/DNS/deployment owner separately because the repository no longer guarantees an automatic staging deploy.

## GitHub repository controls

Recommended controls for the current model:

- require pull requests for `main`;
- require CI and Security checks before merge;
- block force pushes/deletion of `main`;
- protect the GitHub production Environment and deployment secrets;
- keep VPS credentials and provider secrets in GitHub/host secret stores, never repository files.

## Rollback

1. Prefer a revert PR into `main`.
2. Confirm schema compatibility before deploying the older application revision.
3. Restore the matching pre-deploy database backup only when a non-backward-compatible migration makes application-only rollback unsafe.
4. Bring web, workers and Beat back on one compatible release after the schema is ready.
