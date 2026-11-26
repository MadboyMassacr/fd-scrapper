import re
import logging
from datetime import datetime, date
from typing import List, Dict, Any, Optional
import requests
from bs4 import BeautifulSoup

from backend.sbi_service import get_db_connection, parse_tenure_days

logger = logging.getLogger("bob_service")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)
BOB_UPTO_10CR_URL = (
    "https://www.bankofbaroda.in/interest-rate-and-service-charges/"
    "deposits-interest-rates/fixed-deposits-callable-and-non-callable-upto-ten-crores"
)
BOB_ABOVE_10CR_URL = (
    "https://www.bankofbaroda.in/interest-rate-and-service-charges/"
    "deposits-interest-rates/fixed-deposits-callable-and-non-callable-above-ten-crores"
)

# Original Bank of Baroda deposit classifications
BOB_3_TO_10_CR_CATEGORIES = [
    (1, "3 Cr to 4 Cr"),
    (2, "Above 4 Cr to 5 Cr"),
    (3, "Above 5 Cr to 6 Cr"),
    (4, "Above 6 Cr to 7 Cr"),
    (5, "Above 7 Cr to 8 Cr"),
    (6, "Above 8 Cr to 9 Cr"),
    (7, "Above 9 Cr to 10 Cr"),
]

BOB_ABOVE_10_CR_CATEGORIES = [
    (1, "Above 10 Cr to 25 Cr"),
    (2, "Above 25 Cr to 50 Cr"),
    (3, "Above 50 Cr to 100 Cr"),
    (4, "Above 100 Cr to 250 Cr"),
    (5, "Above 250 Cr to 500 Cr"),
    (6, "Above 500 Cr to 750 Cr"),
    (7, "Above 750 Cr to 1000 Cr"),
    (8, "Above 1000 Cr"),
]


def init_bob_bank():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM banks WHERE short_name = 'BOB'")
    row = cursor.fetchone()
    if not row:
        cursor.execute("""
        INSERT INTO banks (name, short_name, logo_url, website_url, bank_type, is_active)
        VALUES ('Bank of Baroda', 'BOB', 'https://upload.wikimedia.org/wikipedia/commons/2/23/Bank_of_Baroda_logo.svg', 'https://www.bankofbaroda.in', 'public', 1)
        """)
        conn.commit()
        logger.info("Seeded Bank of Baroda into database.")
    conn.close()


def parse_rate(val: str) -> Optional[float]:
    if not val:
        return None
    val = val.replace("*", "").replace("$", "").replace("#", "").replace("%", "").strip()
    match = re.search(r"(\d+\.?\d*)", val)
    return float(match.group(1)) if match else None


