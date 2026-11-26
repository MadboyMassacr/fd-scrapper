"""
Scraper registry — maps bank short names to their scraper classes.

Import this module to discover all available scrapers:

    >>> from backend.scrapers import SCRAPER_REGISTRY
    >>> SCRAPER_REGISTRY["SBI"]
    <class 'backend.scrapers.sbi.SBIScraper'>
"""

from backend.scrapers.sbi import SBIScraper
from backend.scrapers.hdfc import HDFCScraper
from backend.scrapers.icici import ICICIScraper

SCRAPER_REGISTRY: dict[str, type] = {
    "SBI": SBIScraper,
    "HDFC": HDFCScraper,
    "ICICI": ICICIScraper,
}

__all__ = [
    "SCRAPER_REGISTRY",
    "SBIScraper",
    "HDFCScraper",
    "ICICIScraper",
]
