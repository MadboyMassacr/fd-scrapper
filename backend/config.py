"""
Application configuration using pydantic-settings.

Loads settings from environment variables and .env file.
All configuration values can be overridden via environment variables
or by placing them in a .env file at the project root.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import List

from pydantic_settings import BaseSettings, SettingsConfigDict


# Resolve the project root (one level above /backend)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """Central configuration for the FD Rate Scraper application."""

    model_config = SettingsConfigDict(
        env_file=os.path.join(_PROJECT_ROOT, ".env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # ── Database ──────────────────────────────────────────────────────
    DATABASE_URL: str = "sqlite:///./fd_rates.db"

    # ── Scheduler ─────────────────────────────────────────────────────
    SCRAPE_HOUR: int = 8
    SCRAPE_MINUTE: int = 0

    # ── Logging ───────────────────────────────────────────────────────
    LOG_LEVEL: str = "INFO"

    # ── API Server ────────────────────────────────────────────────────
    API_HOST: str = "0.0.0.0"
    API_PORT: int = 8000

    # ── CORS ──────────────────────────────────────────────────────────
    CORS_ORIGINS: List[str] = ["http://localhost:3000"]


# Singleton settings instance used throughout the application
settings = Settings()
