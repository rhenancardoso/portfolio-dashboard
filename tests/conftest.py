"""Shared fixtures: a migrated, throwaway SQLite database per test."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.database.db import (
    create_db_engine,
    create_session_factory,
    upgrade_database,
)


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(database_path=tmp_path / "test.db", backup_dir=tmp_path / "backups")


@pytest.fixture
def engine(settings: Settings) -> Iterator[Engine]:
    upgrade_database(settings)
    engine = create_db_engine(settings)
    yield engine
    engine.dispose()


@pytest.fixture
def session_factory(engine: Engine) -> sessionmaker[Session]:
    return create_session_factory(engine)
