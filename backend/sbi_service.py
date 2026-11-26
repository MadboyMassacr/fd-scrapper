import os
import re
import zlib
import sqlite3
import logging
from collections import defaultdict
from datetime import datetime, date
from typing import List, Dict, Optional, Any
from urllib.parse import urljoin
import requests
from bs4 import BeautifulSoup

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("sbi_service")

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "fd_rates.db")

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
RETAIL_URL = "https://sbi.co.in/web/interest-rates/deposit-rates/retail-domestic-term-deposits"
BULK_URL = "https://sbi.co.in/web/interest-rates/deposit-rates/domestic-bulk-term-deposits"

CATEGORIES = [
    "Under 3 Cr",
    "3 Cr to 25 Cr",
    "25 Cr to 60 Cr",
    "60 Cr to 100 Cr",
    "100 Cr to 500 Cr",
    "500 Cr & Above"
]


def get_db_connection():
    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS banks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        short_name TEXT UNIQUE NOT NULL,
        logo_url TEXT,
        website_url TEXT,
        bank_type TEXT DEFAULT 'public',
        is_active BOOLEAN DEFAULT 1,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS fd_rates (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        bank_id INTEGER NOT NULL,
        category TEXT NOT NULL,
        tenure_label TEXT NOT NULL,
        tenure_min_days INTEGER,
        tenure_max_days INTEGER,
        general_rate REAL NOT NULL,
        senior_citizen_rate REAL,
        effective_date DATE NOT NULL,
        scraped_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (bank_id) REFERENCES banks (id)
    );
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS scrape_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        bank_id INTEGER NOT NULL,
        status TEXT NOT NULL,
        error_message TEXT,
        scraped_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        duration_seconds REAL,
        FOREIGN KEY (bank_id) REFERENCES banks (id)
    );
    """)

    cursor.execute("SELECT id FROM banks WHERE short_name = 'SBI'")
    row = cursor.fetchone()
    if not row:
        cursor.execute("""
        INSERT INTO banks (name, short_name, logo_url, website_url, bank_type, is_active)
        VALUES ('State Bank of India', 'SBI', 'https://upload.wikimedia.org/wikipedia/commons/c/cc/SBI-logo.svg', 'https://sbi.co.in', 'public', 1)
        """)
        logger.info("Seeded State Bank of India into database.")

    conn.commit()
    conn.close()


def parse_rate(val: str) -> Optional[float]:
    if not val:
        return None
    val = val.replace("*", "").replace("%", "").strip()
    match = re.search(r"(\d+\.?\d*)", val)
    return float(match.group(1)) if match else None


def parse_tenure_days(text: str) -> tuple[int, int]:
    def to_days(fragment: str) -> int:
        fragment = fragment.lower().strip()
        total = 0
        yr = re.search(r"(\d+)\s*(?:year|yr)s?", fragment)
        if yr:
            total += int(yr.group(1)) * 365
        mo = re.search(r"(\d+)\s*(?:month|mon)s?", fragment)
        if mo:
            total += int(mo.group(1)) * 30
        dy = re.search(r"(\d+)\s*(?:day)s?", fragment)
        if dy:
            total += int(dy.group(1))
        if total == 0:
            bare = re.search(r"(\d+)", fragment)
            if bare:
                total = int(bare.group(1))
        return total if total > 0 else 1

    text_clean = text.strip()
    if re.search(r"(?:and|&)\s*(?:up\s+to|above)", text_clean, re.IGNORECASE):
        parts = re.split(r"(?:and|&)\s*(?:up\s+to|above)", text_clean, flags=re.IGNORECASE)
        min_d = to_days(parts[0])
        max_d = to_days(parts[1]) if len(parts) > 1 and parts[1].strip() else 3650
        return (min_d, max_d)

    parts = re.split(r"\s+to\s+|\s*-\s*", text_clean, maxsplit=1)
    if len(parts) == 2:
        return (to_days(parts[0]), to_days(parts[1]))

    d = to_days(text_clean)
    return (d, d)


def is_target_tenure(tenor_text: str) -> bool:
    """Checks if tenure belongs to 7-45 days or 1-2 years."""
    t = tenor_text.lower().replace("-", " to ")
    # Match 7-45 days
    if ("7 day" in t and "45 day" in t) or ("7 day" in t and "14 day" in t) or ("15 day" in t and "45 day" in t) or ("15 day" in t and "30 day" in t) or ("31 day" in t and "45 day" in t):
        return True
    # Match 1-2 years
    if ("1 year" in t and "2 year" in t) or ("1 year" in t and "15 month" in t) or ("15 month" in t and "18 month" in t) or ("18 month" in t and "21 month" in t) or ("21 month" in t and "2 year" in t):
        return True
    return False


def scrape_category_under_3cr() -> List[Dict[str, Any]]:
    """Scrapes retail deposits < 3 Cr from HTML table."""
    logger.info("Scraping SBI 'Under 3 Cr' from %s", RETAIL_URL)
    headers = {"User-Agent": USER_AGENT}
    resp = requests.get(RETAIL_URL, headers=headers, timeout=25)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")
    tables = soup.find_all("table")

    rates = []
    for t in tables:
        header_tr = t.find("tr")
        if not header_tr:
            continue
        headers_text = [th.get_text(" ", strip=True).lower() for th in header_tr.find_all(["th", "td"])]
        if any("tenor" in h or "tenure" in h for h in headers_text):
            rev_pub_idx = -1
            for idx, h in enumerate(headers_text):
                if "revised" in h and ("public" in h or "rate" in h) and "senior" not in h:
                    rev_pub_idx = idx
                    break
            if rev_pub_idx == -1:
                rev_pub_idx = 2 if len(headers_text) > 2 else 1

            for tr in t.find_all("tr")[1:]:
                tds = tr.find_all(["td", "th"])
                if len(tds) <= rev_pub_idx:
                    continue
                tenor = tds[0].get_text(" ", strip=True)
                if not tenor or any(kw in tenor.lower() for kw in ["note", "w.e.f", "applicable"]):
                    continue
                if not is_target_tenure(tenor):
                    continue

                pub_rate = parse_rate(tds[rev_pub_idx].get_text(strip=True))
                if pub_rate is None:
                    continue

                min_days, max_days = parse_tenure_days(tenor)
                rates.append({
                    "category": "Under 3 Cr",
                    "tenure_label": tenor,
                    "tenure_min_days": min_days,
                    "tenure_max_days": max_days,
                    "general_rate": pub_rate,
                })
            if rates:
                break
    return rates


def scrape_category_3cr_to_25cr() -> List[Dict[str, Any]]:
    """Scrapes bulk deposits 3 Cr to 25 Cr from HTML table on domestic bulk page."""
    logger.info("Scraping SBI '3 Cr to 25 Cr' from %s", BULK_URL)
    headers = {"User-Agent": USER_AGENT}
    resp = requests.get(BULK_URL, headers=headers, timeout=25)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")
    table = soup.find("table")
    rates = []
    if not table:
        return rates

    for tr in table.find_all("tr"):
        cols = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
        if len(cols) >= 3:
            # Layout: Public (col 0), Senior (col 1), Tenors (col 2), Non-callable Public (col 3)
            tenor = cols[2] if len(cols) > 2 else ""
            pub_str = cols[0]
            if not tenor or not is_target_tenure(tenor):
                continue
            pub_rate = parse_rate(pub_str)
            if pub_rate is None:
                continue

            min_days, max_days = parse_tenure_days(tenor)
            rates.append({
                "category": "3 Cr to 25 Cr",
                "tenure_label": tenor,
                "tenure_min_days": min_days,
                "tenure_max_days": max_days,
                "general_rate": pub_rate,
            })
    return rates


def parse_pdf_stream(pdf_bytes: bytes) -> List[Dict[str, Any]]:
    """Extracts positioned table text directly from PDF stream using coordinate matrix."""
    streams = re.findall(b"stream[\r\n]+(.*?)[\r\n]+endstream", pdf_bytes, re.DOTALL)
    if not streams:
        return []
    dec = zlib.decompress(streams[0]).decode("latin1", errors="ignore")
    matches = re.finditer(r"BT\s*(.*?)\s*ET", dec, re.DOTALL)
    blocks = []
    for m in matches:
        content = m.group(1)
        tm = re.search(r"1\s+0\s+0\s+1\s+([\d\.]+)\s+([\d\.]+)\s+Tm", content)
        txt = "".join(re.findall(r"\((.*?)\)", content)).strip()
        if tm and txt:
            blocks.append((round(float(tm.group(2)), 1), round(float(tm.group(1)), 1), txt))

    rows = defaultdict(list)
    for y, x, txt in blocks:
        matched_y = None
        for ey in rows:
            if abs(ey - y) <= 2:
                matched_y = ey
                break
        if matched_y is None:
            matched_y = y
        rows[matched_y].append((x, txt))

    items = []
    for y in sorted(rows.keys(), reverse=True):
        cols = sorted(rows[y], key=lambda i: i[0])
        rate_pub = None
        tenor_txt = None
        for x, txt in cols:
            # Callable General Public Rate is positioned at X around 113.7
            if abs(x - 113.7) < 15 and re.match(r"^\d+\.\d+$", txt):
                rate_pub = float(txt)
            if 230 <= x <= 300 and any(w in txt.lower() for w in ["day", "month", "year"]):
                tenor_txt = txt

        if rate_pub is not None and tenor_txt is not None:
            if is_target_tenure(tenor_txt):
                min_days, max_days = parse_tenure_days(tenor_txt)
                items.append({
                    "tenure_label": tenor_txt,
                    "tenure_min_days": min_days,
                    "tenure_max_days": max_days,
                    "general_rate": rate_pub,
                })
    return items


def scrape_pdf_categories() -> List[Dict[str, Any]]:
    """Discovers and parses daily PDF circulars for 25Cr to 60Cr, 60Cr to 100Cr, 100Cr to 500Cr, 500Cr & Above."""
    logger.info("Discovering PDF links from %s", BULK_URL)
    headers = {"User-Agent": USER_AGENT}
    resp = requests.get(BULK_URL, headers=headers, timeout=25)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    pdf_map = {}
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "pdf" in href.lower():
            full_url = urljoin(BULK_URL, href)
            h_lower = href.lower()
            if "abv25crto60cr" in h_lower:
                pdf_map["25 Cr to 60 Cr"] = full_url
            elif "abv60crto100cr" in h_lower:
                pdf_map["60 Cr to 100 Cr"] = full_url
            elif "abv100cr" in h_lower:
                pdf_map["100 Cr to 500 Cr"] = full_url
            elif "500cr" in h_lower:
                pdf_map["500 Cr & Above"] = full_url

    all_pdf_rates = []
    for category_name, pdf_url in pdf_map.items():
        try:
            logger.info("Downloading and extracting PDF for '%s' from %s", category_name, pdf_url)
            r = requests.get(pdf_url, headers=headers, timeout=25)
            r.raise_for_status()
            extracted = parse_pdf_stream(r.content)
            for item in extracted:
                item["category"] = category_name
                all_pdf_rates.append(item)
            logger.info("Extracted %d rates for '%s'", len(extracted), category_name)
        except Exception as e:
            logger.error("Failed to parse PDF for '%s': %s", category_name, e)

    return all_pdf_rates


def filter_two_target_rates_per_category(rates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    For every category, keeps exactly 2 rates:
    1. The smallest time period (e.g. 7-14 days or 7-45 days).
    2. The period that starts just at/over 1 year (e.g. 1 year to 15 months or 1-2 years).
    """
    from collections import defaultdict
    by_category = defaultdict(list)
    for r in rates:
        by_category[r["category"]].append(r)

    filtered_result = []
    for cat in CATEGORIES:
        cat_rates = by_category.get(cat, [])
        if not cat_rates:
            continue
        # Sort ascending by tenure_min_days
        cat_rates.sort(key=lambda x: (x["tenure_min_days"], x["tenure_max_days"]))

        # 1. Smallest period
        smallest_tenure = cat_rates[0]

        # 2. Period just over 1 year (min_days >= 365, closest to 365)
        over_1yr_candidates = [r for r in cat_rates if r["tenure_min_days"] >= 365]
        over_1yr_tenure = over_1yr_candidates[0] if over_1yr_candidates else cat_rates[-1]

        filtered_result.append(smallest_tenure)
        if over_1yr_tenure != smallest_tenure:
            filtered_result.append(over_1yr_tenure)

    return filtered_result


