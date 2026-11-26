"""
SBI (State Bank of India) FD rate scraper.

Scrapes retail domestic term deposit interest rates from the official SBI
website.  The page typically renders a set of HTML tables showing tenure
buckets alongside General and Senior Citizen rates.

URL: https://sbi.co.in/web/interest-rates/deposit-rates/retail-domestic-term-deposits
"""

from __future__ import annotations

import logging
from typing import Any

import requests
from bs4 import BeautifulSoup, Tag

from backend.scrapers.base_scraper import BaseScraper, USER_AGENT

logger = logging.getLogger(__name__)

SBI_URL = (
    "https://sbi.co.in/web/interest-rates/deposit-rates/"
    "retail-domestic-term-deposits"
)


class SBIScraper(BaseScraper):
    """Scraper for State Bank of India FD interest rates."""

    def scrape(self) -> list[dict[str, Any]]:
        """
        Fetch the SBI FD rates page and parse the rate table.

        The SBI page contains one or more ``<table>`` elements with columns
        roughly structured as:

        | Tenure          | General Public (% p.a.) | Senior Citizens (% p.a.) |
        |-----------------|-------------------------|--------------------------|
        | 7 days to 45 days | 3.50                  | 4.00                     |

        We iterate through all tables on the page, identify rate tables by
        their header content, and extract rows.
        """
        self.logger.info("Fetching SBI FD rates from %s", SBI_URL)

        response = requests.get(
            SBI_URL,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "en-IN,en;q=0.9",
            },
            timeout=30,
        )
        response.raise_for_status()

        soup = BeautifulSoup(response.text, "lxml")
        rates: list[dict[str, Any]] = []

        # SBI typically wraps rate data in <table> tags.  We scan all tables
        # and look for ones whose header row mentions "tenure" or "period"
        # alongside "rate" or "interest".
        tables = soup.find_all("table")
        self.logger.debug("Found %d tables on SBI page", len(tables))

        for table in tables:
            if not isinstance(table, Tag):
                continue

            header_text = ""
            header_row = table.find("tr")
            if header_row:
                header_text = header_row.get_text(" ", strip=True).lower()

            # Heuristic: a rate table mentions tenure/period and rate/interest
            is_rate_table = (
                ("tenure" in header_text or "period" in header_text)
                and ("rate" in header_text or "interest" in header_text or "%" in header_text)
            )

            if not is_rate_table:
                continue

            self.logger.debug("Identified SBI rate table: %s…", header_text[:80])

            rows = table.find_all("tr")
            # Determine column layout from the header
            # Common layouts:
            #   [Tenure, General Rate, Senior Citizen Rate]
            #   [Tenure, Rate (General), Rate (Senior Citizen)]
            # We try to detect the senior citizen column dynamically.
            senior_col_idx: int | None = None
            general_col_idx: int = 1  # default: second column

            if rows:
                ths = rows[0].find_all(["th", "td"])
                for idx, th in enumerate(ths):
                    th_text = th.get_text(" ", strip=True).lower()
                    if "senior" in th_text:
                        senior_col_idx = idx
                    elif "general" in th_text or ("rate" in th_text and "senior" not in th_text):
                        general_col_idx = idx

            # Parse data rows (skip the header)
            for row in rows[1:]:
                cols = row.find_all(["td", "th"])
                if len(cols) < 2:
                    continue

                tenure_text = cols[0].get_text(" ", strip=True)
                if not tenure_text or tenure_text.lower().startswith("note"):
                    continue

                # Skip rows that are clearly sub-headers or footnotes
                if any(kw in tenure_text.lower() for kw in ["note", "applicable", "w.e.f", "effective"]):
                    continue

                try:
                    min_days, max_days = self._parse_tenure_days(tenure_text)
                except (ValueError, IndexError):
                    self.logger.debug("Skipping unparseable tenure: %r", tenure_text)
                    continue

                # General rate
                try:
                    general_rate = self._parse_rate(
                        cols[general_col_idx].get_text(strip=True)
                    )
                except (ValueError, IndexError):
                    self.logger.debug(
                        "Skipping row — cannot parse general rate for tenure %r",
                        tenure_text,
                    )
                    continue

                # Senior citizen rate (optional)
                senior_rate: float | None = None
                if senior_col_idx is not None and senior_col_idx < len(cols):
                    try:
                        senior_rate = self._parse_rate(
                            cols[senior_col_idx].get_text(strip=True)
                        )
                    except ValueError:
                        senior_rate = None

                rates.append(
                    {
                        "tenure_label": tenure_text,
                        "tenure_min_days": min_days,
                        "tenure_max_days": max_days,
                        "general_rate": general_rate,
                        "senior_citizen_rate": senior_rate,
                    }
                )

        # Deduplicate: if the same tenure range appears in multiple tables,
        # keep the last occurrence (often the most current).
        seen: dict[tuple[int, int], int] = {}
        deduped: list[dict[str, Any]] = []
        for r in rates:
            key = (r["tenure_min_days"], r["tenure_max_days"])
            if key in seen:
                deduped[seen[key]] = r
            else:
                seen[key] = len(deduped)
                deduped.append(r)

        self.logger.info("Parsed %d rate rows from SBI", len(deduped))
        return deduped
