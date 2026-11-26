"""
Scrape management routes.

Prefix: ``/api/scrape``
"""

from __future__ import annotations

import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from backend.api.schemas import MessageResponse, ScrapeLogResponse
from backend.db.database import get_db
from backend.db.models import Bank, ScrapeLog
from backend.scrapers import SCRAPER_REGISTRY

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/scrape", tags=["Scraping"])


@router.post("/trigger", response_model=MessageResponse)
def trigger_scrape(
    bank_id: Optional[int] = Query(None, description="Scrape a specific bank; omit for all."),
    db: Session = Depends(get_db),
) -> MessageResponse:
    """
    Manually trigger scraping.

    If *bank_id* is provided, only that bank is scraped.  Otherwise all
    active banks with a registered scraper are scraped.
    """
    results: dict[str, str] = {}

    if bank_id is not None:
        bank = db.query(Bank).filter(Bank.id == bank_id).first()
        if bank is None:
            return MessageResponse(
                message="error",
                details={"error": f"Bank with id {bank_id} not found"},
            )
        banks = [bank]
    else:
        banks = (
            db.query(Bank)
            .filter(Bank.is_active == True)  # noqa: E712
            .all()
        )

    for bank in banks:
        scraper_cls = SCRAPER_REGISTRY.get(bank.short_name)
        if scraper_cls is None:
            results[bank.short_name] = "no_scraper_registered"
            logger.info("No scraper registered for %s — skipping", bank.short_name)
            continue

        try:
            scraper = scraper_cls(bank.short_name, db)
            success = scraper.run()
            results[bank.short_name] = "success" if success else "failed"
        except Exception as exc:
            logger.exception("Unhandled error scraping %s", bank.short_name)
            results[bank.short_name] = f"error: {exc}"

    return MessageResponse(
        message="Scraping complete",
        details=results,
    )


@router.get("/logs", response_model=List[ScrapeLogResponse])
def get_scrape_logs(
    limit: int = Query(50, ge=1, le=500, description="Number of log entries to return."),
    bank_id: Optional[int] = Query(None, description="Filter logs by bank ID."),
    db: Session = Depends(get_db),
) -> list[ScrapeLogResponse]:
    """Return recent scrape log entries, newest first."""
    query = db.query(ScrapeLog).join(Bank)

    if bank_id is not None:
        query = query.filter(ScrapeLog.bank_id == bank_id)

    logs = query.order_by(ScrapeLog.scraped_at.desc()).limit(limit).all()

    return [
        ScrapeLogResponse(
            bank_name=log.bank.name,
            status=log.status.value,
            error_message=log.error_message,
            scraped_at=log.scraped_at,
            duration_seconds=log.duration_seconds,
        )
        for log in logs
    ]
