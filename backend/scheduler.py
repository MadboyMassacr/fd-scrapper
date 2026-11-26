"""
APScheduler-based daily scraping scheduler.

Schedules ``run_all_scrapers()`` to execute once per day at the configured
hour/minute.  Can be run standalone (``python -m backend.scheduler``) or
imported and started alongside the FastAPI app.
"""

from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from backend.config import settings
from backend.db.database import SessionLocal
from backend.db.models import Bank
from backend.scrapers import SCRAPER_REGISTRY

logger = logging.getLogger(__name__)


def run_all_scrapers() -> dict[str, str]:
    """
    Iterate over all active banks that have a registered scraper and run each.

    Returns
    -------
    dict[str, str]
        Mapping of ``bank_short_name`` → ``"success"`` / ``"failed"`` / ``"no_scraper"``.
    """
    logger.info("── Scheduled scraping run started ──")
    results: dict[str, str] = {}

    db = SessionLocal()
    try:
        banks = db.query(Bank).filter(Bank.is_active == True).all()  # noqa: E712

        for bank in banks:
            scraper_cls = SCRAPER_REGISTRY.get(bank.short_name)
            if scraper_cls is None:
                logger.info("No scraper for %s — skipping", bank.short_name)
                results[bank.short_name] = "no_scraper"
                continue

            try:
                scraper = scraper_cls(bank.short_name, db)
                success = scraper.run()
                results[bank.short_name] = "success" if success else "failed"
            except Exception:
                logger.exception("Unhandled error scraping %s", bank.short_name)
                results[bank.short_name] = "error"
    finally:
        db.close()

    logger.info("── Scheduled scraping run finished: %s ──", results)
    return results


def create_scheduler() -> BackgroundScheduler:
    """
    Create and return a configured ``BackgroundScheduler``.

    The scheduler fires ``run_all_scrapers`` daily at the time configured by
    ``SCRAPE_HOUR`` and ``SCRAPE_MINUTE``.
    """
    scheduler = BackgroundScheduler()

    trigger = CronTrigger(
        hour=settings.SCRAPE_HOUR,
        minute=settings.SCRAPE_MINUTE,
    )

    scheduler.add_job(
        run_all_scrapers,
        trigger=trigger,
        id="daily_fd_scrape",
        name="Daily FD Rate Scraper",
        replace_existing=True,
    )

    logger.info(
        "Scheduler configured — daily scrape at %02d:%02d",
        settings.SCRAPE_HOUR,
        settings.SCRAPE_MINUTE,
    )
    return scheduler


# ── Standalone entry point ───────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    )

    # Run once immediately, then start the cron scheduler
    logger.info("Running all scrapers immediately…")
    run_all_scrapers()

    scheduler = create_scheduler()
    scheduler.start()

    logger.info("Scheduler running. Press Ctrl+C to stop.")
    try:
        import time

        while True:
            time.sleep(60)
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()
        logger.info("Scheduler stopped.")
