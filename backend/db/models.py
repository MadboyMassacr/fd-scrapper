"""
SQLAlchemy ORM models for the FD Rate Scraper.

Tables
------
- **banks**       – Master list of banks being tracked.
- **fd_rates**    – Individual FD interest-rate rows scraped from bank websites.
- **scrape_logs** – Audit log recording every scraping attempt.
"""

from __future__ import annotations

import enum
from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    Column,
    Date,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import relationship

from backend.db.database import Base


# ── Enums ────────────────────────────────────────────────────────────────────

class BankType(str, enum.Enum):
    """Classification of Indian banks."""
    public = "public"
    private = "private"
    small_finance = "small_finance"


class ScrapeStatus(str, enum.Enum):
    """Outcome of a scraping run."""
    success = "success"
    failed = "failed"
    partial = "partial"


# ── Models ───────────────────────────────────────────────────────────────────

class Bank(Base):
    """A bank whose FD rates are being tracked."""

    __tablename__ = "banks"

    id: int = Column(Integer, primary_key=True, autoincrement=True)
    name: str = Column(String(200), nullable=False, unique=True)
    short_name: str = Column(String(50), nullable=False, unique=True)
    logo_url: str | None = Column(String(500), nullable=True)
    website_url: str = Column(String(500), nullable=False)
    bank_type: BankType = Column(Enum(BankType), nullable=False)
    is_active: bool = Column(Boolean, default=True, nullable=False)
    created_at: datetime = Column(DateTime, server_default=func.now(), nullable=False)

    # Relationships
    rates = relationship("FDRate", back_populates="bank", cascade="all, delete-orphan")
    scrape_logs = relationship("ScrapeLog", back_populates="bank", cascade="all, delete-orphan")

    def __repr__(self) -> str:
        return f"<Bank(id={self.id}, short_name={self.short_name!r})>"


class FDRate(Base):
    """A single FD interest-rate row for a specific bank and tenure bucket."""

    __tablename__ = "fd_rates"

    id: int = Column(Integer, primary_key=True, autoincrement=True)
    bank_id: int = Column(Integer, ForeignKey("banks.id", ondelete="CASCADE"), nullable=False)

    tenure_label: str = Column(String(200), nullable=False)          # e.g. "7 days to 45 days"
    tenure_min_days: int = Column(Integer, nullable=False)
    tenure_max_days: int = Column(Integer, nullable=False)

    general_rate: float = Column(Float, nullable=False)              # e.g. 6.50
    senior_citizen_rate: float | None = Column(Float, nullable=True) # e.g. 7.00

    effective_date: date = Column(Date, nullable=False)
    scraped_at: datetime = Column(DateTime, server_default=func.now(), nullable=False)

    # Relationships
    bank = relationship("Bank", back_populates="rates")

    # Indexes for common query patterns
    __table_args__ = (
        Index("ix_fd_rates_bank_id", "bank_id"),
        Index("ix_fd_rates_effective_date", "effective_date"),
        Index("ix_fd_rates_scraped_at", "scraped_at"),
        Index("ix_fd_rates_bank_effective", "bank_id", "effective_date"),
    )

    def __repr__(self) -> str:
        return (
            f"<FDRate(id={self.id}, bank_id={self.bank_id}, "
            f"tenure={self.tenure_label!r}, rate={self.general_rate})>"
        )


class ScrapeLog(Base):
    """Audit record for each scraping attempt."""

    __tablename__ = "scrape_logs"

    id: int = Column(Integer, primary_key=True, autoincrement=True)
    bank_id: int = Column(Integer, ForeignKey("banks.id", ondelete="CASCADE"), nullable=False)

    status: ScrapeStatus = Column(Enum(ScrapeStatus), nullable=False)
    error_message: str | None = Column(Text, nullable=True)
    scraped_at: datetime = Column(DateTime, server_default=func.now(), nullable=False)
    duration_seconds: float = Column(Float, nullable=False, default=0.0)

    # Relationships
    bank = relationship("Bank", back_populates="scrape_logs")

    # Indexes
    __table_args__ = (
        Index("ix_scrape_logs_bank_id", "bank_id"),
        Index("ix_scrape_logs_scraped_at", "scraped_at"),
    )

    def __repr__(self) -> str:
        return (
            f"<ScrapeLog(id={self.id}, bank_id={self.bank_id}, "
            f"status={self.status.value!r})>"
        )
