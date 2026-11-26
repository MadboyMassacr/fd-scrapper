import re
import zlib
import logging
from collections import defaultdict
from datetime import datetime, date
from typing import List, Dict, Any, Optional
from urllib.parse import urljoin
import requests
from bs4 import BeautifulSoup

from backend.sbi_service import get_db_connection, parse_tenure_days

logger = logging.getLogger("ubi_service")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)
UBI_RETAIL_URL = "https://www.unionbankofindia.bank.in/en/details/rate-of-interest"
UBI_DEFAULT_BULK_PDF_URL = "https://www.unionbankofindia.bank.in/pdf/interest-rates-for-bulk-deposit.pdf"

UBI_BULK_CATEGORIES = [
    "3 Cr to 10 Cr",
    "Above 10 Cr to 25 Cr",
    "Above 25 Cr to 50 Cr",
    "Above 50 Cr to 100 Cr",
    "Above 100 Cr to 500 Cr",
    "Above 500 Cr",
]


def init_ubi_bank():
    """Seed Union Bank of India into the banks table if not already present."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM banks WHERE short_name = 'UBI'")
    row = cursor.fetchone()
    if not row:
        cursor.execute("""
        INSERT INTO banks (name, short_name, logo_url, website_url, bank_type, is_active)
        VALUES (
            'Union Bank of India',
            'UBI',
            'https://upload.wikimedia.org/wikipedia/commons/d/d0/Union_Bank_of_India_Logo.svg',
            'https://www.unionbankofindia.bank.in',
            'public',
            1
        )
        """)
        conn.commit()
        logger.info("Seeded Union Bank of India into database.")
    conn.close()


def parse_rate(val: str) -> Optional[float]:
    if not val:
        return None
    val = val.replace("*", "").replace("%", "").strip()
    match = re.search(r"(\d+\.?\d*)", val)
    return float(match.group(1)) if match else None


def _build_pdf_cmap(streams: list[bytes]) -> dict[str, str]:
    """Build a ToUnicode CMap lookup from PDF streams for CIDFont text decoding."""
    cmap: dict[str, str] = {}
    for s in streams:
        for loader in (zlib.decompress, lambda b: b):
            try:
                dec = loader(s).decode("latin1", errors="ignore")
            except Exception:
                continue
            if "beginbfchar" not in dec and "beginbfrange" not in dec:
                continue
            for block in re.findall(r"beginbfchar(.*?)endbfchar", dec, re.DOTALL):
                for src, dst in re.findall(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", block):
                    cmap[src.upper()] = bytes.fromhex(dst).decode("utf-16-be", errors="ignore")
            for block in re.findall(r"beginbfrange(.*?)endbfrange", dec, re.DOTALL):
                for st, en, dst in re.findall(
                    r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", block
                ):
                    s_int, e_int, d_int = int(st, 16), int(en, 16), int(dst, 16)
                    for offset in range(e_int - s_int + 1):
                        cmap[f"{s_int + offset:04X}"] = chr(d_int + offset)
    return cmap


def _parse_ubi_bulk_pdf(pdf_bytes: bytes) -> List[Dict[str, Any]]:
    """
    Parse Union Bank of India's bulk deposit PDF (Callable Deposit table)
    and return shortest-period (7-14 Days) and 1-year (1 Year) rates across all 6 bulk slabs.
    """
    streams = re.findall(b"stream[\r\n]+(.*?)[\r\n]+endstream", pdf_bytes, re.DOTALL)
    cmap = _build_pdf_cmap(streams)
    if not cmap:
        logger.warning("No ToUnicode CMap found in UBI bulk PDF.")
        return []

    rows_by_y: dict[float, list[tuple[float, str]]] = defaultdict(list)
    for s in streams:
        try:
            dec = zlib.decompress(s).decode("latin1", errors="ignore")
        except Exception:
            continue
        if "BT" not in dec or "Tm" not in dec:
            continue

        for block in re.findall(r"BT\s*(.*?)\s*ET", dec, re.DOTALL):
            tm = re.search(r"([\d\.]+)\s+([\d\.]+)\s+Tm", block)
            if not tm:
                continue
            x, y = float(tm.group(1)), float(tm.group(2))
            hxs = re.findall(r"<([0-9A-Fa-f]+)>", block)
            txt = "".join(
                cmap.get(hx[k : k + 4].upper(), "")
                for hx in hxs
                for k in range(0, len(hx), 4)
            ).strip()
            if txt:
                rows_by_y[round(y, 0)].append((x, txt))

    # Callable Deposit section appears above Notice Period Deposit (Y < 450)
    rates: List[Dict[str, Any]] = []
    found_short = False
    found_1yr = False

    for y in sorted(rows_by_y.keys()):
        if y >= 450:
            continue
        cols = sorted(rows_by_y[y], key=lambda c: c[0])
        tenor_parts = [txt for x, txt in cols if x < 200]
        if not tenor_parts:
            continue
        tenor_label = " ".join(tenor_parts).strip()
        t_low = tenor_label.lower()

        is_short = "7-14" in t_low or ("7" in t_low and "14" in t_low and "day" in t_low)
        is_1yr = t_low == "1 year"
        if not (is_short or is_1yr):
            continue
        if is_short and found_short:
            continue
        if is_1yr and found_1yr:
            continue

        # Merge split fragments like '6.' + '35' within the rate columns (x >= 200)
        rate_tokens: list[tuple[float, str]] = []
        for x, txt in cols:
            if x < 200:
                continue
            if rate_tokens and (x - rate_tokens[-1][0] < 20) and rate_tokens[-1][1].endswith("."):
                prev_x, prev_txt = rate_tokens[-1]
                rate_tokens[-1] = (prev_x, prev_txt + txt)
            else:
                rate_tokens.append((x, txt))

        joined_rates = " ".join(txt for _, txt in rate_tokens)
        col_values = [float(m) for m in re.findall(r"\d+\.\d+", joined_rates)]
        if len(col_values) < len(UBI_BULK_CATEGORIES):
            logger.warning(
                "Expected %d rate columns for UBI tenor %r, got %d (%r)",
                len(UBI_BULK_CATEGORIES), tenor_label, len(col_values), joined_rates,
            )
            continue

        min_d, max_d = parse_tenure_days(tenor_label)
        for idx, cat in enumerate(UBI_BULK_CATEGORIES):
            rates.append({
                "category": cat,
                "tenure_label": tenor_label,
                "tenure_min_days": min_d,
                "tenure_max_days": max_d,
                "general_rate": col_values[idx],
            })

        if is_short:
            found_short = True
        if is_1yr:
            found_1yr = True

    return rates


def scrape_ubi_all_categories() -> List[Dict[str, Any]]:
    """
    Scrape Union Bank of India FD rates across its original classifications:
    - Under 3 Cr (HTML table on rate-of-interest page)
    - 3 Cr to 10 Cr, Above 10 Cr to 25 Cr, Above 25 Cr to 50 Cr,
      Above 50 Cr to 100 Cr, Above 100 Cr to 500 Cr, Above 500 Cr (Bulk Deposit PDF)
    """
    logger.info("Scraping Union Bank of India rates from %s", UBI_RETAIL_URL)
    headers = {"User-Agent": USER_AGENT}
    rates: List[Dict[str, Any]] = []
    bulk_pdf_url = UBI_DEFAULT_BULK_PDF_URL

    # 1. Under 3 Cr from HTML table + discover bulk PDF link
    try:
        resp = requests.get(UBI_RETAIL_URL, headers=headers, timeout=20)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        for a in soup.find_all("a", href=True):
            if "bulk" in a["href"].lower() and ".pdf" in a["href"].lower():
                bulk_pdf_url = urljoin(UBI_RETAIL_URL, a["href"])
                break

        tables = soup.find_all("table")
        if tables:
            r_short = None
            r_1yr = None
            for tr in tables[0].find_all("tr"):
                cols = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
                if len(cols) < 2:
                    continue
                tenor = cols[0]
                t_low = tenor.lower()
                rate_val = parse_rate(cols[1])
                if rate_val is None:
                    continue

                if ("7-14" in t_low or ("7" in t_low and "14" in t_low and "day" in t_low)) and r_short is None:
                    min_d, max_d = parse_tenure_days(tenor)
                    r_short = {
                        "category": "Under 3 Cr",
                        "tenure_label": tenor,
                        "tenure_min_days": min_d,
                        "tenure_max_days": max_d,
                        "general_rate": rate_val,
                    }
                elif ("1 yr" in t_low or "1 year" in t_low) and not t_low.startswith(">") and r_1yr is None:
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
        logger.error("Error scraping UBI 'Under 3 Cr': %s", e)

    # 2. Bulk Deposit PDF (3 Cr & Above)
    try:
        logger.info("Downloading UBI bulk deposit PDF from %s", bulk_pdf_url)
        pdf_resp = requests.get(bulk_pdf_url, headers=headers, timeout=25)
        pdf_resp.raise_for_status()
        bulk_rates = _parse_ubi_bulk_pdf(pdf_resp.content)
        rates.extend(bulk_rates)
    except Exception as e:
        logger.error("Error scraping UBI bulk PDF: %s", e)

    logger.info("Scraped %d rates for Union Bank of India", len(rates))
    return rates


def save_ubi_rates(rates: List[Dict[str, Any]]):
    """Save Union Bank of India rates to SQLite database."""
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT id FROM banks WHERE short_name = 'UBI'")
    bank_row = cursor.fetchone()
    if not bank_row:
        logger.error("UBI bank record not found in database!")
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
    """, (bank_id, "success" if rates else "empty", None if rates else "No rates extracted", now, 2.0))

    conn.commit()
    conn.close()
    logger.info("Saved %d Union Bank of India FD rates into database.", len(rates))


if __name__ == "__main__":
    init_ubi_bank()
    rates = scrape_ubi_all_categories()
    save_ubi_rates(rates)
    print(f"Done! Scraped and stored {len(rates)} Union Bank of India rates.")
