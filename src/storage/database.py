"""Database initialisation and session management."""

from __future__ import annotations
from contextlib import contextmanager
from pathlib import Path
from sqlmodel import SQLModel, create_engine, Session

_engine = None


def _get_engine(db_path: Path):
    global _engine
    if _engine is None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        url = f"sqlite:///{db_path}"
        _engine = create_engine(url, echo=False, connect_args={"check_same_thread": False})
    return _engine


def init_db(db_path: Path) -> None:
    """Create all tables if they don't exist, then run lightweight migrations."""
    import src.storage.models  # noqa: F401 — registers models with SQLModel metadata
    engine = _get_engine(db_path)
    SQLModel.metadata.create_all(engine)
    _migrate_add_columns(engine)


# Columns added after the first release. SQLModel's create_all won't ALTER an
# existing table, so we add any missing columns by hand (SQLite ADD COLUMN is
# cheap and idempotent thanks to the existence check).
_ADDED_COLUMNS = {
    "opportunities": [
        ("is_target_company", "BOOLEAN DEFAULT 0"),
        ("company_category", "VARCHAR"),
        ("batch_2028", "BOOLEAN DEFAULT 0"),
        ("priority", "BOOLEAN DEFAULT 0"),
    ],
}


def _migrate_add_columns(engine) -> None:
    with engine.begin() as conn:
        for table, columns in _ADDED_COLUMNS.items():
            try:
                existing = {
                    row[1] for row in conn.exec_driver_sql(f'PRAGMA table_info("{table}")')
                }
            except Exception:
                continue
            for name, ddl in columns:
                if name not in existing:
                    try:
                        conn.exec_driver_sql(f'ALTER TABLE "{table}" ADD COLUMN {name} {ddl}')
                    except Exception:
                        pass


@contextmanager
def get_session(db_path: Path):
    """Context-manager session (preferred for short-lived writes)."""
    session = Session(_get_engine(db_path))
    try:
        yield session
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def open_session(db_path: Path) -> Session:
    """Return a long-lived session (caller is responsible for closing)."""
    return Session(_get_engine(db_path))
