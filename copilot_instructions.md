# Personal Financial Portfolio Dashboard
## Project Overview & GitHub Copilot Development Instructions

## 1. Project Overview

Develop a self-hosted personal financial portfolio management application running on an Intel NUC.

The application collects financial market data from external APIs, synchronises investment transactions and holdings from a Moomoo brokerage account, manages manually maintained cash assets, calculates portfolio performance, tracks dividends and provides financial forecasting.

The system replaces an existing Apple Numbers spreadsheet currently used to monitor investments and portfolio performance.

The application must provide a responsive web dashboard accessible from computers, tablets and mobile devices, both locally and remotely.

The primary reporting currency is AUD (Australian Dollar).

### Main objectives

1. Automatically collect current market prices for shares, ETFs and Bitcoin.
2. Synchronise investment transactions and holdings through the Moomoo API.
3. Automatically retrieve and track dividend information.
4. Support manually maintained cash and other non-broker assets.
5. Calculate portfolio valuation, investment costs and profit/loss.
6. Automatically record historical portfolio values.
7. Compare actual portfolio performance against monthly forecasts.
8. Provide configurable market data collection intervals.
9. Provide secure remote access to the dashboard.
10. Minimise recurring operational costs by prioritising free data providers.

---

## 2. Hardware and Operating Environment

### Hardware

- Intel NUC
- Intel Core i5-5300U, dual-core / 4 threads, 2.3 GHz
- 16 GB RAM
- Ethernet
- 256 GB SSD

### Operating system

Ubuntu Server 26.04 LTS (x86_64).

### Runtime

- Python 3
- Linux systemd for service management and scheduling
- SQLite for persistent storage

The application must be compatible with the NUC's x86_64 architecture.

Avoid unnecessarily resource-intensive libraries and services.

Docker is not required. Prefer running Python applications directly as Linux services.

---

## 3. Technology Stack

| Component | Technology |
|---|---|
| Programming language | Python 3 |
| Backend framework | Flask |
| Database | SQLite |
| Database access | SQLAlchemy or lightweight repository layer |
| Database migrations | Alembic |
| Frontend | HTML5, CSS3, JavaScript |
| Charts | Chart.js |
| API communication | Requests |
| Data processing | Standard Python |
| Scheduling | systemd timers / scheduled Python jobs |
| Testing | Pytest |
| Logging | Python logging |
| Deployment | Ubuntu Server (systemd services) |

Keep the application lightweight and modular.

Do not introduce additional frameworks or dependencies unless they provide a clear benefit.

---

## 4. Portfolio Composition

The initial portfolio contains the following assets:

| Symbol | Asset | Market | Currency |
|---|---|---|---|
| BTC-AUD | Bitcoin | Cryptocurrency | AUD |
| AAPL | Apple | US | USD |
| IOZ.AX | iShares Core S&P/ASX 200 ETF | ASX | AUD |
| NDQ.AX | BetaShares Nasdaq 100 ETF | ASX | AUD |
| VEQ.AX | Vanguard FTSE Europe ETF | ASX | AUD |
| SHL.AX | Sonic Healthcare | ASX | AUD |
| VAP.AX | Vanguard Australian Property ETF | ASX | AUD |

The portfolio also contains cash balances and potentially other manually maintained assets.

The initial holdings and average purchase prices should be migrated from the existing Numbers spreadsheet.

Do not hardcode quantities, purchase prices or cash balances into the application. All portfolio information must be stored in the database.

The asset list must be configurable so that new shares, ETFs and cryptocurrencies can be added without modifying application code.

---

## 5. System Architecture

The application must be separated into independent modules.

### 5.1 Market Data Collectors

Responsible for retrieving market prices from external providers.

Required integrations:

- Moomoo market data API, subject to account permissions and market entitlements.
- CoinGecko API for Bitcoin prices.
- An ASX-compatible market data provider.
- An FX provider for currency conversion, particularly USD/AUD.

Implement a provider abstraction so that market data providers can be replaced without modifying portfolio calculation logic.

Every market price record must contain:

