import json
import os
import threading
import logging
from http.server import HTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from datetime import datetime, date

from backend.sbi_service import (
    get_db_connection,
    scrape_all_categories as scrape_sbi,
    save_sbi_rates,
    init_db
)
from backend.bob_service import (
    init_bob_bank,
    scrape_bob_all_categories as scrape_bob,
    save_bob_rates
)
from backend.ubi_service import (
    init_ubi_bank,
    scrape_ubi_all_categories as scrape_ubi,
    save_ubi_rates
)
from backend.pnb_service import (
    init_pnb_bank,
    scrape_pnb_all_categories as scrape_pnb,
    save_pnb_rates
)
from backend.ib_service import (
    init_ib_bank,
    scrape_ib_all_categories as scrape_ib,
    save_ib_rates
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("server")

HOST = "0.0.0.0"
PORT = int(os.environ.get("PORT", 8000))
FRONTEND_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "frontend")


def scrape_and_save_all():
    """Scrapes and saves rates for all supported institutions (SBI, BOB, UBI, PNB, INDB)."""
    sbi_count = 0
    bob_count = 0
    ubi_count = 0
    pnb_count = 0
    ib_count = 0
    try:
        sbi_rates = scrape_sbi()
        save_sbi_rates(sbi_rates)
        sbi_count = len(sbi_rates)
    except Exception as e:
        logger.error("SBI scrape failed: %s", e)

    try:
        bob_rates = scrape_bob()
        save_bob_rates(bob_rates)
        bob_count = len(bob_rates)
    except Exception as e:
        logger.error("BoB scrape failed: %s", e)

    try:
        ubi_rates = scrape_ubi()
        save_ubi_rates(ubi_rates)
        ubi_count = len(ubi_rates)
    except Exception as e:
        logger.error("UBI scrape failed: %s", e)

    try:
        pnb_rates = scrape_pnb()
        save_pnb_rates(pnb_rates)
        pnb_count = len(pnb_rates)
    except Exception as e:
        logger.error("PNB scrape failed: %s", e)

    try:
        ib_rates = scrape_ib()
        save_ib_rates(ib_rates)
        ib_count = len(ib_rates)
    except Exception as e:
        logger.error("Indian Bank scrape failed: %s", e)

    return sbi_count, bob_count, ubi_count, pnb_count, ib_count


