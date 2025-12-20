"""Pixabay scraper using their free API."""

import os
import re
import json
import logging
import aiohttp
from typing import Optional
from bs4 import BeautifulSoup

from .base import BaseScraper

logger = logging.getLogger(__name__)


class PixabayScraper(BaseScraper):
    """
    Scraper for Pixabay.
    
    Pixabay has a free API that provides detailed metadata.
    All Pixabay content is released under the Pixabay License
    (free for commercial use, no attribution required).
    
    API: https://pixabay.com/api/docs/
    Free tier: 5,000 requests/hour
    """
    
    source_name = "pixabay"
    
    def __init__(self):
        super().__init__()
        self.api_key = os.environ.get("PIXABAY_API_KEY")
    
    async def _extract_attribution(self, soup: BeautifulSoup, url: str) -> Optional[dict]:
        """
        Extract attribution from Pixabay page or API.
        """
        result = {
            "photographer": None,
            "license": "Pixabay License",
            "title": None,
        }
        
        # Extract image ID from URL
        image_id = self._extract_image_id(url)
        
        # Try API first if we have a key and image ID
        if self.api_key and image_id:
            api_result = await self._fetch_from_api(image_id)
            if api_result:
                return api_result
        
        # Fall back to page scraping
        # Try JSON-LD schema
        schema_data = self._extract_schema_data(soup)
        if schema_data:
            result.update(schema_data)
        
        # Look for user/photographer name
        if not result["photographer"]:
            # Pixabay shows user with link to profile
            user_link = soup.find("a", href=re.compile(r"/users/[a-zA-Z0-9_-]+"))
            if user_link:
                result["photographer"] = self._clean_text(user_link.get_text())
        
        # Check for author in page text
        if not result["photographer"]:
            text = soup.get_text()
            patterns = [
                r"Image by ([^|\n<]+)",
                r"Photo by ([^|\n<]+)",
                r"by ([^|\n<]+) from Pixabay",
            ]
            for pattern in patterns:
                match = re.search(pattern, text, re.IGNORECASE)
                if match:
                    photographer = self._clean_text(match.group(1))
                    if photographer and len(photographer) < 50:
                        result["photographer"] = photographer
                        break
        
        # Get title from meta or h1
        if not result["title"]:
            og_title = soup.find("meta", {"property": "og:title"})
            if og_title:
                title = og_title.get("content", "")
                # Clean up Pixabay title format
                title = re.sub(r" - Free \w+ on Pixabay$", "", title)
                result["title"] = self._clean_text(title)
        
        if result["photographer"] or result["title"]:
            return result
        
        return None
    
    def _extract_image_id(self, url: str) -> Optional[str]:
        """
        Extract Pixabay image ID from URL.
        URLs like: pixabay.com/photos/description-1234567/
        or: pixabay.com/images/id-1234567/
        """
        # Try various URL patterns
        patterns = [
            r"pixabay\.com/(?:photos|images|illustrations|vectors)/[^/]*-(\d+)",
            r"pixabay\.com/(?:photos|images|illustrations|vectors)/(\d+)",
            r"id-(\d+)",
        ]
        
        for pattern in patterns:
            match = re.search(pattern, url)
            if match:
                return match.group(1)
        
        return None
    
    async def _fetch_from_api(self, image_id: str) -> Optional[dict]:
        """
        Fetch image data from Pixabay API.
        """
        if not self.api_key:
            return None
        
        try:
            # Pixabay API requires searching by ID
            url = f"https://pixabay.com/api/?key={self.api_key}&id={image_id}"
            
            async with aiohttp.ClientSession(timeout=self.timeout) as session:
                async with session.get(url) as response:
                    if response.status == 200:
                        data = await response.json()
                        
                        hits = data.get("hits", [])
                        if hits:
                            hit = hits[0]
                            
                            return {
                                "photographer": hit.get("user"),
                                "photographer_id": hit.get("user_id"),
                                "license": "Pixabay License",
                                "title": hit.get("tags"),  # Pixabay uses tags as description
                                "image_type": hit.get("type"),  # photo, illustration, vector
                                "views": hit.get("views"),
                                "downloads": hit.get("downloads"),
                            }
                    elif response.status == 404:
                        logger.debug(f"Pixabay image not found: {image_id}")
                    else:
                        logger.warning(f"Pixabay API error: {response.status}")
        except Exception as e:
            logger.warning(f"Pixabay API error: {e}")
        
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
                        author = data.get("author") or data.get("creator")
                        if author:
                            if isinstance(author, dict):
                                photographer = author.get("name")
                            elif isinstance(author, str):
                                photographer = author
                        
                        return {
                            "photographer": self._clean_text(photographer),
                            "title": self._clean_text(data.get("name") or data.get("description")),
                            "license": "Pixabay License",
                        }
        except (json.JSONDecodeError, KeyError, TypeError):
            pass
        
        return None