- Asset symbol.
- Price.
- Currency.
- Provider.
- Market timestamp, when supplied.
- Retrieval timestamp.
- Data status.

The application must distinguish between the time a price was published and the time it was retrieved.

Never assume that a retrieved price is real-time.

If an API request fails, retain the last valid price and flag it as stale.

### 5.2 Moomoo Integration

Moomoo is the primary brokerage integration.

Use the official Moomoo OpenAPI documentation:

https://open.moomoo.com/api/overview

The application must use read-only access.

Required functionality:

- Authenticate with the Moomoo API using its supported authorisation mechanism.
- Retrieve available trading accounts.
- Retrieve current account positions.
- Retrieve account balances.
- Retrieve historical executed transactions.
- Retrieve cash movements.
- Retrieve dividend-related information where available.

Do not request trading permissions.

The application must never place, modify or cancel orders.

Important implementation constraints:

- Verify API availability for the user's Moomoo region and account.
- Verify Australian account support and available API endpoints.
- Verify authentication requirements before implementation.
- Do not assume every API endpoint is available under every account or market entitlement.
- Handle token expiration and reauthentication.
- Never expose access tokens, refresh tokens or API credentials to the frontend.
- Store sensitive credentials outside source control.

Use executed transactions (fills), not submitted orders, when calculating holdings.

Implement transaction synchronisation using unique broker transaction identifiers to prevent duplicate records.

The broker's holdings should be authoritative only for assets held in that brokerage account.

Assets held elsewhere must remain independently manageable.

### 5.3 Portfolio Calculation Engine

Responsible for all portfolio calculations.

Calculations must be independent of the data providers and frontend.

Required calculations:

- Total portfolio value.
- Total investment cost.
- Current market value per asset.
- Unrealised profit/loss.
- Realised profit/loss.
- Percentage return.
- Asset allocation.
- Geographic allocation.
- Cash allocation.
- Dividend income.
- Portfolio performance over time.

All reporting calculations must use AUD.

Retain the original transaction currency and market currency.

For foreign currency assets, apply the appropriate USD/AUD exchange rate.

Use weighted-average cost accounting initially, consistent with the existing Numbers spreadsheet.

Transaction fees must be included in acquisition costs and disposal calculations.

Keep realised and unrealised gains separate.

Deposits and withdrawals must not be treated as investment profits.

### 5.4 Dividend Management

Implement automatic dividend tracking using broker cash movements and available dividend information APIs.

Distinguish between:

1. Announced dividends.
2. Expected dividends.
3. Confirmed dividend payments.
4. Dividend withholding tax.

Only confirmed cash credits should be included in actual dividend income.

Expected dividends must be displayed separately.

Support:
- Dividend payment history.
- Dividend income by asset.
- Monthly dividend income.
- Annual dividend income.
- Dividend yield.
- Expected future dividend payments.

If dividend information cannot be retrieved automatically, flag it for manual reconciliation.

Do not invent missing dividend information.

### 5.5 Forecasting Engine

Replicate the existing forecasting functionality from the Numbers spreadsheet.

Required configurable parameters:

- Monthly cash deposits.
- Monthly investment deposits.
- Expected annual portfolio growth.
- Expected dividend yield.
- Forecast period.
- Starting portfolio value.

Generate monthly predicted portfolio values.

Support three scenarios:

- Conservative.
- Expected.
- Optimistic.

Allow forecasting assumptions to be modified without changing source code.

The forecasting engine must be independent of the dashboard.

---

## 6. Database Design

Use SQLite as the persistent database.

Suggested tables:

| Table | Purpose |
|---|---|
| assets | Asset definitions and configuration |
| broker_accounts | Brokerage account information |
| transactions | Purchases, sales and executed fills |
| positions | Latest synchronised broker holdings |
| market_prices | Current and historical prices |
| exchange_rates | Currency exchange rates |
| cash_movements | Deposits, withdrawals and dividends |
| dividends | Announced and received dividends |
| portfolio_snapshots | Historical portfolio valuations |
| forecast_settings | Forecast assumptions |
| portfolio_forecasts | Monthly predicted values |
| app_settings | Application configuration |
| sync_history | Broker synchronisation records |
| collection_logs | Market data collection status |

