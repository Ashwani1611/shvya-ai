# SHVYA AI

SHVYA is a multi-tenant SaaS CRM. AI and automation operate on top of the CRM as the
source of truth — never the other way around.

## Stack

- **Backend:** Python, Django, Django REST Framework
- **Database:** PostgreSQL (+ pgvector from Phase 4 onward)
- **Queue:** Redis + Celery
- **Auth:** JWT (SimpleJWT) for app users, org-scoped API keys for server-to-server access
- **Frontend (V1):** Django Templates + HTMX + JavaScript + Tailwind
- **Architecture:** Multi-tenant, service layer, event-driven, queue-first

See [`CLAUDE.md`](./CLAUDE.md) for the full architecture rules (tenant isolation,
service-layer placement, idempotency, migration conventions, etc.) that any change to
this codebase must follow.

## Project layout

```
apps/            Django apps: accounts, organizations, teams, superadmin, crm,
                  followups, analytics, triggers, calls, telephony, channels,
                  integrations, copilot, knowledge
api/v1/           Versioned API routes
config/           Django project settings, urls, celery, asgi/wsgi
core/             Shared utilities, permissions, pagination, exceptions
services/         Business logic layer (ai, crm, triggers, knowledge, telephony,
                  analytics, notifications) — see CLAUDE.md rule 2
templates/        Django templates (crm dashboard, superadmin console, admin)
scripts/          One-off/maintenance scripts (seeding, embeddings backfill)
tests/            Test suite (pytest)
```

Note: some apps (`accounts`, `superadmin`) are still mid-refactor from flat
`views_flat.py` files to package-style `views/`, `urls/`, `models/`, `serializers/`
— check the actual app directory before assuming a layout.

## Local setup

### Prerequisites
- Python 3.13
- PostgreSQL 15+
- Redis

```bash
# macOS
brew install postgresql@18 redis
brew services start postgresql@18
brew services start redis
```

### Clone and install

```bash
git clone https://github.com/Ashwani1611/shvya-ai.git
cd shvya-ai
python -m venv venv
source venv/bin/activate   # venv\Scripts\activate on Windows
pip install -r requirements-dev.txt
```

### Environment variables

```bash
cp .env.example .env
```

Then fill in `.env`:
- `SECRET_KEY` — any random string for local dev
- `DB_NAME`, `DB_USER`, `DB_PASSWORD` — match your local Postgres user
- `DB_HOST=127.0.0.1`, `DB_PORT=5432`

If your DB password contains a `#`, wrap it in quotes: `DB_PASSWORD='mypass#123'`.
If your shell already has `DB_*` env vars exported, they'll silently override `.env`
— check with `env | grep DB_`.

### Database and server

```bash
psql -U postgres -c "CREATE DATABASE shvya_ai;"
python manage.py migrate
python manage.py createsuperuser   # role is forced to `superadmin` automatically
python manage.py runserver
```

- CRM dashboard login: `http://127.0.0.1:8000/dashboard/login/`
- Superadmin console: `http://127.0.0.1:8000/superadmin/login/`
- Django admin: `http://127.0.0.1:8000/admin/`

A freshly created superuser has no organization, so it lands you in the superadmin
console — from there, create an organization and an org `admin` user to get into the
CRM dashboard.

### Docker (alternative)

```bash
docker compose up
```
Runs Postgres, Redis, and the web app together, applying migrations on startup.

### Tests

```bash
pytest --cov
```

### Branch workflow

`main` is protected — no direct pushes.

```bash
git checkout main
git pull
git checkout -b feature/<short-description>
# ...work...
git add .
git commit -m "feat: <what you did>"
git push -u origin feature/<short-description>
```
Then open a pull request into `main` and wait for review.

Branch prefixes: `feature/...`, `fix/...`, `chore/...`

A pre-commit hook runs `ruff` to catch undefined names and unused imports:
```bash
pre-commit install
```

## Troubleshooting

- **`psql: FATAL: password authentication failed`** — check `.env` matches your
  actual local Postgres password, and check for stale shell `DB_*` env vars.
- **Redis `Connection refused`** — make sure `redis-server` is running
  (`redis-cli ping` should return `PONG`).
- **`NameError` / `AttributeError` in the browser** — run
  `ruff check apps --select F821` to catch undefined names before it reaches the browser.