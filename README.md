# Portfolio Dashboard

Self-hosted personal portfolio dashboard running on an Intel NUC with Ubuntu Server 26.04 LTS.
To work on the code, see [CONTRIBUTING.md](CONTRIBUTING.md).

## Installation

```bash
sudo apt install python3-venv python3-pip pipx
pipx install poetry
git clone git@github.com:rhenancardoso/portfolio-dashboard.git
cd portfolio-dashboard
python3 -m venv .py314
source .py314/bin/activate
poetry install --no-root --only main
python -m app.database.manage upgrade   # create the database
python -m app.database.manage seed      # add the initial assets and default settings
```

`seed` only adds what is missing, so it is safe to re-run.

## Running the web application

The app runs as a systemd service
([deploy/systemd/portfolio-web.service](deploy/systemd/portfolio-web.service)). It listens on
`127.0.0.1:8000`, so it is reachable only from the NUC itself until password protection and
HTTPS are added.

Install the service (adjust `User` and paths in the unit file first):

```bash
sudo cp deploy/systemd/portfolio-web.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now portfolio-web.service
curl http://127.0.0.1:8000/health
```

The health check returns `{"status": "ok", "database": "ok"}`, or HTTP 503 when the database
cannot be reached.

Useful service commands:

```bash
systemctl status portfolio-web.service
journalctl -u portfolio-web.service -f      # follow the logs
sudo systemctl restart portfolio-web.service
```

### Access from other devices on the home network

To open the app from a phone or another computer (for example
`http://192.168.1.147:8000/health`):

1. Allow the port through the firewall, for the home network only:

   ```bash
   sudo ufw allow from 192.168.1.0/24 to any port 8000 proto tcp
   ```

2. Change `127.0.0.1:8000` to `0.0.0.0:8000` in `portfolio-web.service`, then restart it:

   ```bash
   sudo systemctl daemon-reload && sudo systemctl restart portfolio-web.service
   ```

Use `http://`, not `https://`: there is no HTTPS yet. The device must be on the home Wi-Fi
(not mobile data), and some routers block Wi-Fi devices from reaching wired ones ("client
isolation" or a guest network).

The dashboard has no password yet. Once real pages exist, keep the app on `127.0.0.1` or
reach it through a VPN such as Tailscale rather than exposing it to the whole network.

## Configuration

Settings come from environment variables. For the systemd service, put them in
`/etc/portfolio-dashboard/env` (`chmod 600`, never commit it); the service reads that file if
it exists.

| Variable | Default | Purpose |
|---|---|---|
| `PORTFOLIO_DB_PATH` | `data/portfolio.db` | SQLite database file |
| `PORTFOLIO_BACKUP_DIR` | `data/backups` | Backup directory |
| `PORTFOLIO_TIMEZONE` | `Australia/Melbourne` | Timezone used to display times |

## Backup and restore

```bash
python -m app.database.manage backup         # timestamped backup in PORTFOLIO_BACKUP_DIR
python -m app.database.manage restore PATH   # restore a backup; stop the app first
```

Backups are safe to take while the app is running. `restore` saves the current database
before replacing it and prints where the copy went.

To apply database updates after upgrading the application, run
`python -m app.database.manage upgrade`.
