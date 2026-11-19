# Contributing

This guide covers setting up a development environment. To install and configure the
application itself, see [README.md](README.md). The project specification, coding standards
and milestones are in [copilot_instructions.md](copilot_instructions.md); read its sections
13 and 14 before changing financial logic.

## Development environment

Requirements: Ubuntu (or another Linux), Python 3.10 or newer (the project is developed on
3.14) and [Poetry](https://python-poetry.org/) 2.x.

```bash
sudo apt install python3-venv python3-pip pipx
pipx install poetry
git clone git@github.com:rhenancardoso/portfolio-dashboard.git
cd portfolio-dashboard
python3 -m venv .py314
source .py314/bin/activate
poetry install --no-root
```

Poetry is configured with `virtualenvs.create = false` (`poetry.toml`), so it installs into
the environment you have activated. The `.py*` folders are git-ignored. `poetry install`
includes the `dev` group (pytest); the README uses `--only main` for production.

After changing dependencies in `pyproject.toml`, run `poetry lock` and commit `poetry.lock`.

Create a local database and run the app:

```bash
python -m app.database.manage upgrade
python -m app.database.manage seed
gunicorn --bind 127.0.0.1:8000 "app.main:create_app()"
curl http://127.0.0.1:8000/health
```

Configuration is read from environment variables (see the README). The database defaults to
`data/portfolio.db`, which is git-ignored along with `.env`, keys and backups. Never commit
credentials or tokens.

## Tests

```bash
python -m pytest -q
```

Tests use a temporary database and fake provider and Moomoo responses. They must never call a
live brokerage account or market API. Add tests with every change: unit tests for financial
calculations, plus tests for duplicate transactions, failed API requests, stale or missing
prices, currency conversion and month-end snapshots where relevant.

## Project layout

| Path | Contents |
|---|---|
| `app/main.py` | Flask application factory (`create_app`) |
| `app/api/` | Flask routes; keep business logic out of them |
| `app/services/` | Portfolio, transaction, dividend, forecast, snapshot and sync logic |
| `app/integrations/moomoo/` | Read-only Moomoo OpenD client |
| `app/integrations/market_data/` | Price and FX providers behind a common base class |
| `app/database/` | SQLAlchemy models, engine and sessions, Alembic migrations, seed data |
| `app/scheduler/` | Jobs run by the systemd timer |
| `deploy/systemd/` | Service and timer unit files |
| `tests/` | Pytest suite |

## Database changes

Schema changes need an Alembic migration; never edit an applied migration.

```bash
alembic revision --autogenerate -m "describe the change"
python -m app.database.manage upgrade
```

Review the generated file in `app/database/migrations/versions/` before committing. Use
`Decimal` for money, update records inside `session_scope` transactions, and never silently
modify historical records or snapshots.

## Conventions

- PEP 8, type hints and docstrings on public classes and functions.
- Python `Decimal` for all monetary values; keep the currency with the amount.
- External API calls need timeouts, error handling, retry with backoff and rate-limit
  respect. Never assume a price is real-time.
- Never add trading functionality: Moomoo access stays read-only.
- Do not add a dependency unless it clearly earns its place; no paid dependencies without
  approval.
- Keep the app lightweight and modular. Develop incrementally: each milestone must leave the
  application working and testable.

## Git

- Work on a feature branch (for example `feature/add-market-collection`) and open a pull
  request against `main`.
- Write meaningful commit messages and keep `.gitignore` up to date.
- Document architectural changes.
