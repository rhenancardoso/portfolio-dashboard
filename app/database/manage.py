"""Command line database management.

Usage (from the project root):
    python -m app.database.manage upgrade
    python -m app.database.manage seed
    python -m app.database.manage backup
    python -m app.database.manage restore PATH
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from app.config import load_settings
from app.database.db import (
    backup_database,
    create_db_engine,
    create_session_factory,
    restore_database,
    session_scope,
    upgrade_database,
)
from app.database.seed import seed_reference_data


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Portfolio database management")
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("upgrade", help="apply migrations")
    subcommands.add_parser("seed", help="insert missing initial assets and settings")
    subcommands.add_parser("backup", help="write a timestamped database backup")
    restore = subcommands.add_parser("restore", help="restore a backup (stop the app first)")
    restore.add_argument("path", type=Path)
    args = parser.parse_args(argv)

    settings = load_settings()
    if args.command == "upgrade":
        upgrade_database(settings)
    elif args.command == "seed":
        engine = create_db_engine(settings)
        with session_scope(create_session_factory(engine)) as session:
            seed_reference_data(session)
        engine.dispose()
    elif args.command == "backup":
        print(backup_database(settings))
    elif args.command == "restore":
        print(f"Previous database saved to: {restore_database(args.path, settings)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
