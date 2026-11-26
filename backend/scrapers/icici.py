"""
ICICI Bank FD rate scraper.

Scrapes fixed deposit interest rates from the ICICI Bank website.
The page typically shows rate tables organised by tenure buckets with
columns for general customers and senior citizens.

URL: https://www.icicibank.com/personal-banking/deposits/fixed-deposit/fd-interest-rates
"""

from __future__ import annotations

import logging
from typing import Any

import requests
from bs4 import BeautifulSoup, Tag

from backend.scrapers.base_scraper import BaseScraper, USER_AGENT

logger = logging.getLogger(__name__)

ICICI_URL = (
    "https://www.icicibank.com/personal-banking/deposits/"
    "fixed-deposit/fd-interest-rates"
)


class ICICIScraper(BaseScraper):
    """Scraper for ICICI Bank FD interest rates."""

    def scrape(self) -> list[dict[str, Any]]:
        """
        Fetch the ICICI FD rates page and parse rate tables.

        ICICI's FD rate page uses standard HTML tables with structure:

        | Tenure               | General (% p.a.) | Senior Citizens (% p.a.) |
        |----------------------|------------------|--------------------------|
        | 7 days to 14 days    | 3.00             | 3.50                     |
        | ...                  | ...              | ...                      |

        Some pages may use div-based layouts or accordion sections.  We first
        look for ``<table>`` elements; if no rate tables are found we fall
        back to scanning structured ``<div>`` blocks.
        """
        self.logger.info("Fetching ICICI FD rates from %s", ICICI_URL)

        response = requests.get(
            ICICI_URL,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "en-IN,en;q=0.9",
                "Referer": "https://www.icicibank.com/",
            },
            timeout=30,
        )
        response.raise_for_status()

        soup = BeautifulSoup(response.text, "lxml")
        rates: list[dict[str, Any]] = []

        # ── Strategy 1: standard <table> parsing ─────────────────────
        tables = soup.find_all("table")
        self.logger.debug("Found %d tables on ICICI page", len(tables))

        for table in tables:
            if not isinstance(table, Tag):
                continue

            first_row = table.find("tr")
            if not first_row:
                continue
            header_text = first_row.get_text(" ", strip=True).lower()

            has_tenure = any(
                kw in header_text for kw in ("tenure", "period", "maturity", "days")
            )
            has_rate = any(
                kw in header_text for kw in ("rate", "interest", "%", "p.a")
            )
            if not (has_tenure and has_rate):
                continue

            rows = table.find_all("tr")
            ths = rows[0].find_all(["th", "td"])

            # Detect column positions
            tenure_idx = 0
            general_idx = 1
            senior_idx: int | None = None

            for idx, th in enumerate(ths):
                col_text = th.get_text(" ", strip=True).lower()
                if "senior" in col_text:
                    senior_idx = idx
                elif idx > 0 and any(
                    kw in col_text for kw in ("general", "regular", "rate", "interest")
                ):
                    general_idx = idx

            for row in rows[1:]:
                cols = row.find_all(["td", "th"])
                if len(cols) < 2:
                    continue

                tenure_text = cols[tenure_idx].get_text(" ", strip=True)
                if not tenure_text:
                    continue

                # Skip non-data rows
                skip_keywords = [
                    "note", "applicable", "w.e.f", "effective",
                    "disclaimer", "tax", "tds",
                ]
                if any(kw in tenure_text.lower() for kw in skip_keywords):
                    continue

                try:
                    min_d, max_d = self._parse_tenure_days(tenure_text)
                except (ValueError, IndexError):
                    self.logger.debug("Skipping unparseable tenure: %r", tenure_text)
                    continue

                try:
                    gen_rate = self._parse_rate(
                        cols[general_idx].get_text(strip=True)
                    )
                except (ValueError, IndexError):
                    continue

                sr_rate: float | None = None
                if senior_idx is not None and senior_idx < len(cols):
                    try:
                        sr_rate = self._parse_rate(
                            cols[senior_idx].get_text(strip=True)
                        )
                    except ValueError:
                        pass

                rates.append(
                    {
                        "tenure_label": tenure_text,
                        "tenure_min_days": min_d,
                        "tenure_max_days": max_d,
                        "general_rate": gen_rate,
                        "senior_citizen_rate": sr_rate,
                    }
                )

        # ── Strategy 2: div-based fallback ───────────────────────────
        # Some bank pages render rates in <div> grids rather than tables.
        # We look for common CSS class patterns used by ICICI.
        if not rates:
            self.logger.debug("No table-based rates found; trying div fallback")
            rates = self._parse_div_layout(soup)

        # Deduplicate by tenure range
        seen: dict[tuple[int, int], int] = {}
        deduped: list[dict[str, Any]] = []
        for r in rates:
            key = (r["tenure_min_days"], r["tenure_max_days"])
            if key in seen:
                deduped[seen[key]] = r
            else:
                seen[key] = len(deduped)
                deduped.append(r)

        self.logger.info("Parsed %d rate rows from ICICI", len(deduped))
        return deduped

    # ── Private helpers ──────────────────────────────────────────────

    def _parse_div_layout(self, soup: BeautifulSoup) -> list[dict[str, Any]]:
        """
        Fallback parser for div-based rate layouts.

        Scans for elements containing rate-like text patterns and attempts
        to correlate tenure labels with rate values.  This is a best-effort
        heuristic for pages that do not use standard ``<table>`` markup.
        """
        results: list[dict[str, Any]] = []

        # Look for sections/divs that contain "interest rate" in a heading
        sections = soup.find_all(["div", "section"])
        for section in sections:
            heading = section.find(["h2", "h3", "h4"])
            if not heading:
                continue
            heading_text = heading.get_text(strip=True).lower()
            if "interest" not in heading_text and "rate" not in heading_text:
                continue

            # Within this section look for a structured grid of spans / divs
            # containing tenure and rate pairs.
            rows = section.find_all("div", recursive=True)
            for row_div in rows:
                text = row_div.get_text(" ", strip=True)
                # A rate row typically has a tenure phrase and a percentage
                if "%" not in text:
                    continue
                # Try to split into tenure and rate(s)
                parts = [p.strip() for p in text.split("\n") if p.strip()]
                if len(parts) < 2:
                    # Try splitting on whitespace groups
                    import re

                    parts = re.split(r"\s{2,}", text)
                if len(parts) < 2:
                    continue

                tenure_text = parts[0]
                try:
                    min_d, max_d = self._parse_tenure_days(tenure_text)
                    gen_rate = self._parse_rate(parts[1])
                except (ValueError, IndexError):
                    continue

                sr_rate: float | None = None
                if len(parts) >= 3:
                    try:
                        sr_rate = self._parse_rate(parts[2])
                    except ValueError:
                        pass

                results.append(
                    {
                        "tenure_label": tenure_text,
                        "tenure_min_days": min_d,
                        "tenure_max_days": max_d,
                        "general_rate": gen_rate,
                        "senior_citizen_rate": sr_rate,
                    }
                )

        return results
