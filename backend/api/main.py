"""
FastAPI application factory and configuration.

This is the main entry point for the FD Rate Aggregator API.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend.config import settings
from backend.db.database import Base, engine

# Import routers
from backend.api.routes.banks import router as banks_router
from backend.api.routes.rates import router as rates_router
from backend.api.routes.scrape import router as scrape_router

# ── Logging ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


# ── App ──────────────────────────────────────────────────────────────────────

def create_app() -> FastAPI:
    """Build and return the configured FastAPI application."""

    app = FastAPI(
        title="FD Rate Aggregator API",
        description=(
            "Aggregates and compares Fixed Deposit interest rates from "
            "major Indian banks.  Provides endpoints for querying current "
            "rates, historical trends, and cross-bank comparisons."
        ),
        version="1.0.0",
    )

    # ── CORS ─────────────────────────────────────────────────────────
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ── Routers ──────────────────────────────────────────────────────
    app.include_router(banks_router)
    app.include_router(rates_router)
    app.include_router(scrape_router)

    # ── Startup event ────────────────────────────────────────────────
    @app.on_event("startup")
    async def on_startup() -> None:
        """Create database tables on startup if they don't exist."""
        logger.info("Creating database tables (if not exist)…")
        Base.metadata.create_all(bind=engine)
        logger.info("Database tables ready.")

    # ── Root endpoint ────────────────────────────────────────────────
    @app.get("/", tags=["Root"])
    async def root() -> dict:
        """Return basic API information."""
        return {
            "name": "FD Rate Aggregator API",
            "version": "1.0.0",
            "docs": "/docs",
            "endpoints": {
                "banks": "/api/banks",
                "rates": "/api/rates",
                "rates_latest": "/api/rates/latest",
                "rates_compare": "/api/rates/compare",
                "rates_best": "/api/rates/best",
                "scrape_trigger": "/api/scrape/trigger",
                "scrape_logs": "/api/scrape/logs",
            },
        }

    return app


# Create the app instance (used by uvicorn)
app = create_app()
