"""Unsplash scraper using their free API."""

import os
import re
import json
import logging
import aiohttp
from typing import Optional
from bs4 import BeautifulSoup

from .base import BaseScraper

logger = logging.getLogger(__name__)


class UnsplashScraper(BaseScraper):
    """
    Scraper for Unsplash.
    
    Unsplash has a free API, but we can also scrape the page directly.
    All Unsplash photos are free to use under the Unsplash License.
    """
    
    source_name = "unsplash"
    
    def __init__(self):
        super().__init__()
        self.api_key = os.environ.get("UNSPLASH_ACCESS_KEY")
    
    async def _extract_attribution(self, soup: BeautifulSoup, url: str) -> Optional[dict]:
        """
        Extract attribution from Unsplash page or API.
        """
        result = {
            "photographer": None,
            "license": "Unsplash License",
            "title": None,
        }
        
        # Extract photo ID from URL
        photo_id = self._extract_photo_id(url)
        
        # Try API first if we have a key and photo ID
        if self.api_key and photo_id:
            api_result = await self._fetch_from_api(photo_id)
            if api_result:
                return api_result
        
        # Fall back to page scraping
        # Try JSON-LD schema
        schema_data = self._extract_schema_data(soup)
        if schema_data:
            result.update(schema_data)
        
        # Look for photographer name in page
        if not result["photographer"]:
            # Unsplash shows photographer prominently
            # Look for links to user profiles
            user_link = soup.find("a", href=re.compile(r"/@[a-zA-Z0-9_-]+"))
            if user_link:
                result["photographer"] = self._clean_text(user_link.get_text())
        
        # Get title/alt text
        if not result["title"]:
            og_title = soup.find("meta", {"property": "og:title"})
            if og_title:
                title = og_title.get("content", "")
                # Unsplash titles are often "Photo by X on Unsplash"
                if "Photo by" in title:
                    match = re.search(r"Photo by ([^|]+)", title)
                    if match and not result["photographer"]:
                        result["photographer"] = self._clean_text(match.group(1).replace(" on Unsplash", ""))
                else:
                    result["title"] = self._clean_text(title)
        
        if result["photographer"] or result["title"]:
            return result
        
        return None
    
    def _extract_photo_id(self, url: str) -> Optional[str]:
        """
        Extract Unsplash photo ID from URL.
        URLs like: unsplash.com/photos/abc123
        """
        match = re.search(r"unsplash\.com/photos/([a-zA-Z0-9_-]+)", url)
        if match:
            return match.group(1)
        return None
    
    async def _fetch_from_api(self, photo_id: str) -> Optional[dict]:
        """
        Fetch photo data from Unsplash API.
        """
        if not self.api_key:
            return None
        
        try:
            url = f"https://api.unsplash.com/photos/{photo_id}"
            headers = {
                "Authorization": f"Client-ID {self.api_key}",
                "Accept-Version": "v1",
            }
            
            async with aiohttp.ClientSession(timeout=self.timeout) as session:
                async with session.get(url, headers=headers) as response:
                    if response.status == 200:
                        data = await response.json()
                        
                        user = data.get("user", {})
                        
                        return {
                            "photographer": user.get("name"),
                            "license": "Unsplash License",
                            "title": data.get("description") or data.get("alt_description"),
                        }
        except Exception as e:
            logger.warning(f"Unsplash API error: {e}")
        
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
                        author = data.get("author") or data.get("creator")
                        if author:
                            if isinstance(author, dict):
                                photographer = author.get("name")
                            elif isinstance(author, str):
                                photographer = author
                        
                        return {
                            "photographer": self._clean_text(photographer),
                            "title": self._clean_text(data.get("name")),
                            "license": "Unsplash License",
                        }
        except (json.JSONDecodeError, KeyError, TypeError):
            pass
        
        return None
