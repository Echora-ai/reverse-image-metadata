"""Flickr scraper using their free API."""

import os
import re
import json
import logging
import aiohttp
from typing import Optional
from bs4 import BeautifulSoup
from urllib.parse import urlparse, parse_qs

from .base import BaseScraper

logger = logging.getLogger(__name__)

# Flickr license codes to names
FLICKR_LICENSES = {
    "0": "All Rights Reserved",
    "1": "CC BY-NC-SA 2.0",
    "2": "CC BY-NC 2.0",
    "3": "CC BY-NC-ND 2.0",
    "4": "CC BY 2.0",
    "5": "CC BY-SA 2.0",
    "6": "CC BY-ND 2.0",
    "7": "No known copyright restrictions",
    "8": "United States Government Work",
    "9": "CC0 1.0",
    "10": "Public Domain Mark 1.0",
}


class FlickrScraper(BaseScraper):
    """
    Scraper for Flickr.
    
    Flickr has a free API that provides detailed photo metadata
    including photographer name and license information.
    """
    
    source_name = "flickr"
    
    def __init__(self):
        super().__init__()
        self.api_key = os.environ.get("FLICKR_API_KEY")
    
    async def _extract_attribution(self, soup: BeautifulSoup, url: str) -> Optional[dict]:
        """
        Extract attribution from Flickr page or API.
        """
        result = {
            "photographer": None,
            "license": None,
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
        # Look for owner info
        owner_link = soup.find("a", class_=re.compile(r"owner-name|photo-owner"))
        if owner_link:
            result["photographer"] = self._clean_text(owner_link.get_text())
        
        # Try meta tags
        if not result["photographer"]:
            # Flickr uses twitter:creator meta
            creator_meta = soup.find("meta", {"name": "twitter:creator"})
            if creator_meta:
                result["photographer"] = self._clean_text(creator_meta.get("content"))
        
        # Get title
        title_meta = soup.find("meta", {"property": "og:title"})
        if title_meta:
            result["title"] = self._clean_text(title_meta.get("content"))
        
        # Try to find license
        license_elem = soup.find(class_=re.compile(r"license"))
        if license_elem:
            result["license"] = self._clean_text(license_elem.get_text())
        
        if result["photographer"] or result["title"]:
            return result
        
        return None
    
    def _extract_photo_id(self, url: str) -> Optional[str]:
        """
        Extract Flickr photo ID from URL.
        URLs like: flickr.com/photos/username/12345678901
        """
        match = re.search(r"flickr\.com/photos/[^/]+/(\d+)", url)
        if match:
            return match.group(1)
        return None
    
    async def _fetch_from_api(self, photo_id: str) -> Optional[dict]:
        """
        Fetch photo info from Flickr API.
        """
        if not self.api_key:
            return None
        
        try:
            # Get photo info
            url = (
                f"https://api.flickr.com/services/rest/?"
                f"method=flickr.photos.getInfo&"
                f"api_key={self.api_key}&"
                f"photo_id={photo_id}&"
                f"format=json&nojsoncallback=1"
            )
            
            async with aiohttp.ClientSession(timeout=self.timeout) as session:
                async with session.get(url) as response:
                    if response.status == 200:
                        data = await response.json()
                        
                        if data.get("stat") == "ok":
                            photo = data.get("photo", {})
                            owner = photo.get("owner", {})
                            
                            # Get license name
                            license_id = str(photo.get("license", "0"))
                            license_name = FLICKR_LICENSES.get(license_id, "Unknown")
                            
                            return {
                                "photographer": owner.get("realname") or owner.get("username"),
                                "license": license_name,
                                "title": photo.get("title", {}).get("_content"),
                            }
        except Exception as e:
            logger.warning(f"Flickr API error: {e}")
        
        return None