### Database requirements

- Use unique identifiers and appropriate database constraints.
- Use indexes for frequently queried fields.
- Use database transactions for financial data updates.
- Prevent duplicate broker transactions.
- Maintain historical records.
- Implement database migrations.
- Use appropriate decimal precision for financial calculations.
- Never use binary floating-point arithmetic for persisted monetary amounts where exact decimal representation is required.
- Store timestamps consistently in UTC and convert to Australia/Melbourne for presentation.

Portfolio snapshots must be immutable after creation, except through an explicit administrative correction process that retains an audit trail.

---

## 7. Price Collection and Scheduling

Market data update frequency must be configurable through the web dashboard.

Initial recommended configuration:

| Data | Update frequency |
|---|---|
| Bitcoin | Every 3 hours |
| ASX shares | Every 3 hours during market hours |
| US shares | Every 3 hours during market hours |
| Exchange rates | Once daily |
| Moomoo transactions | Every 3 hours |
| Moomoo positions | Every 3 hours |
| Dividend information | Once daily |

Allow users to configure alternative intervals.

The scheduling system must:

- Support independent schedules for different data providers.
- Avoid unnecessary API calls.
- Respect API rate limits.
- Account for ASX and US market hours.
- Handle daylight saving time.
- Avoid duplicate jobs.
- Retry temporary failures with appropriate backoff.
- Record successful and failed collection attempts.

Bitcoin trades 24/7.

Stock market data collection should account for the relevant market's opening hours.

Use Linux systemd timers to trigger scheduled collection processes rather than requiring a permanently running heavy scheduler.

---

## 8. Historical Portfolio Snapshots

The application must automatically record portfolio history.

Implement two snapshot types:

### Daily snapshots

Record:
- Total portfolio value.
- Shares value.
- Bitcoin value.
- Cash value.
- Investment cost.
- Unrealised profit/loss.
- Realised profit/loss.
- Dividend income.

### Monthly snapshots

Record the official portfolio value at the end of each month.

The monthly snapshot must use the latest valid market prices for the relevant market close.

Account for weekends and market holidays.

The system must never modify an existing historical snapshot simply because current prices have changed.

Historical data is essential for comparing actual portfolio performance against the original forecast.

---

## 9. Web Application and Dashboard

Use Flask for the backend and HTML/CSS/JavaScript for the frontend.

Use Chart.js for interactive visualisations.

The frontend must be responsive and support desktop, tablet and mobile browsers.

Implement the following pages:

### 9.1 Portfolio Overview

Display:
- Total portfolio value.
- Total investment cost.
- Unrealised P&L.
- Realised P&L.
- Total dividend income.
- Asset allocation.
- Geographic allocation.
- Historical portfolio performance.

### 9.2 Holdings

Display individual holdings with:
- Symbol.
- Asset name.
- Quantity.
- Average purchase price.
- Current price.
- Market value.
- Investment cost.
- Profit/loss.
- Percentage return.
- Price timestamp.

### 9.3 Transactions

Display:
- Transaction history.
- Purchase and sale details.
- Transaction fees.
- Transaction currency.
- Broker synchronisation status.

### 9.4 Dividends

Display:
- Dividend payment history.
- Total dividends received.
- Monthly and annual income.
- Dividend income per asset.
- Expected future dividends.

### 9.5 Forecasting

Display:
- Actual versus predicted portfolio values.
- Monthly deposits.
- Expected investment growth.
- Conservative, expected and optimistic scenarios.
- Historical forecast accuracy.

### 9.6 Settings

Provide configuration for:
- Moomoo connection.
- Market data providers.
- Market data update intervals.
- Manual cash balances.
- Other non-broker assets.
- Monthly deposits.
- Forecast assumptions.
- Backup and restore.

---

## 10. Remote Access and Security

The dashboard must be accessible both locally and remotely.

Preferred solution: VPN access to the home network.

Tailscale or a router-supported WireGuard VPN can be evaluated.

