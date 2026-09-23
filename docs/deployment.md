# SHVYA deployment runbook

> **Implementation snapshot:** verified on 2026-09-23 against staging runtime commit `84013a4190cfa97644e0216a896fa4ecc59eaebd`. Source code, Django models/migrations, tests, and runtime configuration remain the executable source of truth.

This is the concise release runbook. Environment-specific detail is in [`deployment_environments.md`](./deployment_environments.md).

## Environment map

| Environment | Branch | Compose | Checkout | URL |
| --- | --- | --- | --- | --- |
| Staging | `staging` | `docker-compose.staging.yml` | `/opt/shvya-ai-staging` | `https://staging.shvya-ai.com` |
| Production | `main` | `docker-compose.yml` | `/opt/shvya-ai` | `https://dashboard.shvya-ai.com` |

Staging and production must keep separate environment files, databases, Redis data, sessions, media/static state and provider/test credentials.

## Normal release path

```text
feature / fix / docs branch
          ↓
       staging
          ↓
 CI + staging deployment
          ↓
 manual/runtime verification
          ↓
        main
          ↓
 CI + production deployment
```

Do not assume that all staging commits should be promoted together. Compare the staging → main diff before production promotion and keep the production PR focused on the verified change set.

## Required validation

Before promotion:

```bash
ruff check .
python manage.py makemigrations --check --dry-run --settings=config.settings.testing
python manage.py check --settings=config.settings.testing
pytest --cov

docker compose --env-file .env.example -f docker-compose.yml config --quiet
docker compose --env-file .env.staging.example -f docker-compose.staging.yml config --quiet
```

Repository CI also validates the relevant Docker builds after code-quality/test gates.

## Deployment checks

1. Confirm the intended branch and exact commit SHA.
2. Confirm migrations are reviewed and compatible with the deployed code.
3. Verify liveness and readiness endpoints.
4. Verify background workers/Beat and ASGI/WebSocket services when touched.
5. Verify provider callbacks/integrations only with the correct environment credentials.
6. Check application logs without exposing secrets.
7. For data/config changes, verify post-write state rather than relying only on a successful request.

Health endpoints:

- `/health/live/`
- `/health/ready/`

## Rollback principle

Rollback must preserve data integrity. Revert application code to a previously verified commit only when its schema remains compatible. Do not manually fake migrations or point staging at production data to make a rollback appear successful.

## Documentation-only changes

Documentation-only commits still land on `staging` first. They must not modify runtime code, workflows, Compose/YAML or infrastructure unless that change is explicitly part of the task. Before merging documentation to `main`, verify that the documented staging behavior is the behavior intended for production.
