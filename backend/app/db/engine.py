"""
SQLite persistence (Step 6): engine + schema creation.

One SQLite file per deployment (DATABASE_URL, default backend/voicecare.db).
The engine is cached per URL so tests can point at their own tmp file and the
app always reuses one connection pool. Tables are created on startup and on
first use -- an empty database is a valid state, no migrations needed.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path

from sqlmodel import Session, SQLModel, create_engine

from app.core.config import BACKEND_DIR, get_settings
from app.db.models import AppSetting, CallRecord, Patient  # noqa: F401 (imported for metadata)

logger = logging.getLogger("voicecare.db")

_DEFAULT_DB_PATH = BACKEND_DIR / "voicecare.db"


def resolve_database_url(settings_url: str | None = None) -> str:
    """Return a SQLAlchemy URL. Empty/unset -> the default SQLite file."""
    url = (settings_url or "").strip()
    if not url:
        return f"sqlite:///{_DEFAULT_DB_PATH.as_posix()}"
    if url == "sqlite://":  # special case: in-memory, shared via StaticPool
        return url
    return url


@lru_cache(maxsize=8)
def _engine_for(url: str):
    from sqlalchemy.pool import StaticPool

    if url == "sqlite://":
        # In-memory: a single shared connection so every Session sees the
        # same schema/data (used by tests).
        engine = create_engine(
            url,
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
    else:
        if url.startswith("sqlite:///"):
            Path(url[len("sqlite:///"):]).parent.mkdir(parents=True, exist_ok=True)
        engine = create_engine(url, connect_args={"check_same_thread": False})
    logger.info("Database engine ready: %s", url.split("///")[-1][:60])
    return engine


def get_engine(database_url: str | None = None):
    """Cached engine for the given (or configured) database URL."""
    url = resolve_database_url(
        database_url if database_url is not None else get_settings().database_url
    )
    return _engine_for(url)


def init_db(database_url: str | None = None) -> None:
    """Create tables if they do not exist (idempotent), then add any columns
    that were introduced after the local SQLite file was first created."""
    engine = get_engine(database_url)
    SQLModel.metadata.create_all(engine)
    _sync_new_columns(engine)


def _sync_new_columns(engine) -> None:
    """Add columns present in the models but missing from an existing table.

    SQLite has no formal migrations and the demo database file is created once
    and kept across steps, so a new field (e.g. the Step 9 dashboard fields)
    would otherwise make every query fail on the old file. This only ever ADDs
    nullable columns -- it never drops or rewrites data.
    """
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    for table in SQLModel.metadata.sorted_tables:
        if table.name not in existing_tables:
            continue
        have = {column["name"] for column in inspector.get_columns(table.name)}
        missing = [column for column in table.columns if column.name not in have]
        if not missing:
            continue
        with engine.begin() as connection:
            for column in missing:
                column_type = column.type.compile(engine.dialect)
                connection.execute(
                    text(
                        f'ALTER TABLE "{table.name}" '
                        f'ADD COLUMN "{column.name}" {column_type}'
                    )
                )
                default = getattr(column, "default", None)
                value = getattr(default, "arg", None)
                if isinstance(value, (str, int, float, bool)):
                    connection.execute(
                        text(
                            f'UPDATE "{table.name}" SET "{column.name}" = :value '
                            f'WHERE "{column.name}" IS NULL'
                        ),
                        {"value": value},
                    )
                logger.info(
                    "Schema sync: added %s.%s (%s)", table.name, column.name, column_type
                )


def new_session(database_url: str | None = None) -> Session:
    """Session factory for repositories."""
    return Session(get_engine(database_url))