Do not expose the Flask application directly to the public internet.

Security requirements:

- Password-protected dashboard.
- Secure handling of Moomoo credentials.
- No API credentials in frontend JavaScript.
- No secrets committed to Git.
- Environment variables or protected configuration files for secrets.
- HTTPS for remote access.
- Read-only Moomoo permissions.
- Database backups.
- Application logging.
- Input validation.
- Protection against unauthorised configuration changes.

The application must not have the ability to execute financial transactions.

---

## 11. Suggested Source Code Structure

Organise the project using separation of concerns.

```text
portfolio-dashboard/
│
├── app/
│   ├── main.py
│   ├── config.py
│   │
│   ├── api/
│   │   ├── dashboard.py
│   │   ├── portfolio.py
│   │   ├── assets.py
│   │   ├── transactions.py
│   │   ├── dividends.py
│   │   └── settings.py
│   │
│   ├── integrations/
│   │   ├── moomoo/
│   │   │   ├── authentication.py
│   │   │   ├── client.py
│   │   │   ├── accounts.py
│   │   │   ├── positions.py
│   │   │   ├── transactions.py
│   │   │   └── cash_logs.py
│   │   │
│   │   └── market_data/
│   │       ├── base_provider.py
│   │       ├── coingecko.py
│   │       ├── moomoo_quotes.py
│   │       ├── asx_provider.py
│   │       └── fx_provider.py
│   │
│   ├── services/
│   │   ├── portfolio_service.py
│   │   ├── transaction_service.py
│   │   ├── dividend_service.py
│   │   ├── forecast_service.py
│   │   └── snapshot_service.py
│   │
│   ├── database/
│   │   ├── db.py
│   │   ├── models.py
│   │   └── migrations/
│   │
│   └── scheduler/
│       ├── scheduler.py
│       └── jobs.py
│
├── frontend/
│   ├── templates/
│   ├── static/
│   │   ├── css/
│   │   ├── js/
│   │   └── images/
│   └── pages/
│
├── tests/
│
├── data/
│
├── requirements.txt
├── README.md
├── CONTRIBUTING.md
├── copilot_instructions.md
└── .gitignore
```

This structure is a starting point. It can be adjusted when implementation details become clearer, but maintain modularity and separation of responsibilities.

---

## 12. Development Milestones

Develop the application incrementally.

### Milestone 1: NUC environment

- Install Ubuntu Server 26.04 LTS.
- Configure networking and SSH.
- Install Python dependencies.
- Create project structure.
- Configure Git.
- Create a minimal Flask application.
- Configure application startup through systemd.

### Milestone 2: Database and initial portfolio

- Implement SQLite database.
- Create database schema.
- Implement asset management.
- Import initial holdings and average purchase prices from the existing spreadsheet.
- Implement manual cash balances.

### Milestone 3: Moomoo integration

- Implement authentication.
- Retrieve trading accounts.
- Retrieve current positions.
- Retrieve historical executed transactions.
- Retrieve cash movements.
- Implement synchronisation and duplicate detection.
- Validate the imported information against the broker account.

### Milestone 4: Market data collection

- Implement provider abstraction.
- Integrate CoinGecko.
- Integrate Moomoo quotes where available.
- Integrate an appropriate ASX market data provider.
- Implement FX conversion.
- Implement configurable scheduling.
- Add logging and error handling.

### Milestone 5: Portfolio engine

- Implement transaction-based holdings.
- Implement weighted-average cost.
- Implement portfolio valuation.
- Implement currency conversion.
- Implement realised and unrealised P&L.
- Implement asset allocation.

### Milestone 6: Dashboard

- Implement portfolio overview.
- Implement holdings.
- Implement transactions.
- Implement responsive charts.
- Implement settings.
- Implement price update status.

### Milestone 7: Dividends and forecasting

- Implement dividend synchronisation.
- Implement dividend reconciliation.
- Implement historical snapshots.
- Implement monthly forecasts.
- Implement actual-versus-predicted comparisons.
- Implement forecast scenarios.

### Milestone 8: Production deployment

