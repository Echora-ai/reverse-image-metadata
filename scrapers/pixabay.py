"""Pixabay scraper - page scraping only, no API.

Scrapes photographer info directly from Pixabay photo pages.
No rate limits to worry about.
"""

import re
import json
import logging
from typing import Optional
from bs4 import BeautifulSoup
from urllib.parse import urlparse

from .base import BaseScraper

logger = logging.getLogger(__name__)


class PixabayScraper(BaseScraper):
    """
    Scraper for Pixabay - page scraping only.
    
    All Pixabay content is released under the Pixabay License
    (free for commercial use, no attribution required).
    
    We extract:
    - Username (photographer/contributor)
    - Photo title (from tags)
    - License info
    - Image type (photo, illustration, vector)
    """
    
    source_name = "pixabay"
    
    async def _extract_attribution(self, soup: BeautifulSoup, url: str) -> Optional[dict]:
        """
        Extract attribution from Pixabay page by scraping HTML.
        """
        result = {
            "photographer": None,
            "photographer_url": None,
            "license": "Pixabay License",
            "title": None,
        }
        
        # Try JSON-LD schema first
        schema_data = self._extract_schema_data(soup)
        if schema_data:
            result.update(schema_data)
        
        # Look for user/photographer name
        # Pixabay shows username with follower count
        if not result["photographer"]:
            # Look for links to /users/ profiles
            user_link = soup.find("a", href=re.compile(r"/users/[a-zA-Z0-9_-]+"))
            if user_link:
                # The username is usually the text or in a child element
                text = user_link.get_text()
                if text and len(text) < 50:
                    # Clean up - might have follower count
                    text = re.sub(r"\d+[\s,]*followers?", "", text, flags=re.I).strip()
                    result["photographer"] = self._clean_text(text)
                    
                    href = user_link.get("href", "")
                    if href.startswith("/"):
                        result["photographer_url"] = f"https://pixabay.com{href}"
                    else:
                        result["photographer_url"] = href
        
        # Also try to find username near the download button area
        if not result["photographer"]:
            # Look for any text that looks like a username with followers
            text = soup.get_text()
            match = re.search(r"([a-zA-Z0-9_-]+)\s*[\n\s]*([\d,]+)\s*followers?", text, re.I)
            if match:
                result["photographer"] = match.group(1)
        
        # Get title from page title or og:title
        if not result["title"]:
            og_title = soup.find("meta", {"property": "og:title"})
            if og_title:
                title = og_title.get("content", "")
                # Pixabay titles often end with "- Free ... on Pixabay"
                title = re.sub(r"\s*-\s*Free.*on Pixabay$", "", title, flags=re.I)
                title = re.sub(r"\s*-\s*Free.*$", "", title, flags=re.I)
                result["title"] = self._clean_text(title)
        
        # Also try extracting title from URL (tags are in URL)
        if not result["title"]:
            result["title"] = self._extract_title_from_url(url)
        
        if result["photographer"] or result["title"]:
            return result
        
        return None
    
    def _extract_title_from_url(self, url: str) -> Optional[str]:
        """
        Extract title from Pixabay URL.
        URLs like: pixabay.com/photos/beach-sea-sunset-sun-sunlight-1751455/
        """
        try:
            parsed = urlparse(url)
            path = parsed.path.strip("/")
            
            # Split path: photos/beach-sea-sunset-sun-sunlight-1751455
            parts = path.split("/")
            if len(parts) >= 2:
                # Get the slug part: beach-sea-sunset-sun-sunlight-1751455
                slug = parts[-1]
                # Remove the ID at the end
                slug = re.sub(r"-\d+$", "", slug)
                # Replace hyphens with spaces and title case
                title = slug.replace("-", " ").title()
                return title
        except Exception:
            pass
        
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
                    
                    if data.get("@type") in ["ImageObject", "Photograph", "CreativeWork"]:
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
                            "title": self._clean_text(data.get("name") or data.get("description")),
                            "license": "Pixabay License",
                        }
        except (json.JSONDecodeError, KeyError, TypeError):
            pass
        
        return None
