"""
SQLAlchemy database engine, session factory, and FastAPI dependency.

Provides:
    - ``engine``     – the SQLAlchemy engine bound to the configured DATABASE_URL
    - ``SessionLocal`` – a scoped session factory
    - ``Base``       – declarative base for ORM models
    - ``get_db()``   – FastAPI dependency that yields a DB session
"""

from __future__ import annotations

import logging
from collections.abc import Generator
from typing import Any

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from backend.config import settings

logger = logging.getLogger(__name__)

# ── Engine ────────────────────────────────────────────────────────────────────
# For SQLite we need check_same_thread=False so FastAPI's thread pool can share
# the connection.  For other dialects the kwarg is harmless but we guard anyway.
_connect_args: dict[str, Any] = {}
if settings.DATABASE_URL.startswith("sqlite"):
    _connect_args["check_same_thread"] = False

engine = create_engine(
    settings.DATABASE_URL,
    connect_args=_connect_args,
    echo=False,
    pool_pre_ping=True,
)

# ── Session factory ──────────────────────────────────────────────────────────
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


# ── Declarative Base ─────────────────────────────────────────────────────────
class Base(DeclarativeBase):
    """Base class for all ORM models."""
    pass


# ── FastAPI dependency ───────────────────────────────────────────────────────
def get_db() -> Generator[Session, None, None]:
    """Yield a SQLAlchemy session and ensure it is closed after the request."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