def scrape_bob_all_categories() -> List[Dict[str, Any]]:
    """
    Scrapes Bank of Baroda rates using BoB's own original deposit classifications,
    keeping 2 rates per category: shortest period (7-14 days) and 1 year & above.
    """
    logger.info("Scraping Bank of Baroda rates from official portals...")
    headers = {"User-Agent": USER_AGENT}
    rates: List[Dict[str, Any]] = []

    # 1. Page 1: Up to 10 Crores (Table 0 = Under 3 Cr, Table 1 = 3 Cr to 10 Cr)
    try:
        r1 = requests.get(BOB_UPTO_10CR_URL, headers=headers, timeout=20)
        r1.raise_for_status()
        s1 = BeautifulSoup(r1.text, "html.parser")
        tables1 = s1.find_all("table")

        # Table 0: Under 3 Cr
        if len(tables1) >= 1:
            r_short = None
            r_1yr = None
            for tr in tables1[0].find_all("tr"):
                cols = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
                if len(cols) >= 2:
                    tenor = cols[0]
                    rate_val = parse_rate(cols[1])
                    if rate_val is None:
                        continue
                    if "7 days to 14 days" in tenor.lower() and r_short is None:
                        min_d, max_d = parse_tenure_days(tenor)
                        r_short = {
                            "category": "Under 3 Cr",
                            "tenure_label": tenor,
                            "tenure_min_days": min_d,
                            "tenure_max_days": max_d,
                            "general_rate": rate_val,
                        }
                    elif (tenor.strip().lower() == "1 year" or "above 1 year" in tenor.lower()) and r_1yr is None:
                        min_d, max_d = parse_tenure_days(tenor)
                        r_1yr = {
                            "category": "Under 3 Cr",
                            "tenure_label": tenor,
                            "tenure_min_days": min_d,
                            "tenure_max_days": max_d,
                            "general_rate": rate_val,
                        }
            if r_short:
                rates.append(r_short)
            if r_1yr:
                rates.append(r_1yr)

        # Table 1: 3 Cr to 10 Cr (7 original 1-Cr slabs)
        if len(tables1) >= 2:
            cat_3_10 = {cat: {} for _, cat in BOB_3_TO_10_CR_CATEGORIES}
            for tr in tables1[1].find_all("tr")[2:]:
                cols = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
                if len(cols) >= 8:
                    tenor = cols[0]
                    t_low = tenor.strip().lower()
                    is_short = "7d to 14d" in t_low or "7 days to 14 days" in t_low
                    is_1yr = t_low == "1 year"
                    if not (is_short or is_1yr):
                        continue

                    min_d, max_d = parse_tenure_days(tenor)
                    display_tenor = "7 days to 14 days" if is_short else "1 Year"

                    for col_idx, cat in BOB_3_TO_10_CR_CATEGORIES:
                        rate_val = parse_rate(cols[col_idx])
                        if rate_val is None:
                            continue
                        if is_short and "short" not in cat_3_10[cat]:
                            cat_3_10[cat]["short"] = {
                                "category": cat,
                                "tenure_label": display_tenor,
                                "tenure_min_days": min_d,
                                "tenure_max_days": max_d,
                                "general_rate": rate_val,
                            }
                        elif is_1yr and "1yr" not in cat_3_10[cat]:
                            cat_3_10[cat]["1yr"] = {
                                "category": cat,
                                "tenure_label": display_tenor,
                                "tenure_min_days": min_d,
                                "tenure_max_days": max_d,
                                "general_rate": rate_val,
                            }

            for _, cat in BOB_3_TO_10_CR_CATEGORIES:
                if "short" in cat_3_10[cat]:
                    rates.append(cat_3_10[cat]["short"])
                if "1yr" in cat_3_10[cat]:
                    rates.append(cat_3_10[cat]["1yr"])

    except Exception as e:
        logger.error("Error scraping BoB Up to 10 Cr categories: %s", e)

    # 2. Page 2: Above 10 Crores (8 original slabs up to Above 1000 Cr)
    try:
        r2 = requests.get(BOB_ABOVE_10CR_URL, headers=headers, timeout=20)
        r2.raise_for_status()
        s2 = BeautifulSoup(r2.text, "html.parser")
        t_above10 = s2.find_all("table")[0]
        rows = t_above10.find_all("tr")

        cat_results = {cat: {} for _, cat in BOB_ABOVE_10_CR_CATEGORIES}

        for tr in rows[2:]:
            cols = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
            if len(cols) >= 9:
                tenor = cols[0]
                t_low = tenor.strip().lower()
                is_short = "7 days to 14 days" in t_low
                is_1yr = t_low == "1 year" or "above 1 year" in t_low
                if not (is_short or is_1yr):
                    continue

                min_d, max_d = parse_tenure_days(tenor)

                for col_idx, cat in BOB_ABOVE_10_CR_CATEGORIES:
                    rate_val = parse_rate(cols[col_idx])
                    if rate_val is None:
                        continue
                    if is_short and "short" not in cat_results[cat]:
                        cat_results[cat]["short"] = {
                            "category": cat,
                            "tenure_label": tenor,
                            "tenure_min_days": min_d,
                            "tenure_max_days": max_d,
                            "general_rate": rate_val,
                        }
                    elif is_1yr and "1yr" not in cat_results[cat]:
                        cat_results[cat]["1yr"] = {
                            "category": cat,
                            "tenure_label": tenor,
                            "tenure_min_days": min_d,
                            "tenure_max_days": max_d,
                            "general_rate": rate_val,
                        }

        for _, cat in BOB_ABOVE_10_CR_CATEGORIES:
            if "short" in cat_results[cat]:
                rates.append(cat_results[cat]["short"])
            if "1yr" in cat_results[cat]:
                rates.append(cat_results[cat]["1yr"])

    except Exception as e:
        logger.error("Error scraping BoB Above 10 Cr categories: %s", e)

    logger.info("Scraped %d rates for Bank of Baroda across all original categories", len(rates))
    return rates


def save_bob_rates(rates: List[Dict[str, Any]]):
    """Saves Bank of Baroda rates to SQLite database."""
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT id FROM banks WHERE short_name = 'BOB'")
    bank_row = cursor.fetchone()
    if not bank_row:
        logger.error("BOB bank record not found in database!")
        conn.close()
        return

    bank_id = bank_row["id"]
    today = date.today().isoformat()
    now = datetime.now().isoformat()

    cursor.execute("DELETE FROM fd_rates WHERE bank_id = ? AND effective_date = ?", (bank_id, today))

    for r in rates:
        cursor.execute("""
        INSERT INTO fd_rates (bank_id, category, tenure_label, tenure_min_days, tenure_max_days, general_rate, senior_citizen_rate, effective_date, scraped_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            bank_id,
            r["category"],
            r["tenure_label"],
            r["tenure_min_days"],
            r["tenure_max_days"],
            r["general_rate"],
            None,
            today,
            now
        ))

    cursor.execute("""
    INSERT INTO scrape_logs (bank_id, status, error_message, scraped_at, duration_seconds)
    VALUES (?, ?, ?, ?, ?)
    """, (bank_id, "success" if rates else "empty", None if rates else "No rates extracted", now, 2.0))

    conn.commit()
    conn.close()
    logger.info("Saved %d Bank of Baroda FD rates into database.", len(rates))


if __name__ == "__main__":
    init_bob_bank()
    rates = scrape_bob_all_categories()
    save_bob_rates(rates)
    print("Done! Scraped and stored Bank of Baroda rates.")
