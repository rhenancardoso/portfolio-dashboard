"""Database engine, sessions, migrations and backup/restore for SQLite."""

from __future__ import annotations

import shutil
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from app.config import PROJECT_ROOT, Settings, load_settings

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


def create_db_engine(settings: Settings | None = None) -> Engine:
    """Create an engine with SQLite pragmas suited to a small server.

    Foreign keys are enforced; WAL lets the web app read while collectors write.
    """
    settings = settings or load_settings()
    settings.database_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(settings.database_url)

    @event.listens_for(engine, "connect")
    def _set_pragmas(dbapi_connection: sqlite3.Connection, _record: object) -> None:
        # pysqlite's implicit transaction handling is replaced so that BEGIN is
        # emitted explicitly and rolled back/committed atomically.
        dbapi_connection.isolation_level = None
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys = ON")
        cursor.execute("PRAGMA journal_mode = WAL")
        cursor.execute("PRAGMA synchronous = NORMAL")
        cursor.execute("PRAGMA busy_timeout = 5000")
        cursor.close()

    @event.listens_for(engine, "begin")
    def _begin(connection: sqlite3.Connection) -> None:
        connection.exec_driver_sql("BEGIN")

    return engine


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    """Session factory; objects stay usable after commit."""
    return sessionmaker(bind=engine, expire_on_commit=False)


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    """Provide a transactional session: commit on success, roll back on error.

    Use for every financial data update so changes apply atomically.
    """
    session = factory()
    try:
        yield session
        session.commit()
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()


def alembic_config(settings: Settings | None = None) -> Config:
    """Alembic configuration pointing at the project's migrations and database."""
    settings = settings or load_settings()
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    config.set_main_option("sqlalchemy.url", settings.database_url.replace("%", "%%"))
    return config


def upgrade_database(settings: Settings | None = None, revision: str = "head") -> None:
    """Apply migrations up to ``revision`` (default: latest)."""
    command.upgrade(alembic_config(settings), revision)


def backup_database(settings: Settings | None = None) -> Path:
    """Write a consistent, timestamped copy of the database and return its path.

    Uses SQLite's online backup API, which is safe while the app is running.
    """
    settings = settings or load_settings()
    settings.backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    destination = settings.backup_dir / f"{settings.database_path.stem}-{stamp}.db"
    source = sqlite3.connect(settings.database_path)
    target = sqlite3.connect(destination)
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()
    return destination


def restore_database(backup_path: Path, settings: Settings | None = None) -> Path:
    """Replace the database with a backup and return the path of the safety copy.

    The backup is integrity-checked first and the current database is backed up
    before it is replaced. Stop the application (and dispose of engines) first.
    """
    settings = settings or load_settings()
    backup_path = Path(backup_path)
    if not backup_path.is_file():
        raise FileNotFoundError(backup_path)

    check = sqlite3.connect(f"file:{backup_path}?mode=ro", uri=True)
    try:
        result = check.execute("PRAGMA integrity_check").fetchone()
    finally:
        check.close()
    if result is None or result[0] != "ok":
        raise ValueError(f"Backup failed integrity check: {backup_path}")

    safety_copy = backup_database(settings) if settings.database_path.exists() else None
    for suffix in ("-wal", "-shm"):
        Path(f"{settings.database_path}{suffix}").unlink(missing_ok=True)
    shutil.copyfile(backup_path, settings.database_path)
    return safety_copy if safety_copy is not None else settings.database_path
