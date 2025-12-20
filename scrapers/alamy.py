"""Alamy scraper for photographer attribution."""

import re
import json
import logging
from typing import Optional
from bs4 import BeautifulSoup

from .base import BaseScraper

logger = logging.getLogger(__name__)


class AlamyScraper(BaseScraper):
    """
    Scraper for Alamy stock photos.
    
    Alamy provides detailed contributor and rights information.
    """
    
    source_name = "alamy"
    
    async def _extract_attribution(self, soup: BeautifulSoup, url: str) -> Optional[dict]:
        """
        Extract attribution from Alamy page.
        """
        result = {
            "photographer": None,
            "license": None,
            "title": None,
        }
        
        # Try JSON-LD schema first
        schema_data = self._extract_schema_data(soup)
        if schema_data:
            result.update(schema_data)
        
        # Look for credit line
        if not result["photographer"]:
            # Alamy shows "Credit: Photographer Name" prominently
            credit_patterns = [
                r"Credit[:\s]+([^|\n<]+)",
                r"Contributor[:\s]+([^|\n<]+)",
                r"Photographer[:\s]+([^|\n<]+)",
            ]
            
            text = soup.get_text()
            for pattern in credit_patterns:
                match = re.search(pattern, text, re.IGNORECASE)
                if match:
                    photographer = self._clean_text(match.group(1))
                    if photographer and len(photographer) < 100:
                        result["photographer"] = photographer
                        break
        
        # Look for contributor link
        if not result["photographer"]:
            contributor_link = soup.find("a", href=re.compile(r"/stock-photo/contributor/"))
            if contributor_link:
                result["photographer"] = self._clean_text(contributor_link.get_text())
        
        # Get title from h1 or og:title
        if not result["title"]:
            h1 = soup.find("h1")
            if h1:
                result["title"] = self._clean_text(h1.get_text())
            else:
                og_title = soup.find("meta", {"property": "og:title"})
                if og_title:
                    result["title"] = self._clean_text(og_title.get("content"))
        
        # Determine license type
        result["license"] = self._extract_license(soup)
        
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
                    
                    if data.get("@type") in ["ImageObject", "Photograph", "Product"]:
                        photographer = None
                        author = data.get("author") or data.get("creator")
                        if author:
                            if isinstance(author, dict):
                                photographer = author.get("name")
                            elif isinstance(author, str):
                                photographer = author
                        
                        return {
                            "photographer": self._clean_text(photographer),
                            "title": self._clean_text(data.get("name")),
                            "license": None,
                        }
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            logger.debug(f"Schema extraction failed: {e}")
        
        return None
    
    def _extract_license(self, soup: BeautifulSoup) -> Optional[str]:
        """
        Extract license type from Alamy page.
        """
        text = soup.get_text().lower()
        
        if "rights managed" in text or "rights-managed" in text:
            return "Rights Managed"
        elif "royalty free" in text or "royalty-free" in text:
            return "Royalty Free"
        elif "editorial" in text:
            return "Editorial"
        
        return None
