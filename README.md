# Portfolio Dashboard

Self-hosted personal portfolio dashboard (Flask, SQLite, SQLAlchemy, Alembic) for a
Raspberry Pi. See [copilot_instructions.md](copilot_instructions.md) for the full specification.

## Setup

Dependencies are managed with Poetry and installed into the `.py313` environment:

```bash
source .py313/bin/activate
poetry install --no-root
```

## Database

```bash
python -m app.database.manage upgrade   # create or migrate data/portfolio.db
python -m app.database.manage seed      # regions, assets, settings, baseline retirement plan
python -m app.database.manage backup
python -m app.database.manage restore PATH
```

Environment variables: `PORTFOLIO_DB_PATH`, `PORTFOLIO_BACKUP_DIR`, `PORTFOLIO_TIMEZONE`.

## Moomoo integration (read-only)

The app reads accounts, positions, cash balances, executed fills (with order fees) and
cash flows through the official Moomoo API (`moomoo-api` SDK). It never places, modifies
or cancels orders and never unlocks trading: only query calls are reachable in
`app/integrations/moomoo/client.py`.

### How authentication works

Moomoo's API goes through **OpenD**, a gateway program from Moomoo. You log OpenD in to
your moomoo ID; the app only connects to OpenD over TCP and never handles your Moomoo
password. Before the first use, accept the API disclaimer for Moomoo AU
(<https://risk-disclosure.au.moomoo.com/index?agreementNo=AUOT0025>) and restart OpenD.

1. Download OpenD from <https://www.moomoo.com/download/OpenAPI> and log in.
2. Keep the API port at `11111` (or set `MOOMOO_OPEND_PORT`).
3. OpenD is published for Windows, macOS, CentOS and Ubuntu. If it can't run on the
   Raspberry Pi, run it on another machine and set `MOOMOO_OPEND_HOST`; in that case
   enable RSA protocol encryption in OpenD and set `MOOMOO_RSA_KEY_PATH` to the same key.

| Variable | Default | Purpose |
|---|---|---|
| `MOOMOO_OPEND_HOST` | `127.0.0.1` | OpenD host |
| `MOOMOO_OPEND_PORT` | `11111` | OpenD API port |
| `MOOMOO_SECURITY_FIRM` | `FUTUAU` | Broker entity (Moomoo AU) |
| `MOOMOO_TRADING_ENV` | `REAL` | `REAL` or `SIMULATE` (paper trading) |
| `MOOMOO_RSA_KEY_PATH` | – | RSA private key for remote OpenD |
| `MOOMOO_CONNECT_TIMEOUT` | `15` | Seconds to wait for OpenD |

### Commands

```bash
python -m app.integrations.moomoo probe      # read-only check of what the API returns; writes nothing
python -m app.integrations.moomoo sync       # import into the database, then validate holdings
python -m app.integrations.moomoo validate   # compare synced positions with imported fills
```

Run `probe` first. It confirms which markets your account returns (ASX trading is not
supported by the Moomoo API, so ASX positions and fills must be confirmed) and lists the
cash flow types, which are needed to classify deposits and dividends.

### What a sync does

- Each step (accounts, positions, balances, fills, cash flows) runs in its own database
  transaction and is recorded in `sync_history` (success, partial or failed).
- Fills and cash flows are de-duplicated on Moomoo's identifiers, so re-running is safe.
  The first run imports from `moomoo.history_start` (app setting, default `2025-01-01`);
  later runs overlap the previous run by a few days.
- Cash flows are fetched one day per request (Moomoo limit: 20 requests / 30 s), so the
  first backfill takes several minutes. Progress is committed every 30 days.
- Broker codes are matched to assets through `assets.broker_symbol` (e.g. `AU.IOZ`).
  Unknown codes are reported, never guessed: add the asset and re-sync.
- Positions are replaced on each sync; balances are appended (history kept). Imported
  fills are never changed: a fill cancelled after import is reported for manual review.
- Validation compares broker quantities with quantities implied by imported fills.
  Holdings bought before `moomoo.history_start` show as differences until opening
  positions are recorded.

## Tests

```bash
python -m pytest -q
```

Tests use a temporary database and fake Moomoo responses; they never contact a broker.
