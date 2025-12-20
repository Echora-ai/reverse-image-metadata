"""Scrapers package for extracting attribution from various photo sources."""

from urllib.parse import urlparse
from typing import Optional

from .base import BaseScraper
from .getty import GettyScraper
from .shutterstock import ShutterstockScraper
from .unsplash import UnsplashScraper
from .flickr import FlickrScraper
from .alamy import AlamyScraper
from .news import NewsScraper

# Priority order for known sources (most reliable first)
PRIORITY_DOMAINS = [
    "gettyimages.com",
    "gettyimages.co.uk",
    "shutterstock.com",
    "unsplash.com",
    "flickr.com",
    "alamy.com",
    "500px.com",
    "apimages.com",
    "reuters.com",
    "nytimes.com",
]

# Map domains to scrapers
SCRAPER_MAP = {
    "gettyimages.com": GettyScraper,
    "gettyimages.co.uk": GettyScraper,
    "shutterstock.com": ShutterstockScraper,
    "unsplash.com": UnsplashScraper,
    "flickr.com": FlickrScraper,
    "alamy.com": AlamyScraper,
    "apimages.com": NewsScraper,
    "reuters.com": NewsScraper,
    "nytimes.com": NewsScraper,
}


def get_scraper_for_url(url: str) -> Optional[BaseScraper]:
    """
    Get the appropriate scraper for a given URL.
    Returns None if no scraper is available for the domain.
    """
    try:
        parsed = urlparse(url)
        domain = parsed.netloc.lower()
        
        # Remove www. prefix if present
        if domain.startswith("www."):
            domain = domain[4:]
        
        # Check each known domain
        for known_domain, scraper_class in SCRAPER_MAP.items():
            if known_domain in domain:
                return scraper_class()
        
        return None
    except Exception:
        return None


__all__ = [
    "BaseScraper",
    "GettyScraper",
    "ShutterstockScraper",
    "UnsplashScraper",
    "FlickrScraper",
    "AlamyScraper",
    "NewsScraper",
    "get_scraper_for_url",
    "PRIORITY_DOMAINS",
    "SCRAPER_MAP",
]
