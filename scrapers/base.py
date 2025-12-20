"""Base scraper class for photo attribution extraction."""

import aiohttp
import logging
from abc import ABC, abstractmethod
from typing import Optional
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)


class BaseScraper(ABC):
    """
    Abstract base class for photo attribution scrapers.
    
    Each scraper implementation handles a specific photo site
    and extracts photographer name, license info, and title.
    """
    
    source_name: str = "unknown"
    
    def __init__(self):
        self.user_agent = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        )
        self.timeout = aiohttp.ClientTimeout(total=15)
    
    async def scrape(self, url: str) -> Optional[dict]:
        """
        Scrape attribution data from the given URL.
        
        Returns:
            dict with keys: photographer, license, title
            or None if scraping fails
        """
        try:
            html = await self._fetch_page(url)
            if not html:
                return None
            
            soup = BeautifulSoup(html, "html.parser")
            return await self._extract_attribution(soup, url)
            
        except Exception as e:
            logger.error(f"Error scraping {url}: {e}")
            return None
    
    async def _fetch_page(self, url: str) -> Optional[str]:
        """
        Fetch the HTML content of a page.
        """
        headers = {
            "User-Agent": self.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
            "Accept-Encoding": "gzip, deflate",
            "DNT": "1",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
        }
        
        async with aiohttp.ClientSession(timeout=self.timeout) as session:
            async with session.get(url, headers=headers) as response:
                if response.status == 200:
                    return await response.text()
                else:
                    logger.warning(f"Failed to fetch {url}: {response.status}")
                    return None
    
    @abstractmethod
    async def _extract_attribution(self, soup: BeautifulSoup, url: str) -> Optional[dict]:
        """
        Extract attribution data from parsed HTML.
        
        Must be implemented by subclasses.
        
        Args:
            soup: BeautifulSoup parsed HTML
            url: Original URL (for reference)
            
        Returns:
            dict with keys: photographer, license, title
        """
        pass
    
    def _clean_text(self, text: Optional[str]) -> Optional[str]:
        """
        Clean and normalize extracted text.
        """
        if not text:
            return None
        
        # Remove extra whitespace
        cleaned = " ".join(text.split())
        
        # Remove common prefixes
        prefixes_to_remove = [
            "Photo by ",
            "By ",
            "Credit: ",
            "Image by ",
            "Photographer: ",
            "© ",
            "Copyright ",
        ]
        
        for prefix in prefixes_to_remove:
            if cleaned.startswith(prefix):
                cleaned = cleaned[len(prefix):]
        
        return cleaned.strip() if cleaned else None