- Configure VPN access.
- Implement authentication.
- Configure automated backups.
- Implement recovery procedures.
- Test application restart.
- Test database integrity.
- Validate the application after power loss and API failures.

---

## 13. Coding Standards and Instructions for GitHub Copilot

Follow these rules throughout development.

### General

1. Use Python 3.
2. Follow PEP 8.
3. Use type hints for functions and classes.
4. Use descriptive variable and function names.
5. Write modular, reusable functions.
6. Add docstrings to public classes and functions.
7. Avoid unnecessary abstractions.
8. Avoid global mutable state.
9. Keep business logic independent of Flask routes.
10. Prefer simple, maintainable implementations suitable for a small home NUC server.

### Financial calculations

1. Use Python Decimal for monetary calculations.
2. Avoid floating-point arithmetic for financial amounts.
3. Keep currency information associated with monetary values.
4. Clearly distinguish realised and unrealised gains.
5. Do not treat deposits as investment returns.
6. Preserve original transaction prices and currencies.
7. Never silently modify historical records.
8. Write unit tests for financial calculations.

### API integrations

1. Use timeouts for all external API requests.
2. Handle HTTP errors and invalid responses.
3. Implement appropriate retry mechanisms.
4. Respect API rate limits.
5. Never assume an API provides real-time data.
6. Record provider and retrieval timestamps.
7. Avoid hardcoding API credentials.
8. Implement provider-independent interfaces.

### Database

1. Use parameterised queries or SQLAlchemy.
2. Use database transactions when updating financial records.
3. Prevent duplicate imported transactions.
4. Use appropriate indexes.
5. Preserve historical information.
6. Use migrations for schema changes.
7. Implement backup and restore functionality.

### Testing

1. Use Pytest.
2. Write unit tests for calculations.
3. Mock external APIs in unit tests.
4. Write integration tests for database operations.
5. Test duplicate transaction handling.
6. Test failed API requests.
7. Test missing and stale market prices.
8. Test currency conversions.
9. Test month-end snapshot generation.

Never rely on a live brokerage account or live market API for routine automated unit tests.

### Git

- Use meaningful commit messages.
- Never commit credentials or tokens.
- Keep `.gitignore` updated.
- Document architectural changes.
- Keep dependencies version-controlled.

---

## 14. Important Development Constraints

GitHub Copilot must follow these principles:

- Do not implement trading functionality.
- Do not assume Moomoo API features are available without checking the official documentation.
- Do not assume ASX market data is free or unrestricted.
- Do not invent API endpoints or authentication flows.
- Do not hardcode portfolio holdings.
- Do not introduce paid dependencies without explicit approval.
- Do not overcomplicate the NUC deployment.
- Do not use cloud databases when SQLite is sufficient.
- Do not introduce Docker unless specifically requested.
- Do not change established accounting rules without explaining the impact.
- Do not remove existing functionality when extending the application.

When implementing a feature, first inspect the existing codebase and follow its established patterns.

If a requirement is ambiguous, ask for clarification rather than making assumptions that affect financial calculations or data integrity.

---

## 15. Initial Development Priorities

Start development in this order:

1. Establish the NUC development environment.
2. Create the Flask application.
3. Implement SQLite database and initial asset configuration.
4. Implement Moomoo API authentication and account retrieval.
5. Implement Moomoo positions and transaction synchronisation.
6. Validate the imported holdings and transactions.
7. Implement market data collection.
8. Implement portfolio calculations.
9. Build the dashboard.

Do not attempt to implement all features simultaneously.

Each milestone must leave the application in a working and testable state.

## 16. Definition of Success

The final application should provide an automated, reliable and self-hosted financial portfolio management system that replaces the existing Numbers spreadsheet.

It should automatically retrieve market data, synchronise brokerage activity, track dividends, calculate investment performance, preserve historical portfolio values and provide configurable long-term forecasts.

The application should operate continuously on the Intel NUC, with minimal maintenance and no mandatory recurring subscription costs.

**Primary design objective: a lightweight, modular, secure and maintainable personal financial portfolio server.**