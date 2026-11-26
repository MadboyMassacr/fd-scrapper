"""
FD rate query routes.

Prefix: ``/api/rates``
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import List, Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func
from sqlalchemy.orm import Session

from backend.api.schemas import (
    BankRateEntry,
    FDRateResponse,
    RateComparisonResponse,
)
from backend.db.database import get_db
from backend.db.models import Bank, FDRate

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/rates", tags=["Rates"])


# ── Helpers ──────────────────────────────────────────────────────────────────

def _rate_to_response(rate: FDRate) -> FDRateResponse:
    """Convert an ORM ``FDRate`` (with joined bank) to a response model."""
    return FDRateResponse(
        id=rate.id,
        bank_id=rate.bank_id,
        bank_name=rate.bank.name,
        bank_short_name=rate.bank.short_name,
        tenure_label=rate.tenure_label,
        tenure_min_days=rate.tenure_min_days,
        tenure_max_days=rate.tenure_max_days,
        general_rate=rate.general_rate,
        senior_citizen_rate=rate.senior_citizen_rate,
        effective_date=rate.effective_date,
        scraped_at=rate.scraped_at,
    )


# ── Endpoints ────────────────────────────────────────────────────────────────

@router.get("/", response_model=List[FDRateResponse])
def get_rates(
    effective_date: Optional[date] = Query(None, alias="date", description="Filter by effective date (YYYY-MM-DD). Defaults to today."),
    bank_id: Optional[int] = Query(None, description="Filter by bank ID."),
    min_tenure: Optional[int] = Query(None, description="Minimum tenure in days."),
    max_tenure: Optional[int] = Query(None, description="Maximum tenure in days."),
    db: Session = Depends(get_db),
) -> list[FDRateResponse]:
    """
    List FD rates with optional filters.

    By default returns rates for today's effective date.
    """
    if effective_date is None:
        effective_date = date.today()

    query = db.query(FDRate).join(Bank).filter(FDRate.effective_date == effective_date)

    if bank_id is not None:
        query = query.filter(FDRate.bank_id == bank_id)
    if min_tenure is not None:
        query = query.filter(FDRate.tenure_min_days >= min_tenure)
    if max_tenure is not None:
        query = query.filter(FDRate.tenure_max_days <= max_tenure)

    rates = query.order_by(Bank.name, FDRate.tenure_min_days).all()
    return [_rate_to_response(r) for r in rates]


@router.get("/latest", response_model=List[FDRateResponse])
def get_latest_rates(db: Session = Depends(get_db)) -> list[FDRateResponse]:
    """
    Return the latest scraped rates for every active bank.

    For each bank, finds the most recent ``scraped_at`` timestamp and returns
    all rate rows from that scraping run.
    """
    # Sub-query: latest scraped_at per bank
    latest_sq = (
        db.query(
            FDRate.bank_id,
            func.max(FDRate.scraped_at).label("max_scraped_at"),
        )
        .group_by(FDRate.bank_id)
        .subquery()
    )

    rates = (
        db.query(FDRate)
        .join(Bank)
        .join(
            latest_sq,
            (FDRate.bank_id == latest_sq.c.bank_id)
            & (FDRate.scraped_at == latest_sq.c.max_scraped_at),
        )
        .filter(Bank.is_active == True)  # noqa: E712
        .order_by(Bank.name, FDRate.tenure_min_days)
        .all()
    )
    return [_rate_to_response(r) for r in rates]


@router.get("/compare", response_model=List[RateComparisonResponse])
def compare_rates(
    min_tenure: Optional[int] = Query(None, description="Filter: tenure_min_days >= value"),
    max_tenure: Optional[int] = Query(None, description="Filter: tenure_max_days <= value"),
    db: Session = Depends(get_db),
) -> list[RateComparisonResponse]:
    """
    Compare latest FD rates across all active banks.

    Groups rates by tenure bucket and shows each bank's rate side-by-side.
    """
    # Get latest rates for all banks (reuse sub-query logic)
    latest_sq = (
        db.query(
            FDRate.bank_id,
            func.max(FDRate.scraped_at).label("max_scraped_at"),
        )
        .group_by(FDRate.bank_id)
        .subquery()
    )

    query = (
        db.query(FDRate)
        .join(Bank)
        .join(
            latest_sq,
            (FDRate.bank_id == latest_sq.c.bank_id)
            & (FDRate.scraped_at == latest_sq.c.max_scraped_at),
        )
        .filter(Bank.is_active == True)  # noqa: E712
    )

    if min_tenure is not None:
        query = query.filter(FDRate.tenure_min_days >= min_tenure)
    if max_tenure is not None:
        query = query.filter(FDRate.tenure_max_days <= max_tenure)

    rates = query.order_by(FDRate.tenure_min_days, Bank.name).all()

    # Group by tenure bucket (min_days, max_days)
    buckets: dict[tuple[int, int, str], list[FDRate]] = defaultdict(list)
    for r in rates:
        key = (r.tenure_min_days, r.tenure_max_days, r.tenure_label)
        buckets[key].append(r)

    results: list[RateComparisonResponse] = []
    for (min_d, max_d, label), bucket_rates in sorted(buckets.items()):
        bank_entries = [
            BankRateEntry(
                bank_name=r.bank.name,
                bank_short_name=r.bank.short_name,
                general_rate=r.general_rate,
                senior_citizen_rate=r.senior_citizen_rate,
            )
            for r in bucket_rates
        ]
        results.append(
            RateComparisonResponse(
                tenure_label=label,
                tenure_min_days=min_d,
                tenure_max_days=max_d,
                rates=bank_entries,
            )
        )

    return results


@router.get("/history/{bank_id}", response_model=List[FDRateResponse])
def get_rate_history(
    bank_id: int,
    days: int = Query(30, ge=1, le=365, description="Number of days of history."),
    db: Session = Depends(get_db),
) -> list[FDRateResponse]:
    """
    Return historical FD rates for a specific bank over the last *days* days.
    """
    cutoff = date.today() - timedelta(days=days)

    rates = (
        db.query(FDRate)
        .join(Bank)
        .filter(
            FDRate.bank_id == bank_id,
            FDRate.effective_date >= cutoff,
        )
        .order_by(FDRate.effective_date.desc(), FDRate.tenure_min_days)
        .all()
    )
    return [_rate_to_response(r) for r in rates]


@router.get("/best", response_model=List[RateComparisonResponse])
def get_best_rates(db: Session = Depends(get_db)) -> list[RateComparisonResponse]:
    """
    Return the best (highest general_rate) FD rates per tenure bucket across
    all active banks from the latest scrape.
    """
    # Get latest rates first
    latest_sq = (
        db.query(
            FDRate.bank_id,
            func.max(FDRate.scraped_at).label("max_scraped_at"),
        )
        .group_by(FDRate.bank_id)
        .subquery()
    )

    rates = (
        db.query(FDRate)
        .join(Bank)
        .join(
            latest_sq,
            (FDRate.bank_id == latest_sq.c.bank_id)
            & (FDRate.scraped_at == latest_sq.c.max_scraped_at),
        )
        .filter(Bank.is_active == True)  # noqa: E712
        .all()
    )

    # Group by tenure bucket and pick the best
    buckets: dict[tuple[int, int, str], list[FDRate]] = defaultdict(list)
    for r in rates:
        key = (r.tenure_min_days, r.tenure_max_days, r.tenure_label)
        buckets[key].append(r)

    results: list[RateComparisonResponse] = []
    for (min_d, max_d, label), bucket_rates in sorted(buckets.items()):
        # Sort by general_rate descending to surface the best
        bucket_rates.sort(key=lambda x: x.general_rate, reverse=True)
        entries = [
            BankRateEntry(
                bank_name=r.bank.name,
                bank_short_name=r.bank.short_name,
                general_rate=r.general_rate,
                senior_citizen_rate=r.senior_citizen_rate,
            )
            for r in bucket_rates
        ]
        results.append(
            RateComparisonResponse(
                tenure_label=label,
                tenure_min_days=min_d,
                tenure_max_days=max_d,
                rates=entries,
            )
        )

    return results
