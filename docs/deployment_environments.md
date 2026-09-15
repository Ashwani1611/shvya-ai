# SHVYA AI deployment environments

SHVYA AI uses two long-lived environments and short-lived feature/fix branches.

## Branches and environments

| Branch | Environment | URL | Server directory |
| --- | --- | --- | --- |
| `main` | Production | `https://dashboard.shvya-ai.com` | `/opt/shvya-ai` |
| `staging` | Staging / pre-production | `https://staging.shvya-ai.com` | `/opt/shvya-ai-staging` |
| `feature/*`, `fix/*` | Development | local / PR CI | developer machine |

Production and staging run separate PostgreSQL, Redis, Celery, media, static,
internal Nginx, and WhatsApp session storage. They share only the VPS and the
public TLS edge. The public Nginx container is attached to the staging Docker
network only so it can proxy to the isolated staging Nginx container.

## Normal release flow

1. Create `feature/<name>` or `fix/<name>` from `staging`.
2. Open a pull request into `staging`.
3. CI must pass.
4. Merge to `staging`.
5. `Deploy Staging` automatically deploys `/opt/shvya-ai-staging`.
6. Verify the feature at `staging.shvya-ai.com` using staging/test credentials.
7. Open a pull request from `staging` into `main`.
8. CI must pass again.
9. Merge to `main`.
10. `Deploy` backs up PostgreSQL, migrates, updates services, performs readiness
    checks, and deploys production.

Do not merge feature branches directly to `main` except for a documented
emergency hotfix.

## Environment files

Production keeps `/opt/shvya-ai/.env` and staging keeps
`/opt/shvya-ai-staging/.env.staging`. Both files are ignored by Git.

The staging deploy creates `.env.staging` automatically on first deploy from
`.env.staging.example` and generates independent Django, database, webhook,
gateway, and Basic Auth secrets. Meta, OpenAI, SMTP, payment, and other
third-party credentials must use sandbox/test accounts in staging when those
integrations are tested.

Never point staging at the production PostgreSQL database, production Redis, or
production WhatsApp/Instagram credentials.

## Staging access and outbound safety

The staging website is protected by HTTP Basic Auth in its private Nginx layer.
The deployment generates a random password on first deploy and keeps the plain
credential only in `/opt/shvya-ai-staging/.env.staging`. The generated
`.staging.htpasswd` contains only the password hash and is ignored by Git.

To retrieve the generated login while connected to the VPS:

```bash
sudo grep -E '^STAGING_BASIC_AUTH_(USER|PASSWORD)=' /opt/shvya-ai-staging/.env.staging
```

WhatsApp API, Hosted WhatsApp, and Instagram outbound transports have an
additional application-level guard. In staging they are blocked by default.
Outbound testing requires both:

```env
OUTBOUND_MESSAGING_ENABLED=True
STAGING_ALLOWED_RECIPIENTS=919999999999,17841400000000000
```

Only explicitly allowlisted test recipients can be sent to in staging.
Production is not affected by this staging-only guard.

## Database safety

Production deployment creates a timestamped `pg_dump` before migrations and
keeps 14 days of deployment backups in `/opt/shvya-ai/backups`.

Staging migrations only run against the isolated `shvya-staging` Compose
project database volume.

If production data is ever copied to staging, anonymize personal/customer data
before it becomes available to staging users.

## Health checks

- `/health/live/` checks that the Django process can respond.
- `/health/ready/` checks both PostgreSQL and Redis.

Deployments use readiness checks before reporting success. Staging health probes
are exempt from Basic Auth but expose only service health, not application data.

## DNS and TLS

Create an `A` record for `staging.shvya-ai.com` pointing to the same VPS as the
production dashboard. Before TLS exists, the public edge exposes only the ACME
challenge and returns HTTP 503 for application traffic so Basic Auth credentials
are never sent over plain HTTP. The staging deploy then provisions a Let's
Encrypt certificate, redirects HTTP to HTTPS, adds
`X-Robots-Tag: noindex, nofollow, noarchive`, and proxies HTTPS traffic to the
private staging Nginx container. If DNS is not ready, the isolated Docker stack
still deploys locally and TLS provisioning can be retried by rerunning the
staging deploy.

## GitHub repository settings

Recommended repository controls:

- Require pull requests for `main` and `staging`.
- Require the `CI` checks before merge.
- Block force pushes and branch deletion for both long-lived branches.
- Configure the GitHub `production` Environment with required approval.
- `staging` may deploy automatically after CI.
- Keep VPS credentials in GitHub Actions secrets, never in repository files.

The workflows reference GitHub Environments named `staging` and `production`.
Environment protection rules are configured in GitHub repository settings.

## Rollback

Application rollback:

1. Revert the bad PR or reset `staging` to a known good commit for staging.
2. For production, create a revert PR into `main` and deploy it through CI.
3. If a migration is not backward compatible, restore the matching pre-deploy
   database backup before bringing the previous application version back.

Prefer backward-compatible, expand/contract database migrations so ordinary
application rollbacks do not require a database restore.