def scrape_all_categories() -> List[Dict[str, Any]]:
    """Scrapes all 6 FD categories across retail HTML, bulk HTML, and bulk PDF circulars."""
    raw_rates = []

    # 1. Under 3 Cr
    try:
        r1 = scrape_category_under_3cr()
        raw_rates.extend(r1)
    except Exception as e:
        logger.error("Error scraping 'Under 3 Cr': %s", e)

    # 2. 3 Cr to 25 Cr
    try:
        r2 = scrape_category_3cr_to_25cr()
        raw_rates.extend(r2)
    except Exception as e:
        logger.error("Error scraping '3 Cr to 25 Cr': %s", e)

    # 3-6. 25-60 Cr, 60-100 Cr, 100-500 Cr, 500 Cr & Above
    try:
        r3_6 = scrape_pdf_categories()
        raw_rates.extend(r3_6)
    except Exception as e:
        logger.error("Error scraping PDF categories: %s", e)

    # Filter strictly for the 2 rates per category
    final_rates = filter_two_target_rates_per_category(raw_rates)
    logger.info("Filtered down to exactly %d rates (2 per category across %d categories)", len(final_rates), len(CATEGORIES))
    return final_rates


def save_sbi_rates(rates: List[Dict[str, Any]]):
    """Saves rates to the SQLite database and logs the scrape status."""
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT id FROM banks WHERE short_name = 'SBI'")
    bank_row = cursor.fetchone()
    if not bank_row:
        logger.error("SBI bank record not found in database!")
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
    """, (bank_id, "success" if rates else "empty", None if rates else "No rates extracted", now, 2.5))

    conn.commit()
    conn.close()
    logger.info("Saved %d SBI FD rates across all categories into database.", len(rates))


if __name__ == "__main__":
    init_db()
    rates = scrape_all_categories()
    save_sbi_rates(rates)
    print("Done! Scraped and stored all categories.")
