"""Pexels scraper - page scraping only, no API.

Scrapes photographer info directly from Pexels photo pages.
No rate limits to worry about.
"""

import re
import json
import logging
from typing import Optional
from bs4 import BeautifulSoup

from .base import BaseScraper

logger = logging.getLogger(__name__)


class PexelsScraper(BaseScraper):
    """
    Scraper for Pexels - page scraping only.
    
    All Pexels photos are free to use under the Pexels License.
    We extract:
    - Photographer name (from profile link)
    - Photo title
    - License info (CC0 or Pexels License)
    - Location (if available)
    """
    
    source_name = "pexels"
    
    async def _extract_attribution(self, soup: BeautifulSoup, url: str) -> Optional[dict]:
        """
        Extract attribution from Pexels page by scraping HTML.
        """
        result = {
            "photographer": None,
            "photographer_url": None,
            "license": "Pexels License",
            "title": None,
            "location": None,
        }
        
        # Try JSON-LD schema first (most reliable)
        schema_data = self._extract_schema_data(soup)
        if schema_data:
            result.update(schema_data)
        
        # Look for photographer name from profile link
        # Pexels uses links like /@username
        if not result["photographer"]:
            photographer_link = soup.find("a", href=re.compile(r"/@[a-zA-Z0-9_-]+"))
            if photographer_link:
                result["photographer"] = self._clean_text(photographer_link.get_text())
                href = photographer_link.get("href", "")
                if href.startswith("/"):
                    result["photographer_url"] = f"https://www.pexels.com{href}"
                else:
                    result["photographer_url"] = href
        
        # Also check for heading with photographer name
        if not result["photographer"]:
            # Look for h1 or h2 with photographer name pattern
            for heading in soup.find_all(["h1", "h2"]):
                text = heading.get_text()
                if text and len(text) < 50 and not any(x in text.lower() for x in ["photo", "image", "free"]):
                    result["photographer"] = self._clean_text(text)
                    break
        
        # Get title from og:title or page heading
        if not result["title"]:
            og_title = soup.find("meta", {"property": "og:title"})
            if og_title:
                title = og_title.get("content", "")
                # Pexels titles often include "· Free Stock Photo"
                title = re.sub(r"\s*[·|]\s*Free.*$", "", title, flags=re.IGNORECASE)
                result["title"] = self._clean_text(title)
        
        # Try to get location from page
        if not result["location"]:
            # Pexels shows location with a flag icon
            location_elem = soup.find(attrs={"data-testid": "photo-location"})
            if location_elem:
                result["location"] = self._clean_text(location_elem.get_text())
            else:
                # Look for location in structured content
                for elem in soup.find_all(class_=re.compile(r"location", re.I)):
                    text = elem.get_text()
                    if text and len(text) < 100:
                        result["location"] = self._clean_text(text)
                        break
        
        # Check for CC0 license specifically
        page_text = soup.get_text().lower()
        if "cc0" in page_text or "public domain" in page_text:
            result["license"] = "CC0 (Public Domain)"
        
        if result["photographer"] or result["title"]:
            return result
        
        return None
    
    def _extract_schema_data(self, soup: BeautifulSoup) -> Optional[dict]:
        """
        Extract data from JSON-LD schema markup.
        """
        try:
            scripts = soup.find_all("script", {"type": "application/ld+json"})
            for script in scripts:
                if script.string:
                    data = json.loads(script.string)
                    
                    if isinstance(data, list):
                        data = data[0] if data else {}
                    
                    if data.get("@type") in ["ImageObject", "Photograph"]:
                        photographer = None
                        photographer_url = None
                        
                        author = data.get("author") or data.get("creator")
                        if author:
                            if isinstance(author, dict):
                                photographer = author.get("name")
                                photographer_url = author.get("url")
                            elif isinstance(author, str):
                                photographer = author
                        
                        return {
                            "photographer": self._clean_text(photographer),
                            "photographer_url": photographer_url,
                            "title": self._clean_text(data.get("name")),
                            "license": "Pexels License",
                        }
        except (json.JSONDecodeError, KeyError, TypeError):
            pass
        
        return None
