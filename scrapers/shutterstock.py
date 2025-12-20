"""Shutterstock scraper for photographer attribution."""

import re
import json
import logging
from typing import Optional
from bs4 import BeautifulSoup

from .base import BaseScraper

logger = logging.getLogger(__name__)


class ShutterstockScraper(BaseScraper):
    """
    Scraper for Shutterstock.
    
    Shutterstock displays contributor name prominently on image pages.
    """
    
    source_name = "shutterstock"
    
    async def _extract_attribution(self, soup: BeautifulSoup, url: str) -> Optional[dict]:
        """
        Extract attribution from Shutterstock page.
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
        
        # Look for contributor link/name
        if not result["photographer"]:
            # Shutterstock uses contributor links with specific patterns
            contributor_link = soup.find("a", href=re.compile(r"/g/[^/]+"))
            if contributor_link:
                result["photographer"] = self._clean_text(contributor_link.get_text())
            
            # Also check for "by" attribution
            if not result["photographer"]:
                by_text = soup.find(text=re.compile(r"by\s+", re.IGNORECASE))
                if by_text:
                    parent = by_text.parent
                    if parent:
                        link = parent.find("a")
                        if link:
                            result["photographer"] = self._clean_text(link.get_text())
        
        # Get title from page
        if not result["title"]:
            title_tag = soup.find("h1")
            if title_tag:
                result["title"] = self._clean_text(title_tag.get_text())
            else:
                og_title = soup.find("meta", {"property": "og:title"})
                if og_title:
                    result["title"] = self._clean_text(og_title.get("content"))
        
        # Shutterstock images are typically royalty-free
        result["license"] = "Royalty Free"
        
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
