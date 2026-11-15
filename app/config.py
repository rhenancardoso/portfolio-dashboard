"""Application configuration, loaded from environment variables with safe defaults."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Settings:
    """Immutable application settings."""

    database_path: Path
    backup_dir: Path
    display_timezone: str = "Australia/Melbourne"

    @property
    def database_url(self) -> str:
        """SQLAlchemy URL for the SQLite database file."""
        return f"sqlite:///{self.database_path}"


def load_settings() -> Settings:
    """Build settings from environment variables.

    PORTFOLIO_DB_PATH    - SQLite file location (default: data/portfolio.db)
    PORTFOLIO_BACKUP_DIR - backup directory (default: data/backups)
    PORTFOLIO_TIMEZONE   - presentation timezone (default: Australia/Melbourne)
    """
    data_dir = PROJECT_ROOT / "data"
    return Settings(
        database_path=Path(os.environ.get("PORTFOLIO_DB_PATH", data_dir / "portfolio.db")),
        backup_dir=Path(os.environ.get("PORTFOLIO_BACKUP_DIR", data_dir / "backups")),
        display_timezone=os.environ.get("PORTFOLIO_TIMEZONE", "Australia/Melbourne"),
    )
