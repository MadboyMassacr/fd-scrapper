import re
import logging
from datetime import datetime, date
from typing import List, Dict, Any, Optional
import requests
from bs4 import BeautifulSoup

from backend.sbi_service import get_db_connection, parse_tenure_days

logger = logging.getLogger("ib_service")

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
IB_RETAIL_URL = "https://indianbank.bank.in/en/deposit-rates"
IB_BULK_URL = "https://indianbank.bank.in/en/interest-rates-for-deposits-of-above-rs.5.00crore"

IB_BULK_CATEGORIES = [
    (1, "Above 3 Cr to 5 Cr"),
    (2, "Above 5 Cr to 25 Cr"),
    (3, "Above 25 Cr to 100 Cr"),
    (4, "Above 100 Cr to 500 Cr"),
    (5, "Above 500 Cr to 1000 Cr"),
    (6, "Above 1000 Cr"),
]


def init_ib_bank():
    """Seed Indian Bank into the banks table if not already present."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM banks WHERE short_name = 'INDB'")
    row = cursor.fetchone()
    if not row:
        cursor.execute("""
        INSERT INTO banks (name, short_name, logo_url, website_url, bank_type, is_active)
        VALUES (
            'Indian Bank',
            'INDB',
            'https://upload.wikimedia.org/wikipedia/commons/e/e1/Indian_Bank_logo.svg',
            'https://indianbank.bank.in',
            'public',
            1
        )
        """)
        conn.commit()
        logger.info("Seeded Indian Bank into database.")
    conn.close()


def parse_rate(val: str) -> Optional[float]:
    if not val:
        return None
    val = val.replace("*", "").replace("%", "").strip()
    match = re.search(r"(\d+\.?\d*)", val)
    return float(match.group(1)) if match else None


def scrape_ib_all_categories() -> List[Dict[str, Any]]:
    """
    Scrape Indian Bank FD rates across its original classifications:
    - Under 3 Cr (Retail Term Deposits table on /en/deposit-rates)
    - Above 3 Cr to 5 Cr, Above 5 Cr to 25 Cr, Above 25 Cr to 100 Cr,
      Above 100 Cr to 500 Cr, Above 500 Cr to 1000 Cr, Above 1000 Cr
      (Callable Bulk Deposits table on /en/interest-rates-for-deposits-of-above-rs.5.00crore)
    """
    logger.info("Scraping Indian Bank rates...")
    headers = {"User-Agent": USER_AGENT}
    rates: List[Dict[str, Any]] = []

    # 1. Under 3 Cr from /en/deposit-rates (Table 0, Revised Rate col index 2)
    try:
        r1 = requests.get(IB_RETAIL_URL, headers=headers, timeout=20)
        r1.raise_for_status()
        s1 = BeautifulSoup(r1.text, "html.parser")
        tables1 = s1.find_all("table")
        if tables1:
            r_short = None
            r_1yr = None
            for tr in tables1[0].find_all("tr"):
                cols = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
                if len(cols) < 3:
                    continue
                tenor = cols[0]
                t_low = tenor.lower().strip()
                rate_val = parse_rate(cols[2])
                if rate_val is None:
                    continue

                if ("7 days to 14" in t_low or ("7" in t_low and "14" in t_low and "day" in t_low)) and r_short is None:
                    min_d, max_d = parse_tenure_days(tenor)
                    r_short = {
                        "category": "Under 3 Cr",
                        "tenure_label": tenor,
                        "tenure_min_days": min_d,
                        "tenure_max_days": max_d,
                        "general_rate": rate_val,
                    }
                elif t_low == "1 year" and r_1yr is None:
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
    except Exception as e:
        logger.error("Error scraping Indian Bank 'Under 3 Cr': %s", e)

    # 2. Bulk categories from /en/interest-rates-for-deposits-of-above-rs.5.00crore (Table 0 = Callable)
    try:
        r2 = requests.get(IB_BULK_URL, headers=headers, timeout=20)
        r2.raise_for_status()
        s2 = BeautifulSoup(r2.text, "html.parser")
        tables2 = s2.find_all("table")
        if tables2:
            cat_results = {cat: {} for _, cat in IB_BULK_CATEGORIES}
            for tr in tables2[0].find_all("tr"):
                cols = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
                if len(cols) < 7:
                    continue
                tenor = cols[0]
                t_low = tenor.lower().strip()
                is_short = "7" in t_low and "14" in t_low and "day" in t_low
                is_1yr = t_low == "1 year"
                if not (is_short or is_1yr):
                    continue

                min_d, max_d = parse_tenure_days(tenor)
                clean_tenor = "7 days to 14 days" if is_short else "1 year"

                for col_idx, cat in IB_BULK_CATEGORIES:
                    rate_val = parse_rate(cols[col_idx])
                    if rate_val is None:
                        continue
                    if is_short and "short" not in cat_results[cat]:
                        cat_results[cat]["short"] = {
                            "category": cat,
                            "tenure_label": clean_tenor,
                            "tenure_min_days": min_d,
                            "tenure_max_days": max_d,
                            "general_rate": rate_val,
                        }
                    elif is_1yr and "1yr" not in cat_results[cat]:
                        cat_results[cat]["1yr"] = {
                            "category": cat,
                            "tenure_label": clean_tenor,
                            "tenure_min_days": min_d,
                            "tenure_max_days": max_d,
                            "general_rate": rate_val,
                        }

            for _, cat in IB_BULK_CATEGORIES:
                if "short" in cat_results[cat]:
                    rates.append(cat_results[cat]["short"])
                if "1yr" in cat_results[cat]:
                    rates.append(cat_results[cat]["1yr"])
    except Exception as e:
        logger.error("Error scraping Indian Bank bulk categories: %s", e)

    logger.info("Scraped %d rates for Indian Bank", len(rates))
    return rates


def save_ib_rates(rates: List[Dict[str, Any]]):
    """Save Indian Bank rates to SQLite database."""
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT id FROM banks WHERE short_name = 'INDB'")
    bank_row = cursor.fetchone()
    if not bank_row:
        logger.error("INDB bank record not found in database!")
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
            now,
        ))

    cursor.execute("""
    INSERT INTO scrape_logs (bank_id, status, error_message, scraped_at, duration_seconds)
    VALUES (?, ?, ?, ?, ?)
    """, (bank_id, "success" if rates else "empty", None if rates else "No rates extracted", now, 1.5))

    conn.commit()
    conn.close()
    logger.info("Saved %d Indian Bank FD rates into database.", len(rates))


if __name__ == "__main__":
    init_ib_bank()
    rates = scrape_ib_all_categories()
    save_ib_rates(rates)
    print(f"Done! Scraped and stored {len(rates)} Indian Bank rates.")
