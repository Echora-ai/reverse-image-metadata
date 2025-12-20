"""Getty Images scraper for photographer attribution."""

import re
import json
import logging
from typing import Optional
from bs4 import BeautifulSoup

from .base import BaseScraper

logger = logging.getLogger(__name__)


class GettyScraper(BaseScraper):
    """
    Scraper for Getty Images.
    
    Getty has well-structured metadata including:
    - Photographer/artist name
    - Collection name
    - License type (Rights Managed, Royalty Free, Editorial)
    - Image title/caption
    """
    
    source_name = "getty"
    
    async def _extract_attribution(self, soup: BeautifulSoup, url: str) -> Optional[dict]:
        """
        Extract attribution from Getty Images page.
        """
        result = {
            "photographer": None,
            "license": None,
            "title": None,
        }
        
        # Try to get data from JSON-LD schema
        schema_data = self._extract_schema_data(soup)
        if schema_data:
            result["photographer"] = schema_data.get("photographer")
            result["title"] = schema_data.get("title")
        
        # Try meta tags as fallback
        if not result["photographer"]:
            # Getty uses various meta tags
            artist_meta = soup.find("meta", {"name": "artist"})
            if artist_meta:
                result["photographer"] = self._clean_text(artist_meta.get("content"))
        
        if not result["title"]:
            title_meta = soup.find("meta", {"property": "og:title"})
            if title_meta:
                result["title"] = self._clean_text(title_meta.get("content"))
        
        # Look for artist/credit in page content
        if not result["photographer"]:
            # Getty often has "Credit: Photographer Name" in the page
            credit_patterns = [
                r"Credit[:\s]+([^,<]+)",
                r"Artist[:\s]+([^,<]+)",
                r"By[:\s]+([^,<]+)",
            ]
            
            text = soup.get_text()
            for pattern in credit_patterns:
                match = re.search(pattern, text, re.IGNORECASE)
                if match:
                    photographer = self._clean_text(match.group(1))
                    if photographer and len(photographer) < 100:  # Sanity check
                        result["photographer"] = photographer
                        break
        
        # Extract license info
        result["license"] = self._extract_license(soup)
        
        # Only return if we found at least photographer or title
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
                    
                    # Handle both single objects and arrays
                    if isinstance(data, list):
                        data = data[0] if data else {}
                    
                    # Check for ImageObject or Photograph schema
                    if data.get("@type") in ["ImageObject", "Photograph", "CreativeWork"]:
                        photographer = None
                        
                        # Author/creator field
                        author = data.get("author") or data.get("creator")
                        if author:
                            if isinstance(author, dict):
                                photographer = author.get("name")
                            elif isinstance(author, str):
                                photographer = author
                        
                        # Copyright holder as fallback
                        if not photographer:
                            holder = data.get("copyrightHolder")
                            if isinstance(holder, dict):
                                photographer = holder.get("name")
                        
                        return {
                            "photographer": self._clean_text(photographer),
                            "title": self._clean_text(data.get("name") or data.get("headline")),
                        }
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            logger.debug(f"Schema extraction failed: {e}")
        
        return None
    
    def _extract_license(self, soup: BeautifulSoup) -> Optional[str]:
        """
        Extract license type from Getty page.
        """
        text = soup.get_text().lower()
        
        if "rights managed" in text or "rights-managed" in text:
            return "Rights Managed"
        elif "royalty free" in text or "royalty-free" in text:
            return "Royalty Free"
        elif "editorial" in text:
            return "Editorial"
        
        return None
