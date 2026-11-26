"""
Seed the database with initial bank records.

Run this script to populate the ``banks`` table with the 10 Indian banks
tracked by the FD Rate Scraper.  Existing banks (matched by ``short_name``)
are skipped to make the script idempotent.

Usage:
    python -m backend.seed_banks
"""

from __future__ import annotations

import logging

from backend.db.database import Base, SessionLocal, engine
from backend.db.models import Bank, BankType

logger = logging.getLogger(__name__)

# ── Bank seed data ───────────────────────────────────────────────────────────

SEED_BANKS: list[dict] = [
    {
        "name": "State Bank of India",
        "short_name": "SBI",
        "website_url": "https://www.sbi.co.in",
        "bank_type": BankType.public,
    },
    {
        "name": "HDFC Bank",
        "short_name": "HDFC",
        "website_url": "https://www.hdfcbank.com",
        "bank_type": BankType.private,
    },
    {
        "name": "ICICI Bank",
        "short_name": "ICICI",
        "website_url": "https://www.icicibank.com",
        "bank_type": BankType.private,
    },
    {
        "name": "Punjab National Bank",
        "short_name": "PNB",
        "website_url": "https://www.pnbindia.in",
        "bank_type": BankType.public,
    },
    {
        "name": "Bank of Baroda",
        "short_name": "BoB",
        "website_url": "https://www.bankofbaroda.in",
        "bank_type": BankType.public,
    },
    {
        "name": "Axis Bank",
        "short_name": "Axis",
        "website_url": "https://www.axisbank.com",
        "bank_type": BankType.private,
    },
    {
        "name": "Kotak Mahindra Bank",
        "short_name": "Kotak",
        "website_url": "https://www.kotak.com",
        "bank_type": BankType.private,
    },
    {
        "name": "Canara Bank",
        "short_name": "Canara",
        "website_url": "https://www.canarabank.com",
        "bank_type": BankType.public,
    },
    {
        "name": "Union Bank of India",
        "short_name": "Union",
        "website_url": "https://www.unionbankofindia.co.in",
        "bank_type": BankType.public,
    },
    {
        "name": "Indian Bank",
        "short_name": "Indian",
        "website_url": "https://www.indianbank.in",
        "bank_type": BankType.public,
    },
]


def seed_banks() -> int:
    """
    Insert seed bank records into the database.

    Returns
    -------
    int
        Number of new banks inserted.
    """
    # Ensure tables exist
    Base.metadata.create_all(bind=engine)

    db = SessionLocal()
    inserted = 0
    try:
        for data in SEED_BANKS:
            existing = (
                db.query(Bank)
                .filter(Bank.short_name == data["short_name"])
                .first()
            )
            if existing:
                logger.debug("Bank %s already exists — skipping", data["short_name"])
                continue

            bank = Bank(**data)
            db.add(bank)
            inserted += 1
            logger.info("Seeded bank: %s (%s)", data["name"], data["short_name"])

        db.commit()
    except Exception:
        db.rollback()
        logger.exception("Error seeding banks")
        raise
    finally:
        db.close()

    logger.info("Seeding complete — %d new banks inserted", inserted)
    return inserted


# ── Standalone entry point ───────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    )
    seed_banks()
