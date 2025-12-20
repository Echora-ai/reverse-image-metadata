"""Pexels scraper using their free API."""

import os
import re
import json
import logging
import aiohttp
from typing import Optional
from bs4 import BeautifulSoup

from .base import BaseScraper

logger = logging.getLogger(__name__)


class PexelsScraper(BaseScraper):
    """
    Scraper for Pexels.
    
    Pexels has a free API that provides photographer info.
    All Pexels photos are free to use under the Pexels License.
    
    API: https://www.pexels.com/api/
    Free tier: 200 requests/hour, 20,000 requests/month
    """
    
    source_name = "pexels"
    
    def __init__(self):
        super().__init__()
        self.api_key = os.environ.get("PEXELS_API_KEY")
    
    async def _extract_attribution(self, soup: BeautifulSoup, url: str) -> Optional[dict]:
        """
        Extract attribution from Pexels page or API.
        """
        result = {
            "photographer": None,
            "license": "Pexels License",
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
            # Pexels shows photographer with link to profile
            photographer_link = soup.find("a", href=re.compile(r"/@[a-zA-Z0-9_-]+"))
            if photographer_link:
                result["photographer"] = self._clean_text(photographer_link.get_text())
        
        # Also check for "Photo by X" pattern
        if not result["photographer"]:
            text = soup.get_text()
            match = re.search(r"Photo by ([^|\n]+)", text, re.IGNORECASE)
            if match:
                result["photographer"] = self._clean_text(match.group(1))
        
        # Get title from meta
        if not result["title"]:
            og_title = soup.find("meta", {"property": "og:title"})
            if og_title:
                title = og_title.get("content", "")
                # Pexels titles often include "Photo by X"
                if " · " in title:
                    result["title"] = self._clean_text(title.split(" · ")[0])
                else:
                    result["title"] = self._clean_text(title)
        
        if result["photographer"] or result["title"]:
            return result
        
        return None
    
    def _extract_photo_id(self, url: str) -> Optional[str]:
        """
        Extract Pexels photo ID from URL.
        URLs like: pexels.com/photo/description-1234567/
        """
        match = re.search(r"pexels\.com/photo/[^/]*-(\d+)", url)
        if match:
            return match.group(1)
        # Also try direct ID pattern
        match = re.search(r"pexels\.com/photo/(\d+)", url)
        if match:
            return match.group(1)
        return None
    
    async def _fetch_from_api(self, photo_id: str) -> Optional[dict]:
        """
        Fetch photo data from Pexels API.
        """
        if not self.api_key:
            return None
        
        try:
            url = f"https://api.pexels.com/v1/photos/{photo_id}"
            headers = {
                "Authorization": self.api_key,
            }
            
            async with aiohttp.ClientSession(timeout=self.timeout) as session:
                async with session.get(url, headers=headers) as response:
                    if response.status == 200:
                        data = await response.json()
                        
                        return {
                            "photographer": data.get("photographer"),
                            "photographer_url": data.get("photographer_url"),
                            "license": "Pexels License",
                            "title": data.get("alt"),
                        }
                    elif response.status == 404:
                        logger.debug(f"Pexels photo not found: {photo_id}")
                    else:
                        logger.warning(f"Pexels API error: {response.status}")
        except Exception as e:
            logger.warning(f"Pexels API error: {e}")
        
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
                            "license": "Pexels License",
                        }
        except (json.JSONDecodeError, KeyError, TypeError):
            pass
        
        return None
