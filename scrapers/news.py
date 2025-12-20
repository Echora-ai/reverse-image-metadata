"""News site scraper for photo credits (AP, Reuters, NYT, etc.)."""

import re
import json
import logging
from typing import Optional
from bs4 import BeautifulSoup

from .base import BaseScraper

logger = logging.getLogger(__name__)


class NewsScraper(BaseScraper):
    """
    Scraper for news sites (AP Images, Reuters, NYT, etc.).
    
    News sites typically show photo credits in image captions
    or dedicated credit lines.
    """
    
    source_name = "news"
    
    async def _extract_attribution(self, soup: BeautifulSoup, url: str) -> Optional[dict]:
        """
        Extract attribution from news site page.
        """
        result = {
            "photographer": None,
            "license": "Editorial",
            "title": None,
        }
        
        # Determine which news site we're on
        url_lower = url.lower()
        
        if "apimages" in url_lower or "apnews" in url_lower:
            result = await self._extract_ap(soup, result)
        elif "reuters" in url_lower:
            result = await self._extract_reuters(soup, result)
        elif "nytimes" in url_lower:
            result = await self._extract_nyt(soup, result)
        else:
            # Generic extraction
            result = await self._extract_generic(soup, result)
        
        if result["photographer"] or result["title"]:
            return result
        
        return None
    
    async def _extract_ap(self, soup: BeautifulSoup, result: dict) -> dict:
        """
        Extract from AP Images/AP News.
        """
        # AP Images has structured metadata
        # Look for photographer credit
        credit_elem = soup.find(class_=re.compile(r"credit|photographer|byline", re.I))
        if credit_elem:
            result["photographer"] = self._clean_text(credit_elem.get_text())
        
        # Check meta tags
        if not result["photographer"]:
            author_meta = soup.find("meta", {"name": "author"})
            if author_meta:
                result["photographer"] = self._clean_text(author_meta.get("content"))
        
        # Caption often contains "(AP Photo/Photographer Name)"
        text = soup.get_text()
        ap_match = re.search(r"\(AP Photo/([^)]+)\)", text)
        if ap_match:
            result["photographer"] = self._clean_text(ap_match.group(1))
        
        # Get title
        title_meta = soup.find("meta", {"property": "og:title"})
        if title_meta:
            result["title"] = self._clean_text(title_meta.get("content"))
        
        return result
    
    async def _extract_reuters(self, soup: BeautifulSoup, result: dict) -> dict:
        """
        Extract from Reuters.
        """
        # Reuters uses structured captions
        # Look for credit in caption
        caption = soup.find(class_=re.compile(r"caption|credit", re.I))
        if caption:
            caption_text = caption.get_text()
            # Reuters format: "REUTERS/Photographer Name"
            match = re.search(r"REUTERS/([^/\n<]+)", caption_text, re.I)
            if match:
                result["photographer"] = self._clean_text(match.group(1))
        
        # Check for byline
        if not result["photographer"]:
            byline = soup.find(class_="byline")
            if byline:
                result["photographer"] = self._clean_text(byline.get_text())
        
        # Get title
        h1 = soup.find("h1")
        if h1:
            result["title"] = self._clean_text(h1.get_text())
        
        return result
    
    async def _extract_nyt(self, soup: BeautifulSoup, result: dict) -> dict:
        """
        Extract from New York Times.
        """
        # NYT shows photo credit in figcaption or credit spans
        figcaption = soup.find("figcaption")
        if figcaption:
            # Credit is often in a span with "credit" class
            credit_span = figcaption.find(class_=re.compile(r"credit", re.I))
            if credit_span:
                result["photographer"] = self._clean_text(credit_span.get_text())
        
        # Also check for photographer in image credit
        if not result["photographer"]:
            credit_patterns = [
                r"Credit[.:\s]+([^|\n<]+)",
                r"Photo[graph]*(?:er)?[:\s]+([^|\n<]+)",
                r"By[:\s]+([^|\n<]+)",
            ]
            
            text = soup.get_text()
            for pattern in credit_patterns:
                match = re.search(pattern, text, re.IGNORECASE)
                if match:
                    photographer = self._clean_text(match.group(1))
                    if photographer and len(photographer) < 100:
                        result["photographer"] = photographer
                        break
        
        # Get title
        title_meta = soup.find("meta", {"property": "og:title"})
        if title_meta:
            result["title"] = self._clean_text(title_meta.get("content"))
        
        return result
    
    async def _extract_generic(self, soup: BeautifulSoup, result: dict) -> dict:
        """
        Generic extraction for unknown news sites.
        """
        # Try common credit patterns
        credit_patterns = [
            r"\((?:Photo|Image)\s*(?:by|:)?\s*([^)]+)\)",
            r"Credit[:\s]+([^|\n<]+)",
            r"Photo(?:grapher)?[:\s]+([^|\n<]+)",
            r"By[:\s]+([^|\n<]+)",
        ]
        
        text = soup.get_text()
        for pattern in credit_patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                photographer = self._clean_text(match.group(1))
                if photographer and len(photographer) < 100:
                    # Skip if it looks like a filename or URL
                    if not re.search(r"\.(jpg|jpeg|png|gif|webp)$", photographer, re.I):
                        result["photographer"] = photographer
                        break
        
        # Get title from meta
        title_meta = soup.find("meta", {"property": "og:title"})
        if title_meta:
            result["title"] = self._clean_text(title_meta.get("content"))
        
        return result
