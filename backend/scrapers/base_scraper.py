"""
Abstract base class for all bank FD-rate scrapers.

Every bank-specific scraper should subclass ``BaseScraper`` and implement
the ``scrape()`` method which fetches and parses the rate page, returning
a list of rate dictionaries.

The ``run()`` method orchestrates scraping, persistence, retry logic, and
audit logging so subclasses only need to worry about parsing.
"""

from __future__ import annotations

import logging
import re
import time
from abc import ABC, abstractmethod
from datetime import date, datetime
from typing import Any

from sqlalchemy.orm import Session

from backend.db.models import Bank, FDRate, ScrapeLog, ScrapeStatus

logger = logging.getLogger(__name__)

# Common browser User-Agent to avoid being blocked by bank websites
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/128.0.0.0 Safari/537.36"
)

# Maximum number of retries when scrape() fails
MAX_RETRIES = 3

# Seconds to wait between retries (doubles each attempt)
BASE_RETRY_DELAY = 2.0


class BaseScraper(ABC):
    """
    Abstract base scraper for bank FD interest-rate pages.

    Parameters
    ----------
    bank_short_name : str
        The short name of the bank (e.g. ``"SBI"``).  Must match a row in
        the ``banks`` table.
    db_session : Session
        An active SQLAlchemy session used for reading bank info and persisting
        scraped rates.
    """

    def __init__(self, bank_short_name: str, db_session: Session) -> None:
        self.bank_short_name = bank_short_name
        self.db = db_session
        self.bank: Bank | None = None
        self.logger = logging.getLogger(f"{__name__}.{self.__class__.__name__}")

    # ── Abstract ──────────────────────────────────────────────────────

    @abstractmethod
    def scrape(self) -> list[dict[str, Any]]:
        """
        Fetch and parse FD rates from the bank website.

        Returns
        -------
        list[dict]
            Each dict must contain:
            - tenure_label (str)
            - tenure_min_days (int)
            - tenure_max_days (int)
            - general_rate (float)
            - senior_citizen_rate (float | None)
        """
        ...

    # ── Public orchestrator ───────────────────────────────────────────

    def run(self) -> bool:
        """
        Execute the full scraping pipeline with retries, persistence, and
        audit logging.

        Returns
        -------
        bool
            ``True`` if scraping succeeded, ``False`` otherwise.
        """
        # Resolve bank record
        self.bank = (
            self.db.query(Bank)
            .filter(Bank.short_name == self.bank_short_name)
            .first()
        )
        if self.bank is None:
            self.logger.error(
                "Bank '%s' not found in database — cannot scrape.",
                self.bank_short_name,
            )
            return False

        start_time = time.time()
        last_error: str = ""

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                self.logger.info(
                    "Scraping %s — attempt %d/%d",
                    self.bank_short_name, attempt, MAX_RETRIES,
                )

                rates = self.scrape()

                if not rates:
                    self.logger.warning(
                        "Scraper for %s returned zero rates on attempt %d.",
                        self.bank_short_name, attempt,
                    )
                    last_error = "Scraper returned zero rates"
                    # Treat empty result as partial on the last try
                    if attempt < MAX_RETRIES:
                        time.sleep(BASE_RETRY_DELAY * attempt)
                        continue
                    # Last attempt — log as partial
                    duration = time.time() - start_time
                    self._log_scrape(ScrapeStatus.partial, last_error, duration)
                    return False

                # Persist the scraped rates
                self._save_rates(rates)
                duration = time.time() - start_time
                self._log_scrape(ScrapeStatus.success, None, duration)
                self.logger.info(
                    "Successfully scraped %d rates for %s in %.1fs",
                    len(rates), self.bank_short_name, duration,
                )
                return True

            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                self.logger.warning(
                    "Attempt %d/%d for %s failed: %s",
                    attempt, MAX_RETRIES, self.bank_short_name, last_error,
                )
                if attempt < MAX_RETRIES:
                    time.sleep(BASE_RETRY_DELAY * attempt)

        # All retries exhausted
        duration = time.time() - start_time
        self._log_scrape(ScrapeStatus.failed, last_error, duration)
        self.logger.error(
            "All %d attempts failed for %s. Last error: %s",
            MAX_RETRIES, self.bank_short_name, last_error,
        )
        return False

    # ── Helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _parse_rate(text: str) -> float:
        """
        Parse a rate string like ``"7.25%"`` or ``"7.25"`` into a float.

        Parameters
        ----------
        text : str
            Raw rate text from the webpage.

        Returns
        -------
        float
            The numeric rate value (e.g. ``7.25``).

        Raises
        ------
        ValueError
            If no numeric value can be extracted from *text*.
        """
        if not text:
            raise ValueError("Empty rate text")

        cleaned = text.strip().replace("%", "").replace(",", "").strip()

        # Handle dash / hyphen meaning "not available"
        if cleaned in ("-", "–", "—", "N/A", "NA", ""):
            raise ValueError(f"Rate not available: {text!r}")

        match = re.search(r"(\d+\.?\d*)", cleaned)
        if match is None:
            raise ValueError(f"Cannot parse rate from: {text!r}")

        return float(match.group(1))

    @staticmethod
    def _parse_tenure_days(text: str) -> tuple[int, int]:
        """
        Parse a human-readable tenure string into (min_days, max_days).

        Supports formats commonly seen on Indian bank websites:
        - ``"7 days to 45 days"``
        - ``"46 days to 179 days"``
        - ``"6 months to 9 months"``
        - ``"1 year to 2 years"``
        - ``"1 year 1 day to 2 years"``
        - ``"5 years and above"``
        - ``"10 years"``

        Parameters
        ----------
        text : str
            The tenure description as it appears on the bank website.

        Returns
        -------
        tuple[int, int]
            ``(min_days, max_days)``
        """

        def _to_days(fragment: str) -> int:
            """Convert a single tenure fragment like '1 year 6 months 1 day' to days."""
            fragment = fragment.lower().strip()
            total = 0
            # Extract years
            yr = re.search(r"(\d+)\s*(?:year|yr)s?", fragment)
            if yr:
                total += int(yr.group(1)) * 365
            # Extract months
            mo = re.search(r"(\d+)\s*(?:month|mon)s?", fragment)
            if mo:
                total += int(mo.group(1)) * 30
            # Extract days
            dy = re.search(r"(\d+)\s*(?:day)s?", fragment)
            if dy:
                total += int(dy.group(1))
            # If nothing was parsed, try bare number (assume days)
            if total == 0:
                bare = re.search(r"(\d+)", fragment)
                if bare:
                    total = int(bare.group(1))
            return total if total > 0 else 1

        text_clean = text.strip()

        # "X and above" / "X & above" — treat max as 10 years
        if re.search(r"(?:and|&)\s*above", text_clean, re.IGNORECASE):
            part = re.split(r"(?:and|&)\s*above", text_clean, flags=re.IGNORECASE)[0]
            min_d = _to_days(part)
            return (min_d, 3650)

        # "X to Y" / "X - Y"
        parts = re.split(r"\s+to\s+|\s*-\s*", text_clean, maxsplit=1)
        if len(parts) == 2:
            return (_to_days(parts[0]), _to_days(parts[1]))

        # Single value like "5 years" or "180 days"
        d = _to_days(text_clean)
        return (d, d)

    # ── Private persistence helpers ───────────────────────────────────

    def _save_rates(self, rates: list[dict[str, Any]]) -> None:
        """Persist a list of rate dicts as ``FDRate`` rows."""
        today = date.today()
        now = datetime.utcnow()

        for r in rates:
            fd = FDRate(
                bank_id=self.bank.id,
                tenure_label=r["tenure_label"],
                tenure_min_days=r["tenure_min_days"],
                tenure_max_days=r["tenure_max_days"],
                general_rate=r["general_rate"],
                senior_citizen_rate=r.get("senior_citizen_rate"),
                effective_date=today,
                scraped_at=now,
            )
            self.db.add(fd)

        self.db.commit()

    def _log_scrape(
        self,
        status: ScrapeStatus,
        error_message: str | None,
        duration: float,
    ) -> None:
        """Write an entry to the ``scrape_logs`` table."""
        log = ScrapeLog(
            bank_id=self.bank.id,
            status=status,
            error_message=error_message,
            scraped_at=datetime.utcnow(),
            duration_seconds=round(duration, 2),
        )
        self.db.add(log)
        self.db.commit()
