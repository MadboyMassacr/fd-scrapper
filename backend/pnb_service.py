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

logger = logging.getLogger("pnb_service")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)
PNB_DEPOSIT_URL = "https://pnb.bank.in/interest-rates-deposit.html"
PNB_BULK_PAGE_URL = "https://pnb.bank.in/interest-rates-bulk-deposit.html"

PNB_BULK_CATEGORIES = [
    "Above 10 Cr to 25 Cr",
    "Above 25 Cr to 100 Cr",
    "Above 100 Cr to 500 Cr",
    "Above 500 Cr to 1000 Cr",
    "Above 1000 Cr",
]


def init_pnb_bank():
    """Seed Punjab National Bank into the banks table if not already present."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM banks WHERE short_name = 'PNB'")
    row = cursor.fetchone()
    if not row:
        cursor.execute("""
        INSERT INTO banks (name, short_name, logo_url, website_url, bank_type, is_active)
        VALUES (
            'Punjab National Bank',
            'PNB',
            'https://upload.wikimedia.org/wikipedia/commons/b/b2/Punjab_National_Bank_new_logo.svg',
            'https://pnb.bank.in',
            'public',
            1
        )
        """)
        conn.commit()
        logger.info("Seeded Punjab National Bank into database.")
    conn.close()


def parse_rate(val: str) -> Optional[float]:
    if not val:
        return None
    val = val.replace("*", "").replace("%", "").strip()
    match = re.search(r"(\d+\.?\d*)", val)
    return float(match.group(1)) if match else None


def _build_pdf_cmap(streams: list[bytes]) -> dict[str, str]:
    """Build a ToUnicode CMap lookup from PDF streams."""
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


def _extract_table_short_and_1yr(table, category_name: str, tenor_col: int, rate_col: int) -> List[Dict[str, Any]]:
    """Extract shortest period (7-14 days) and 1 Year rate from a PNB HTML table."""
    r_short = None
    r_1yr = None
    for tr in table.find_all("tr"):
        cols = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
        if len(cols) <= max(tenor_col, rate_col):
            continue
        tenor = cols[tenor_col]
        t_low = tenor.lower().strip()
        rate_val = parse_rate(cols[rate_col])
        if rate_val is None:
            continue

        if ("7" in t_low and "14" in t_low and "day" in t_low) and r_short is None:
            min_d, max_d = parse_tenure_days(tenor)
            r_short = {
                "category": category_name,
                "tenure_label": tenor,
                "tenure_min_days": min_d,
                "tenure_max_days": max_d,
                "general_rate": rate_val,
            }
        elif t_low in ("1 year", "1 yr") and r_1yr is None:
            min_d, max_d = parse_tenure_days(tenor)
            r_1yr = {
                "category": category_name,
                "tenure_label": tenor,
                "tenure_min_days": min_d,
                "tenure_max_days": max_d,
                "general_rate": rate_val,
            }

    out = []
    if r_short:
        out.append(r_short)
    if r_1yr:
        out.append(r_1yr)
    return out


def _parse_pnb_bulk_pdf(pdf_bytes: bytes) -> List[Dict[str, Any]]:
    """
    Parse PNB's Bulk Deposit (>10 Cr) PDF using its ToUnicode CMap.
    Extracts Retail Customers (Callable) rates for 7-14 days and 1 year across all 5 >10 Cr slabs.
    """
    streams = re.findall(b"stream[\r\n]+(.*?)[\r\n]+endstream", pdf_bytes, re.DOTALL)
    cmap = _build_pdf_cmap(streams)
    if not cmap:
        logger.warning("No ToUnicode CMap found in PNB bulk PDF.")
        return []

    rows_by_y: dict[float, list[tuple[float, str]]] = defaultdict(list)
    for s in streams:
        try:
            dec = zlib.decompress(s).decode("latin1", errors="ignore")
        except Exception:
            continue
        if "BT" not in dec or "Tm" not in dec:
            continue
        # Only inspect streams that contain the Retail Customers bulk table
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
                # Bucket Y within 2 units so tenor label and rate text on same visual row align
                matched_y = None
                for ey in rows_by_y:
                    if abs(ey - y) <= 2:
                        matched_y = ey
                        break
                rows_by_y[matched_y if matched_y is not None else round(y, 0)].append((x, txt))
        if rows_by_y:
            break

    rates: List[Dict[str, Any]] = []
    found_short = False
    found_1yr = False

    # Y increases upward on this PDF; Retail Customers table is at lower Y (Y ~ 255 to 450)
    for y in sorted(rows_by_y.keys()):
        cols = sorted(rows_by_y[y], key=lambda c: c[0])
        tenor_parts = [txt for x, txt in cols if x < 120]
        if not tenor_parts:
            continue
        tenor_label = " ".join(tenor_parts).strip()
        t_low = tenor_label.lower()

        is_short = "7" in t_low and "14" in t_low and "day" in t_low
        is_1yr = t_low == "1 year"
        if not (is_short or is_1yr):
            continue
        if is_short and found_short:
            continue
        if is_1yr and found_1yr:
            continue

        # Callable columns sit at X ~ 150..400
        rate_str = " ".join(txt for x, txt in cols if 140 <= x <= 410)
        col_values = [float(m) for m in re.findall(r"\d+\.\d{2}", rate_str)]
        if len(col_values) < len(PNB_BULK_CATEGORIES):
            logger.warning(
                "Expected %d rate columns for PNB tenor %r, got %d (%r)",
                len(PNB_BULK_CATEGORIES), tenor_label, len(col_values), rate_str,
            )
            continue

        min_d, max_d = parse_tenure_days(tenor_label)
        clean_label = "7-14 days" if is_short else "1 year"
        for idx, cat in enumerate(PNB_BULK_CATEGORIES):
            rates.append({
                "category": cat,
                "tenure_label": clean_label,
                "tenure_min_days": min_d,
                "tenure_max_days": max_d,
                "general_rate": col_values[idx],
            })

        if is_short:
            found_short = True
        if is_1yr:
            found_1yr = True

    return rates


def scrape_pnb_all_categories() -> List[Dict[str, Any]]:
    """
    Scrape Punjab National Bank FD rates across its original classifications:
    - Under 3 Cr (HTML table)
    - 3 Cr to 10 Cr (HTML table)
    - Above 10 Cr to 25 Cr, Above 25 Cr to 100 Cr, Above 100 Cr to 500 Cr,
      Above 500 Cr to 1000 Cr, Above 1000 Cr (Bulk Deposit PDF)
    """
    logger.info("Scraping Punjab National Bank rates...")
    headers = {"User-Agent": USER_AGENT}
    rates: List[Dict[str, Any]] = []

    # 1. Under 3 Cr and 3 Cr to 10 Cr from interest-rates-deposit.html
    try:
        r1 = requests.get(PNB_DEPOSIT_URL, headers=headers, timeout=20)
        r1.raise_for_status()
        s1 = BeautifulSoup(r1.text, "html.parser")
        for t in s1.find_all("table"):
            first_row = t.find("tr")
            if not first_row:
                continue
            title_text = first_row.get_text(" ", strip=True).lower()
            if "domestic/nro" in title_text and "fixed deposit" in title_text:
                rates.extend(_extract_table_short_and_1yr(t, "Under 3 Cr", tenor_col=1, rate_col=2))
            elif "domestic td" in title_text and "3 cr" in title_text:
                rates.extend(_extract_table_short_and_1yr(t, "3 Cr to 10 Cr", tenor_col=1, rate_col=3))
    except Exception as e:
        logger.error("Error scraping PNB deposit HTML tables: %s", e)

    # 2. >10 Cr Bulk Deposit PDF discovered from interest-rates-bulk-deposit.html
    try:
        r2 = requests.get(PNB_BULK_PAGE_URL, headers=headers, timeout=20)
        r2.raise_for_status()
        m = re.search(r'''var\s+fileUrl\s*=\s*[`'"]([^`'"]+)[`'"]''', r2.text)
        if m:
            pdf_url = urljoin(PNB_BULK_PAGE_URL, m.group(1))
            logger.info("Downloading PNB bulk deposit PDF from %s", pdf_url)
            pdf_resp = requests.get(pdf_url, headers=headers, timeout=25)
            pdf_resp.raise_for_status()
            rates.extend(_parse_pnb_bulk_pdf(pdf_resp.content))
        else:
            logger.warning("Could not locate fileUrl on PNB bulk deposit page.")
    except Exception as e:
        logger.error("Error scraping PNB bulk deposit PDF: %s", e)

    logger.info("Scraped %d rates for Punjab National Bank", len(rates))
    return rates


def save_pnb_rates(rates: List[Dict[str, Any]]):
    """Save Punjab National Bank rates to SQLite database."""
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT id FROM banks WHERE short_name = 'PNB'")
    bank_row = cursor.fetchone()
    if not bank_row:
        logger.error("PNB bank record not found in database!")
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
    logger.info("Saved %d Punjab National Bank FD rates into database.", len(rates))


if __name__ == "__main__":
    init_pnb_bank()
    rates = scrape_pnb_all_categories()
    save_pnb_rates(rates)
    print(f"Done! Scraped and stored {len(rates)} Punjab National Bank rates.")
