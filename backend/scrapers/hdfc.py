"""
HDFC Bank FD rate scraper.

Scrapes fixed deposit interest rates from the HDFC Bank website.
The page presents FD rates in HTML tables segregated by customer type
(regular vs. senior citizens) and tenure buckets.

URL: https://www.hdfcbank.com/personal/resources/learning-centre/save/fixed-deposit-interest-rate
"""

from __future__ import annotations

import logging
from typing import Any

import requests
from bs4 import BeautifulSoup, Tag

from backend.scrapers.base_scraper import BaseScraper, USER_AGENT

logger = logging.getLogger(__name__)

HDFC_URL = (
    "https://www.hdfcbank.com/personal/resources/learning-centre/"
    "save/fixed-deposit-interest-rate"
)


class HDFCScraper(BaseScraper):
    """Scraper for HDFC Bank FD interest rates."""

    def scrape(self) -> list[dict[str, Any]]:
        """
        Fetch the HDFC FD rates page (static HTML) and parse the rate tables.

        HDFC's page typically contains multiple tables.  The main FD rate table
        has columns for:
        - Tenure / Period
        - Rate for General Public (% p.a.)
        - Rate for Senior Citizens (% p.a.)

        Some pages split General and Senior Citizen tables separately.  We
        handle both layouts:
        1. **Combined table** — a single table with tenure, general rate, and
           senior citizen rate columns.
        2. **Separate tables** — we correlate rows by tenure text to merge
           general and senior citizen rates.
        """
        self.logger.info("Fetching HDFC FD rates from %s", HDFC_URL)

        response = requests.get(
            HDFC_URL,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "en-IN,en;q=0.9",
                "Referer": "https://www.hdfcbank.com/",
            },
            timeout=30,
        )
        response.raise_for_status()

        soup = BeautifulSoup(response.text, "lxml")
        rates: list[dict[str, Any]] = []

        # Strategy: find all tables and classify them
        tables = soup.find_all("table")
        self.logger.debug("Found %d tables on HDFC page", len(tables))

        general_map: dict[str, dict[str, Any]] = {}
        senior_map: dict[str, float] = {}

        for table in tables:
            if not isinstance(table, Tag):
                continue

            # Read header to determine table type
            first_row = table.find("tr")
            if not first_row:
                continue
            header_text = first_row.get_text(" ", strip=True).lower()

            # Must look like a rate table
            has_tenure = "tenure" in header_text or "period" in header_text
            has_rate = "rate" in header_text or "interest" in header_text or "%" in header_text
            if not (has_tenure and has_rate):
                continue

            is_senior_table = "senior" in header_text
            is_combined = "senior" in header_text and "general" in header_text

            rows = table.find_all("tr")

            # Detect column indices from header
            ths = rows[0].find_all(["th", "td"])
            tenure_idx = 0
            general_idx: int | None = None
            senior_idx: int | None = None
            rate_idx = 1  # fallback: second column is the rate

            for idx, th in enumerate(ths):
                col_text = th.get_text(" ", strip=True).lower()
                if "senior" in col_text:
                    senior_idx = idx
                elif "general" in col_text or "regular" in col_text:
                    general_idx = idx
                elif ("rate" in col_text or "interest" in col_text) and idx > 0:
                    rate_idx = idx

            # If it's a combined table with both columns identified
            if is_combined and general_idx is not None and senior_idx is not None:
                self._parse_combined_table(rows[1:], tenure_idx, general_idx, senior_idx, rates)
                continue

            # Otherwise treat as single-rate-column table
            effective_rate_idx = general_idx if general_idx is not None else rate_idx
            for row in rows[1:]:
                cols = row.find_all(["td", "th"])
                if len(cols) < 2:
                    continue

                tenure_text = cols[tenure_idx].get_text(" ", strip=True)
                if not tenure_text or any(
                    kw in tenure_text.lower()
                    for kw in ["note", "applicable", "w.e.f", "effective", "disclaimer"]
                ):
                    continue

                try:
                    rate_val = self._parse_rate(cols[effective_rate_idx].get_text(strip=True))
                except (ValueError, IndexError):
                    continue

                if is_senior_table:
                    senior_map[tenure_text] = rate_val
                else:
                    try:
                        min_d, max_d = self._parse_tenure_days(tenure_text)
                    except ValueError:
                        continue
                    general_map[tenure_text] = {
                        "tenure_label": tenure_text,
                        "tenure_min_days": min_d,
                        "tenure_max_days": max_d,
                        "general_rate": rate_val,
                        "senior_citizen_rate": None,
                    }

                    # If there's a senior column in this same table row, grab it
                    if senior_idx is not None and senior_idx < len(cols):
                        try:
                            sr = self._parse_rate(cols[senior_idx].get_text(strip=True))
                            general_map[tenure_text]["senior_citizen_rate"] = sr
                        except ValueError:
                            pass

        # Merge separate senior table data into general map
        for tenure_text, sr_rate in senior_map.items():
            if tenure_text in general_map:
                general_map[tenure_text]["senior_citizen_rate"] = sr_rate

        # If we used the combined-table path, rates is already populated
        if not rates:
            rates = list(general_map.values())

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

        self.logger.info("Parsed %d rate rows from HDFC", len(deduped))
        return deduped

    # ── Private helpers ──────────────────────────────────────────────

    def _parse_combined_table(
        self,
        rows: list[Tag],
        tenure_idx: int,
        general_idx: int,
        senior_idx: int,
        results: list[dict[str, Any]],
    ) -> None:
        """Parse rows from a combined general + senior citizen rate table."""
        for row in rows:
            cols = row.find_all(["td", "th"])
            if len(cols) < max(tenure_idx, general_idx, senior_idx) + 1:
                continue

            tenure_text = cols[tenure_idx].get_text(" ", strip=True)
            if not tenure_text or any(
                kw in tenure_text.lower()
                for kw in ["note", "applicable", "w.e.f", "effective"]
            ):
                continue

            try:
                min_d, max_d = self._parse_tenure_days(tenure_text)
                gen_rate = self._parse_rate(cols[general_idx].get_text(strip=True))
            except (ValueError, IndexError):
                continue

            sr_rate: float | None = None
            try:
                sr_rate = self._parse_rate(cols[senior_idx].get_text(strip=True))
            except (ValueError, IndexError):
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
