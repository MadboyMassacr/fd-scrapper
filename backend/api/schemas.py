"""
Pydantic response schemas for the FD Rate Aggregator API.

These models define the shape of every JSON response returned by the API.
All models use ``model_config`` with ``from_attributes=True`` so they can
be populated directly from SQLAlchemy ORM instances.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import List, Optional

from pydantic import BaseModel, ConfigDict


# ── Bank ─────────────────────────────────────────────────────────────────────

class BankResponse(BaseModel):
    """Public representation of a bank."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    short_name: str
    logo_url: Optional[str] = None
    website_url: str
    bank_type: str


# ── FD Rate ──────────────────────────────────────────────────────────────────

class FDRateResponse(BaseModel):
    """A single FD rate row with bank context."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    bank_id: int
    bank_name: str
    bank_short_name: str
    tenure_label: str
    tenure_min_days: int
    tenure_max_days: int
    general_rate: float
    senior_citizen_rate: Optional[float] = None
    effective_date: date
    scraped_at: datetime


# ── Rate Comparison ──────────────────────────────────────────────────────────

class BankRateEntry(BaseModel):
    """Rate for a single bank inside a comparison row."""

    bank_name: str
    bank_short_name: str
    general_rate: float
    senior_citizen_rate: Optional[float] = None


class RateComparisonResponse(BaseModel):
    """Compare rates across banks for a specific tenure bucket."""

    tenure_label: str
    tenure_min_days: int
    tenure_max_days: int
    rates: List[BankRateEntry]


# ── Scrape Log ───────────────────────────────────────────────────────────────

class ScrapeLogResponse(BaseModel):
    """Audit log entry for a scraping run."""

    model_config = ConfigDict(from_attributes=True)

    bank_name: str
    status: str
    error_message: Optional[str] = None
    scraped_at: datetime
    duration_seconds: float


# ── Generic / Utility ────────────────────────────────────────────────────────

class MessageResponse(BaseModel):
    """Simple message envelope (e.g. for trigger responses)."""

    message: str
    details: Optional[dict] = None
