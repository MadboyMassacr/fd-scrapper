# 🏦 Fixed Deposit Interest Rate Aggregator — SBI Live Scraper

A single-page dashboard that scrapes official Indian bank portals for their day's active Fixed Deposit (FD) interest rates, adhering to the Government of India / RBI daily disclosure mandate.

Currently active and configured for **State Bank of India (SBI)** retail domestic term deposits (< Rs. 3 Crore).

---

## 🚀 Features

- **Live SBI Scraper**: Directly fetches from official SBI Retail Domestic Term Deposits page using BeautifulSoup.
- **Normalized Data Model**: Accurately parses tenure buckets (`7 days to 45 days`, up to `10 years`), general public rates, and senior citizen rates.
- **SQLite Database**: Persistent relational storage (`fd_rates.db`) keeping historical audits and latest scrapes.
- **Background Cron / Auto-Sync**: Background daemon automatically runs periodic daily updates.
- **Interactive Dashboard**:
  - Top general and senior citizen rate KPI cards.
  - Quick filter buttons (*Under 1 Year*, *1–3 Years*, *3+ Years*).
  - Search bar by tenure keyword.
  - Senior citizen rate highlight toggle.
  - On-demand **"Scrape Now"** button triggering instant live bank synchronization.
  - Official RBI policy & compliance verification link.

---

## 📁 Project Structure

```
FD-Scraper/
├── backend/
│   ├── sbi_service.py      # Core scraping logic, parser & SQLite persistence
│   ├── server.py           # Multi-threaded HTTP & REST API server + background scheduler
│   ├── scrapers/           # Modular bank scrapers
│   └── db/                 # Database schema models
├── frontend/
│   └── index.html          # Responsive single-page client dashboard (Tailwind CSS)
├── fd_rates.db             # SQLite database storing banks, rates, and audit logs
├── run.py                  # Entrypoint to run server & scraper
└── README.md
```

---

## 🏃 Quick Start

To run the application locally:

```bash
python run.py
```

Then open your browser at:
👉 **[http://localhost:8000](http://localhost:8000)**

---

## 🔌 API Endpoints

- `GET /api/rates/latest` — Returns latest active FD interest rates by tenure.
- `GET /api/stats` — High-level summary metrics (highest rate, best tenure, last sync time).
- `GET /api/banks` — List of tracked banking institutions.
- `GET /api/scrape/logs` — Audit log of recent scraper executions and durations.
- `POST /api/scrape/trigger` — Trigger an immediate live scrape of bank portals.