class FDRateServer(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=FRONTEND_DIR, **kwargs)

    def _send_json(self, data, status=200):
        body = json.dumps(data, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query_params = parse_qs(parsed.query)

        if path == "/api/categories":
            bank_filter = query_params.get("bank", [None])[0]
            conn = get_db_connection()
            if bank_filter and bank_filter != "All":
                rows = conn.execute(
                    """
                    SELECT DISTINCT f.category
                    FROM fd_rates f
                    JOIN banks b ON b.id = f.bank_id
                    WHERE b.short_name = ?
                    ORDER BY f.id ASC
                    """,
                    (bank_filter,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT DISTINCT category FROM fd_rates ORDER BY id ASC"
                ).fetchall()
            conn.close()
            categories = ["All"] + [r["category"] for r in rows]
            self._send_json(categories)
            return

        elif path == "/api/banks":
            conn = get_db_connection()
            rows = conn.execute("SELECT id, name, short_name, logo_url, website_url, bank_type, is_active FROM banks WHERE is_active = 1 ORDER BY id").fetchall()
            banks = [dict(r) for r in rows]
            conn.close()
            self._send_json(banks)
            return

        elif path == "/api/rates/latest" or path == "/api/rates":
            conn = get_db_connection()
            category_filter = query_params.get("category", [None])[0]
            bank_filter = query_params.get("bank", [None])[0]

            where_clauses = []
            params = []
            if category_filter and category_filter != "All":
                where_clauses.append("f.category = ?")
                params.append(category_filter)
            if bank_filter and bank_filter != "All":
                where_clauses.append("b.short_name = ?")
                params.append(bank_filter)

            where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

            query = f"""
                SELECT 
                    f.id, f.bank_id, b.name as bank_name, b.short_name as bank_short_name,
                    f.category, f.tenure_label, f.tenure_min_days, f.tenure_max_days,
                    f.general_rate, f.senior_citizen_rate, f.effective_date, f.scraped_at
                FROM fd_rates f
                JOIN banks b ON b.id = f.bank_id
                {where_sql}
                ORDER BY b.short_name ASC, f.id ASC
            """
            rows = conn.execute(query, params).fetchall()
            rates = [dict(r) for r in rows]
            conn.close()
            self._send_json(rates)
            return

        elif path == "/api/stats":
            conn = get_db_connection()
            bank_count = conn.execute("SELECT COUNT(DISTINCT bank_id) FROM fd_rates").fetchone()[0]
            category_count = conn.execute("SELECT COUNT(DISTINCT category) FROM fd_rates").fetchone()[0]
            max_rate_row = conn.execute("""
                SELECT f.general_rate, f.category, f.tenure_label, b.name as bank_name, b.short_name as bank_short_name
                FROM fd_rates f JOIN banks b ON b.id = f.bank_id 
                ORDER BY f.general_rate DESC LIMIT 1
            """).fetchone()
            
            latest_log = conn.execute("SELECT scraped_at, status FROM scrape_logs ORDER BY id DESC LIMIT 1").fetchone()
            conn.close()

            stats = {
                "banks_tracked": bank_count,
                "categories_tracked": category_count,
                "highest_general_rate": max_rate_row["general_rate"] if max_rate_row else None,
                "best_bank": max_rate_row["bank_short_name"] if max_rate_row else None,
                "best_category": max_rate_row["category"] if max_rate_row else None,
                "best_tenure": max_rate_row["tenure_label"] if max_rate_row else None,
                "last_scraped_at": latest_log["scraped_at"] if latest_log else datetime.now().isoformat(),
                "last_status": latest_log["status"] if latest_log else "active"
            }
            self._send_json(stats)
            return

        elif path == "/api/scrape/logs":
            conn = get_db_connection()
            rows = conn.execute("""
                SELECT l.id, b.name as bank_name, l.status, l.error_message, l.scraped_at, l.duration_seconds
                FROM scrape_logs l
                JOIN banks b ON b.id = l.bank_id
                ORDER BY l.id DESC LIMIT 10
            """).fetchall()
            logs = [dict(r) for r in rows]
            conn.close()
            self._send_json(logs)
            return

        if path == "/" or not os.path.exists(os.path.join(FRONTEND_DIR, path.lstrip("/"))):
            self.path = "/index.html"
        return super().do_GET()

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/scrape/trigger":
            logger.info("Manual scrape triggered via API for SBI, BoB, UBI, PNB, and Indian Bank")
            try:
                sbi_c, bob_c, ubi_c, pnb_c, ib_c = scrape_and_save_all()
                self._send_json({
                    "status": "success",
                    "message": f"Successfully scraped {sbi_c} SBI, {bob_c} BoB, {ubi_c} UBI, {pnb_c} PNB, and {ib_c} Indian Bank rates",
                    "scraped_at": datetime.now().isoformat(),
                    "total_count": sbi_c + bob_c + ubi_c + pnb_c + ib_c
                })
            except Exception as e:
                logger.error("Scraping failed: %s", str(e))
                self._send_json({"status": "failed", "error": str(e)}, status=500)
            return

        self.send_error(404, "Not Found")


def run_scheduler():
    import time
    logger.info("Daily background scraper scheduler started.")
    while True:
        time.sleep(6 * 3600)
        try:
            logger.info("Automatic scheduled scrape running for all banks...")
            scrape_and_save_all()
        except Exception as e:
            logger.error("Scheduled scrape failed: %s", e)


def main():
    init_db()
    init_bob_bank()
    init_ubi_bank()
    init_pnb_bank()
    init_ib_bank()

    conn = get_db_connection()
    count = conn.execute("SELECT COUNT(*) FROM fd_rates").fetchone()[0]
    conn.close()

    if count == 0:
        logger.info("No rates found in DB. Scraping all banks immediately...")
        scrape_and_save_all()

    t = threading.Thread(target=run_scheduler, daemon=True)
    t.start()

    logger.info(f"Server starting on http://localhost:{PORT}")
    server = HTTPServer((HOST, PORT), FDRateServer)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Server stopped.")


if __name__ == "__main__":
    main()
