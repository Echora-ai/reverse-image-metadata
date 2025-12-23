"""Enhanced Reverse Image Attribution Service with Cloudflare Bypass and Pexels API"""

import asyncio
import logging
import re
import json
import aiohttp
import hashlib
import base64
import random
import os
from abc import ABC, abstractmethod
from typing import Optional
from urllib.parse import quote_plus, urlencode, urlparse, parse_qs
from dataclasses import dataclass, field
from functools import lru_cache
from concurrent.futures import ThreadPoolExecutor
from itertools import cycle
from threading import Lock

from fastapi import FastAPI, HTTPException, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, HttpUrl
from bs4 import BeautifulSoup

# cloudscraper for Cloudflare bypass
try:
    import cloudscraper
    HAS_CLOUDSCRAPER = True
except ImportError:
    HAS_CLOUDSCRAPER = False
    logging.warning("cloudscraper not installed - Cloudflare bypass unavailable")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Reverse Image Attribution API", version="5.2.1")

# ============== PEXELS API KEY ROTATION ==============
# Load API keys from environment
PEXELS_API_KEY_PRIMARY = os.getenv("PEXELS_API_KEY", "")
PEXELS_API_KEY_BACKUP = os.getenv("PEXELS_API_KEY_BACKUP", "")

# Build list of available keys (filter out empty ones)
PEXELS_API_KEYS = [k for k in [PEXELS_API_KEY_PRIMARY, PEXELS_API_KEY_BACKUP] if k]

# Thread-safe counter for round-robin key selection
class ApiKeyRotator:
    """Thread-safe round-robin API key rotator for load distribution"""
    
    def __init__(self, keys: list[str]):
        self._keys = keys if keys else []
        self._counter = 0
        self._lock = Lock()
        self._request_counts = {i: 0 for i in range(len(keys))} if keys else {}
    
    def get_next_key(self) -> tuple[str, int]:
        """Get the next API key in rotation. Returns (key, key_index)"""
        if not self._keys:
            return ("", -1)
        
        with self._lock:
            key_index = self._counter % len(self._keys)
            self._counter += 1
            self._request_counts[key_index] = self._request_counts.get(key_index, 0) + 1
            return (self._keys[key_index], key_index)
    
    def get_stats(self) -> dict:
        """Get usage stats for each key"""
        with self._lock:
            return {
                "total_keys": len(self._keys),
                "total_requests": self._counter,
                "requests_per_key": dict(self._request_counts)
            }
    
    def has_keys(self) -> bool:
        return len(self._keys) > 0

# Global rotator instance
pexels_key_rotator = ApiKeyRotator(PEXELS_API_KEYS)

logger.info(f"Pexels API Keys loaded: {len(PEXELS_API_KEYS)} keys available")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

# Thread pool for sync cloudscraper calls
executor = ThreadPoolExecutor(max_workers=4)

# ============== USER AGENTS ==============
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
]

def get_random_user_agent():
    return random.choice(USER_AGENTS)

# ============== MODELS ==============

class SearchRequest(BaseModel):
    image_url: HttpUrl
    max_results: Optional[int] = 10
    timeout: Optional[int] = 30
    engines: Optional[list[str]] = None

class ImageMetadata(BaseModel):
    type: str = "image"
    id: Optional[str] = None
    title: Optional[str] = None
    filename: Optional[str] = None
    creator: Optional[str] = None
    creator_url: Optional[str] = None
    date_created: Optional[str] = None
    description: Optional[str] = None
    keywords: list[str] = []
    location: Optional[str] = None
    copyright: Optional[str] = None
    license: Optional[str] = None
    source_url: Optional[str] = None
    source_domain: Optional[str] = None
    confidence: float = 0.0
    scrape_status: str = "pending"  # pending, success, partial, failed

class SearchResponse(BaseModel):
    found: bool
    image_url: str
    results: list[ImageMetadata]
    matched_urls: list[str] = []  # Raw URLs found by search engines
    search_engines_used: list[str]
    total_matches_found: int = 0
    error: Optional[str] = None

# ============== URL TRANSFORMATION ==============

def transform_url_to_page(url: str) -> str:
    """
    Transform CDN image URLs to their corresponding page URLs.
    E.g., images.pexels.com/photos/123/... -> www.pexels.com/photo/123/
    """
    try:
        parsed = urlparse(url)
        domain = parsed.netloc.lower()
        path = parsed.path
        
        # Pexels: images.pexels.com/photos/ID/... -> www.pexels.com/photo/ID/
        if "images.pexels.com" in domain:
            match = re.search(r'/photos/(\d+)/', path)
            if match:
                photo_id = match.group(1)
                return f"https://www.pexels.com/photo/{photo_id}/"
        
        # Unsplash: images.unsplash.com/photo-ID... -> unsplash.com/photos/ID
        if "images.unsplash.com" in domain:
            match = re.search(r'photo-([a-zA-Z0-9_-]+)', path)
            if match:
                photo_id = match.group(1)
                return f"https://unsplash.com/photos/{photo_id}"
        
        # Pixabay: cdn.pixabay.com/photo/YYYY/MM/DD/HH/MM/name-ID.ext
        if "pixabay.com" in domain and "/photo/" in path:
            match = re.search(r'-(\d+)\.[a-z]+$', path, re.IGNORECASE)
            if match:
                photo_id = match.group(1)
                return f"https://pixabay.com/photos/id-{photo_id}/"
        
        # Getty: media.gettyimages.com/id/ID/... -> gettyimages.com/detail/ID
        if "gettyimages" in domain and "/id/" in path:
            match = re.search(r'/id/(\d+)/', path)
            if match:
                photo_id = match.group(1)
                return f"https://www.gettyimages.com/detail/{photo_id}"
        
        # Adobe Stock: as1.ftcdn.net/v2/jpg/0X/XX/XX/ID.jpg -> stock.adobe.com/ID
        if "ftcdn.net" in domain:
            match = re.search(r'/(\d+)_', path)
            if match:
                photo_id = match.group(1)
                return f"https://stock.adobe.com/{photo_id}"
        
        # Shutterstock: image.shutterstock.com/z/stock-photo-ID.jpg
        if "shutterstock.com" in domain:
            match = re.search(r'-(\d+)\.[a-z]+$', path, re.IGNORECASE)
            if match:
                photo_id = match.group(1)
                return f"https://www.shutterstock.com/image-photo/{photo_id}"
        
    except Exception as e:
        logger.warning(f"URL transform failed for {url}: {e}")
    
    return url

def deduplicate_urls(urls: list[str]) -> list[str]:
    """Deduplicate URLs by their photo ID where possible."""
    seen_ids = set()
    unique_urls = []
    
    for url in urls:
        # Extract photo ID for deduplication
        photo_id = None
        
        if "pexels.com" in url:
            match = re.search(r'/photos?/(\d+)', url)
            if match:
                photo_id = f"pexels_{match.group(1)}"
        elif "unsplash.com" in url:
            match = re.search(r'photo[s-]?([a-zA-Z0-9_-]+)', url)
            if match:
                photo_id = f"unsplash_{match.group(1)}"
        elif "pixabay.com" in url:
            match = re.search(r'-(\d+)', url)
            if match:
                photo_id = f"pixabay_{match.group(1)}"
        
        if photo_id:
            if photo_id in seen_ids:
                continue
            seen_ids.add(photo_id)
        
        unique_urls.append(url)
    
    return unique_urls

# ============== SCRAPERS ==============

# PRIORITIZE PEXELS FIRST - Most common source for user images
PRIORITY_DOMAINS = [
    "pexels.com",        # PRIORITY #1 - Most common free stock source
    "unsplash.com",      # PRIORITY #2
    "pixabay.com",       # PRIORITY #3
    "flickr.com",        # PRIORITY #4
    "gettyimages.com",   # Paid sources lower priority
    "shutterstock.com", 
    "alamy.com", 
    "istockphoto.com",
    "stock.adobe.com", 
    "500px.com", 
    "depositphotos.com"
]

class BaseScraper(ABC):
    source_name: str = "unknown"
    
    def __init__(self):
        self.user_agent = get_random_user_agent()
        self.timeout = 15
    
    def _empty_metadata(self, url: str) -> dict:
        return {
            "type": "image",
            "id": None,
            "title": None,
            "filename": self._extract_filename(url),
            "creator": None,
            "creator_url": None,
            "date_created": None,
            "description": None,
            "keywords": [],
            "location": None,
            "copyright": None,
            "license": None,
            "source_url": url,
            "source_domain": self.source_name,
            "scrape_status": "pending",
        }
    
    def _extract_filename(self, url: str) -> Optional[str]:
        try:
            path = urlparse(url).path
            filename = path.split("/")[-1]
            if "." in filename and len(filename) < 200:
                return filename.split("?")[0]
        except:
            pass
        return None
    
    async def scrape(self, url: str) -> Optional[dict]:
        """Scrape with Cloudflare bypass using cloudscraper."""
        try:
            # Transform CDN URL to page URL
            page_url = transform_url_to_page(url)
            logger.info(f"Scraping: {page_url} (from {url})")
            
            html = await self._fetch_with_cloudflare_bypass(page_url)
            
            if not html:
                result = self._empty_metadata(page_url)
                result["scrape_status"] = "failed"
                return result
            
            # Check if we got a Cloudflare challenge page
            if "Just a moment" in html[:500] or "_cf_chl_opt" in html[:1000]:
                logger.warning(f"Got Cloudflare challenge for {page_url}")
                result = self._empty_metadata(page_url)
                result["scrape_status"] = "failed"
                return result
            
            # Check if we got HTML (not binary image data)
            if not html.strip().startswith("<") and not html.strip().startswith("<!"):
                logger.warning(f"Got non-HTML response for {page_url}")
                result = self._empty_metadata(page_url)
                result["scrape_status"] = "failed"
                return result
            
            soup = BeautifulSoup(html, "html.parser")
            result = await self._extract_metadata(soup, page_url)
            
            if result:
                # Determine scrape status based on extracted fields
                has_creator = bool(result.get("creator"))
                has_title = bool(result.get("title"))
                
                if has_creator and has_title:
                    result["scrape_status"] = "success"
                elif has_creator or has_title:
                    result["scrape_status"] = "partial"
                else:
                    result["scrape_status"] = "failed"
                
                return result
            else:
                result = self._empty_metadata(page_url)
                result["scrape_status"] = "failed"
                return result
            
        except Exception as e:
            logger.error(f"Error scraping {url}: {e}")
            result = self._empty_metadata(url)
            result["scrape_status"] = "failed"
            return result
    
    async def _fetch_with_cloudflare_bypass(self, url: str) -> Optional[str]:
        """Fetch URL using cloudscraper to bypass Cloudflare."""
        if HAS_CLOUDSCRAPER:
            try:
                # Run cloudscraper in thread pool since it's sync
                loop = asyncio.get_event_loop()
                html = await loop.run_in_executor(
                    executor,
                    self._sync_cloudscraper_fetch,
                    url
                )
                return html
            except Exception as e:
                logger.warning(f"cloudscraper failed for {url}: {e}")
        
        # Fallback to aiohttp
        return await self._fetch_with_aiohttp(url)
    
    def _sync_cloudscraper_fetch(self, url: str) -> Optional[str]:
        """Synchronous fetch using cloudscraper."""
        try:
            scraper = cloudscraper.create_scraper(
                browser={
                    'browser': 'chrome',
                    'platform': 'windows',
                    'desktop': True
                }
            )
            response = scraper.get(
                url,
                timeout=self.timeout,
                headers={
                    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                    'Accept-Language': 'en-US,en;q=0.9',
                    'Accept-Encoding': 'gzip, deflate, br',
                    'Connection': 'keep-alive',
                    'Upgrade-Insecure-Requests': '1',
                }
            )
            if response.status_code == 200:
                return response.text
            else:
                logger.warning(f"cloudscraper got status {response.status_code} for {url}")
                return None
        except Exception as e:
            logger.error(f"cloudscraper error for {url}: {e}")
            raise
    
    async def _fetch_with_aiohttp(self, url: str) -> Optional[str]:
        """Fallback fetch using aiohttp."""
        try:
            headers = {
                "User-Agent": self.user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Accept-Encoding": "gzip, deflate",
                "Connection": "keep-alive",
                "Upgrade-Insecure-Requests": "1",
            }
            timeout = aiohttp.ClientTimeout(total=self.timeout)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url, headers=headers, allow_redirects=True) as response:
                    if response.status != 200:
                        return None
                    
                    content_type = response.headers.get("Content-Type", "")
                    if "image/" in content_type:
                        logger.warning(f"Got image content-type for {url}")
                        return None
                    
                    return await response.text()
        except Exception as e:
            logger.error(f"aiohttp error for {url}: {e}")
            return None
    
    @abstractmethod
    async def _extract_metadata(self, soup: BeautifulSoup, url: str) -> Optional[dict]:
        pass
    
    def _clean_text(self, text: Optional[str]) -> Optional[str]:
        if not text:
            return None
        cleaned = " ".join(text.split())
        for prefix in ["Photo by ", "By ", "Credit: ", "Image by ", "© ", "Photography by "]:
            if cleaned.startswith(prefix):
                cleaned = cleaned[len(prefix):]
        return cleaned.strip() if cleaned else None
    
    def _extract_keywords(self, soup: BeautifulSoup) -> list[str]:
        keywords = []
        meta_kw = soup.find("meta", {"name": "keywords"})
        if meta_kw:
            content = meta_kw.get("content", "")
            keywords.extend([k.strip() for k in content.split(",") if k.strip()])
        for tag in soup.find_all("meta", {"property": "article:tag"}):
            kw = tag.get("content", "").strip()
            if kw and kw not in keywords:
                keywords.append(kw)
        return keywords[:20]
    
    def _extract_date(self, soup: BeautifulSoup) -> Optional[str]:
        date_props = [
            ("meta", {"property": "article:published_time"}),
            ("meta", {"property": "og:published_time"}),
            ("meta", {"name": "date"}),
            ("meta", {"name": "DC.date"}),
            ("time", {"datetime": True}),
        ]
        for tag_name, attrs in date_props:
            elem = soup.find(tag_name, attrs)
            if elem:
                date_str = elem.get("content") or elem.get("datetime")
                if date_str:
                    date_str = date_str.strip()[:10]
                    if re.match(r"\d{4}-\d{2}-\d{2}", date_str):
                        return date_str
        return None
    
    def _extract_description(self, soup: BeautifulSoup) -> Optional[str]:
        for prop in ["og:description", "description"]:
            meta = soup.find("meta", {"property": prop}) or soup.find("meta", {"name": prop})
            if meta:
                desc = meta.get("content", "").strip()
                if desc and len(desc) > 10:
                    return desc[:500]
        return None
    
    def _extract_location(self, soup: BeautifulSoup) -> Optional[str]:
        """Extract location from various sources."""
        # Try JSON-LD contentLocation
        for script in soup.find_all("script", {"type": "application/ld+json"}):
            try:
                data = json.loads(script.string) if script.string else {}
                if isinstance(data, list):
                    data = data[0] if data else {}
                loc = data.get("contentLocation")
                if isinstance(loc, dict):
                    return loc.get("name")
                elif isinstance(loc, str):
                    return loc
            except:
                pass
        
        # Try meta geo tags
        geo_tags = ["geo.placename", "geo.region", "ICBM"]
        for tag in geo_tags:
            meta = soup.find("meta", {"name": tag})
            if meta:
                loc = meta.get("content", "").strip()
                if loc:
                    return loc
        
        return None
    
    def _build_copyright(self, creator: Optional[str], year: Optional[str] = None) -> Optional[str]:
        if not creator:
            return None
        if year:
            return f"© {year} {creator}"
        return f"© {creator}"


class PexelsScraper(BaseScraper):
    source_name = "pexels"
    
    async def scrape(self, url: str) -> Optional[dict]:
        """Override scrape to try Pexels API first with round-robin key rotation"""
        # Try to extract Pexels photo ID
        photo_id = pexels_api.extract_pexels_id(url)
        
        if photo_id and pexels_key_rotator.has_keys():
            logger.info(f"Attempting Pexels API lookup for photo ID: {photo_id}")
            api_result = await pexels_api.search_by_id(photo_id)
            
            if api_result and api_result.get("creator"):
                logger.info(f"Got Pexels metadata via API for {photo_id}: {api_result.get('creator')}")
                return api_result
            else:
                logger.info(f"Pexels API lookup failed for {photo_id}, falling back to scraping")
        
        # Fall back to regular scraping if API didn't work
        return await super().scrape(url)
    
    async def _extract_metadata(self, soup: BeautifulSoup, url: str) -> Optional[dict]:
        result = self._empty_metadata(url)
        result["license"] = "Pexels License"
        
        # Try JSON-LD first
        for script in soup.find_all("script", {"type": "application/ld+json"}):
            try:
                data = json.loads(script.string) if script.string else {}
                if isinstance(data, list):
                    data = data[0] if data else {}
                if data.get("@type") in ["ImageObject", "Photograph"]:
                    author = data.get("author") or data.get("creator")
                    if isinstance(author, dict):
                        result["creator"] = self._clean_text(author.get("name"))
                        result["creator_url"] = author.get("url")
                    elif isinstance(author, str):
                        result["creator"] = self._clean_text(author)
                    result["title"] = self._clean_text(data.get("name"))
                    result["description"] = data.get("description")
                    
                    date_created = data.get("dateCreated") or data.get("uploadDate")
                    if date_created:
                        result["date_created"] = date_created[:10]
                    
                    # Location from JSON-LD
                    loc = data.get("contentLocation")
                    if isinstance(loc, dict):
                        result["location"] = loc.get("name")
                    elif isinstance(loc, str):
                        result["location"] = loc
                    
                    keywords = data.get("keywords")
                    if isinstance(keywords, list):
                        result["keywords"] = keywords[:20]
                    elif isinstance(keywords, str):
                        result["keywords"] = [k.strip() for k in keywords.split(",")][:20]
                    break
            except:
                pass
        
        # Fallback: find photographer link - Pexels uses /@username pattern
        if not result["creator"]:
            # Priority 1: Look for the main photographer link with heading (usually the primary author link)
            for heading in soup.find_all(["h1", "h2", "h3", "h4"]):
                parent_link = heading.find_parent("a")
                if parent_link:
                    href = parent_link.get("href", "")
                    if "/@" in href or "/users/" in href:
                        creator_name = heading.get_text().strip()
                        if creator_name and len(creator_name) < 100:
                            result["creator"] = self._clean_text(creator_name)
                            result["creator_url"] = f"https://www.pexels.com{href}" if href.startswith("/") else href
                            break
            
            # Priority 2: Try various patterns for photographer links
            if not result["creator"]:
                patterns = [
                    r"^/@([a-zA-Z0-9_-]+)$",  # /@username (exact match)
                    r"^/@([a-zA-Z0-9_-]+)/$",  # /@username/ (with trailing slash)
                ]
                for pattern in patterns:
                    link = soup.find("a", href=re.compile(pattern))
                    if link:
                        creator_name = link.get_text().strip()
                        if creator_name and len(creator_name) < 100:
                            result["creator"] = self._clean_text(creator_name)
                            href = link.get("href", "")
                            result["creator_url"] = f"https://www.pexels.com{href}" if href.startswith("/") else href
                            break
        
        # Fallback: look for photographer in page text
        if not result["creator"]:
            # Look for "Photo by X" pattern in the page
            for elem in soup.find_all(["span", "div", "a", "p"]):
                text = elem.get_text().strip()
                match = re.search(r"(?:Photo|Image|Taken)\s+by\s+([A-Za-z][A-Za-z0-9\s_-]{1,50})", text, re.IGNORECASE)
                if match:
                    result["creator"] = self._clean_text(match.group(1))
                    break
        
        # Title from og:title
        if not result["title"]:
            og = soup.find("meta", {"property": "og:title"})
            if og:
                title = og.get("content", "")
                # Clean up Pexels suffix
                title = re.sub(r"\s*[·|]\s*Free.*$", "", title, flags=re.IGNORECASE)
                title = re.sub(r"\s*[·|]\s*Pexels.*$", "", title, flags=re.IGNORECASE)
                result["title"] = self._clean_text(title)
        
        # Location extraction - Multiple strategies for Pexels
        if not result["location"]:
            # Strategy 1: Look for location patterns in text containing city/state/country patterns
            # Pexels shows location like "Tampa, FL, United States"
            location_patterns = [
                # US locations: City, ST, United States or City, ST, USA
                r"([A-Z][a-zA-Z\s]+,\s*[A-Z]{2},\s*(?:United States|USA))",
                # International: City, Country
                r"([A-Z][a-zA-Z\s]+,\s*[A-Z][a-zA-Z\s]+(?:,\s*[A-Z][a-zA-Z\s]+)?)",
            ]
            
            # Search in all text-containing elements
            for elem in soup.find_all(["span", "div", "p", "a"]):
                text = elem.get_text().strip()
                # Skip if too long (likely not a location)
                if len(text) > 100:
                    continue
                    
                # Check if this looks like a location (contains comma-separated parts)
                if "," in text:
                    parts = [p.strip() for p in text.split(",")]
                    # Location usually has 2-3 parts (city, state/country, or city, state, country)
                    if 2 <= len(parts) <= 4:
                        # Verify it looks like a location (first part is capitalized, etc.)
                        first_part = parts[0]
                        if first_part and first_part[0].isupper() and len(first_part) > 1:
                            # Check if it contains common location indicators
                            text_lower = text.lower()
                            if any(indicator in text_lower for indicator in [
                                "united states", "usa", "uk", "canada", "australia", 
                                "germany", "france", "italy", "spain", "japan",
                                ", fl", ", ca", ", ny", ", tx", ", wa", ", or",  # US state codes
                                ", on", ", bc", ", ab", ", qc",  # Canadian provinces
                            ]) or (len(parts) >= 2 and len(parts[1].strip()) == 2):  # State code
                                result["location"] = self._clean_text(text)
                                break
            
            # Strategy 2: Look for elements with location-related classes or data attributes
            if not result["location"]:
                for elem in soup.find_all(True, attrs={"data-testid": re.compile(r"location", re.IGNORECASE)}):
                    text = elem.get_text().strip()
                    if text and len(text) < 100:
                        result["location"] = self._clean_text(text)
                        break
            
            # Strategy 3: Look for geo-related meta tags
            if not result["location"]:
                geo_meta = soup.find("meta", {"property": "og:location"})
                if geo_meta:
                    result["location"] = geo_meta.get("content", "").strip()
            
            # Strategy 4: Check for location in specific Pexels patterns
            if not result["location"]:
                page_text = soup.get_text()
                location_text_patterns = [
                    r"📍\s*([^<\n]+)",  # Pin emoji
                    r"Location:\s*([^<\n]+)",
                    r"Taken (?:in|at)\s+([A-Z][^<\n]{3,50})",
                ]
                for pattern in location_text_patterns:
                    match = re.search(pattern, page_text, re.IGNORECASE)
                    if match:
                        loc_text = match.group(1).strip()
                        # Verify it looks like a location
                        if "," in loc_text or any(c in loc_text.lower() for c in ["city", "state", "country"]):
                            result["location"] = self._clean_text(loc_text)
                            break
        
        if not result["description"]:
            result["description"] = self._extract_description(soup)
        
        if not result["keywords"]:
            result["keywords"] = self._extract_keywords(soup)
        
        if not result["date_created"]:
            result["date_created"] = self._extract_date(soup)
        
        year = result["date_created"][:4] if result["date_created"] else None
        result["copyright"] = self._build_copyright(result["creator"], year)
        
        return result


class UnsplashScraper(BaseScraper):
    source_name = "unsplash"
    
    async def _extract_metadata(self, soup: BeautifulSoup, url: str) -> Optional[dict]:
        result = self._empty_metadata(url)
        result["license"] = "Unsplash License"
        
        for script in soup.find_all("script", {"type": "application/ld+json"}):
            try:
                data = json.loads(script.string) if script.string else {}
                if data.get("@type") == "ImageObject":
                    author = data.get("author")
                    if isinstance(author, dict):
                        result["creator"] = self._clean_text(author.get("name"))
                        result["creator_url"] = author.get("url")
                    result["title"] = self._clean_text(data.get("name"))
                    result["description"] = data.get("description")
                    
                    date_created = data.get("dateCreated") or data.get("uploadDate")
                    if date_created:
                        result["date_created"] = date_created[:10]
                    
                    loc = data.get("contentLocation")
                    if isinstance(loc, dict):
                        result["location"] = loc.get("name")
                    
                    keywords = data.get("keywords")
                    if isinstance(keywords, list):
                        result["keywords"] = keywords[:20]
                    elif isinstance(keywords, str):
                        result["keywords"] = [k.strip() for k in keywords.split(",")][:20]
                    break
            except:
                pass
        
        if not result["creator"]:
            meta = soup.find("meta", {"name": "twitter:creator"})
            if meta:
                result["creator"] = self._clean_text(meta.get("content", "").replace("@", ""))
        
        if not result["description"]:
            result["description"] = self._extract_description(soup)
        
        if not result["keywords"]:
            result["keywords"] = self._extract_keywords(soup)
        
        if not result["location"]:
            result["location"] = self._extract_location(soup)
        
        year = result["date_created"][:4] if result["date_created"] else None
        result["copyright"] = self._build_copyright(result["creator"], year)
        
        return result


class PixabayScraper(BaseScraper):
    source_name = "pixabay"
    
    async def _extract_metadata(self, soup: BeautifulSoup, url: str) -> Optional[dict]:
        result = self._empty_metadata(url)
        result["license"] = "Pixabay License"
        
        for link in soup.find_all("a", href=re.compile(r"/users/")):
            text = link.get_text().strip()
            if text and len(text) < 50:
                result["creator"] = self._clean_text(text)
                result["creator_url"] = f"https://pixabay.com{link.get('href', '')}"
                break
        
        og = soup.find("meta", {"property": "og:title"})
        if og:
            result["title"] = self._clean_text(og.get("content", ""))
        
        result["description"] = self._extract_description(soup)
        result["keywords"] = self._extract_keywords(soup)
        result["date_created"] = self._extract_date(soup)
        result["location"] = self._extract_location(soup)
        
        year = result["date_created"][:4] if result["date_created"] else None
        result["copyright"] = self._build_copyright(result["creator"], year)
        
        return result


class FlickrScraper(BaseScraper):
    source_name = "flickr"
    
    async def _extract_metadata(self, soup: BeautifulSoup, url: str) -> Optional[dict]:
        result = self._empty_metadata(url)
        
        owner_link = soup.find("a", class_=re.compile(r"owner-name|attribution"))
        if owner_link:
            result["creator"] = self._clean_text(owner_link.get_text())
            href = owner_link.get("href", "")
            if href:
                result["creator_url"] = f"https://www.flickr.com{href}" if href.startswith("/") else href
        
        title_tag = soup.find("h1", class_=re.compile(r"photo-title"))
        if title_tag:
            result["title"] = self._clean_text(title_tag.get_text())
        
        license_link = soup.find("a", href=re.compile(r"creativecommons.org"))
        if license_link:
            result["license"] = self._clean_text(license_link.get_text()) or "Creative Commons"
        
        result["description"] = self._extract_description(soup)
        result["keywords"] = self._extract_keywords(soup)
        result["date_created"] = self._extract_date(soup)
        result["location"] = self._extract_location(soup)
        
        year = result["date_created"][:4] if result["date_created"] else None
        result["copyright"] = self._build_copyright(result["creator"], year)
        
        return result


class ShutterstockScraper(BaseScraper):
    source_name = "shutterstock"
    
    async def _extract_metadata(self, soup: BeautifulSoup, url: str) -> Optional[dict]:
        result = self._empty_metadata(url)
        result["license"] = "Shutterstock License (Paid)"
        
        contrib = soup.find("a", href=re.compile(r"/g/[^/]+"))
        if contrib:
            result["creator"] = self._clean_text(contrib.get_text())
            result["creator_url"] = f"https://www.shutterstock.com{contrib.get('href', '')}"
        
        og = soup.find("meta", {"property": "og:title"})
        if og:
            title = og.get("content", "")
            title = re.sub(r"\s*[-|]\s*Shutterstock.*$", "", title, flags=re.IGNORECASE)
            result["title"] = self._clean_text(title)
        
        result["description"] = self._extract_description(soup)
        result["keywords"] = self._extract_keywords(soup)
        result["date_created"] = self._extract_date(soup)
        result["location"] = self._extract_location(soup)
        
        year = result["date_created"][:4] if result["date_created"] else None
        result["copyright"] = self._build_copyright(result["creator"], year)
        
        return result


class GettyImagesScraper(BaseScraper):
    source_name = "gettyimages"
    
    async def _extract_metadata(self, soup: BeautifulSoup, url: str) -> Optional[dict]:
        result = self._empty_metadata(url)
        result["license"] = "Getty Images License (Paid)"
        
        for script in soup.find_all("script", {"type": "application/ld+json"}):
            try:
                data = json.loads(script.string) if script.string else {}
                if data.get("@type") == "ImageObject":
                    author = data.get("author")
                    if isinstance(author, dict):
                        result["creator"] = self._clean_text(author.get("name"))
                        result["creator_url"] = author.get("url")
                    elif isinstance(author, str):
                        result["creator"] = self._clean_text(author)
                    result["title"] = self._clean_text(data.get("name"))
                    result["description"] = data.get("description")
                    
                    date_created = data.get("dateCreated") or data.get("uploadDate")
                    if date_created:
                        result["date_created"] = date_created[:10]
                    
                    keywords = data.get("keywords")
                    if isinstance(keywords, list):
                        result["keywords"] = keywords[:20]
                    elif isinstance(keywords, str):
                        result["keywords"] = [k.strip() for k in keywords.split(",")][:20]
                    break
            except:
                pass
        
        if not result["description"]:
            result["description"] = self._extract_description(soup)
        
        if not result["keywords"]:
            result["keywords"] = self._extract_keywords(soup)
        
        if not result["location"]:
            result["location"] = self._extract_location(soup)
        
        year = result["date_created"][:4] if result["date_created"] else None
        result["copyright"] = self._build_copyright(result["creator"], year)
        
        return result


class GenericScraper(BaseScraper):
    source_name = "generic"
    
    async def _extract_metadata(self, soup: BeautifulSoup, url: str) -> Optional[dict]:
        result = self._empty_metadata(url)
        
        # Try JSON-LD
        for script in soup.find_all("script", {"type": "application/ld+json"}):
            try:
                data = json.loads(script.string) if script.string else {}
                if isinstance(data, list):
                    for item in data:
                        if item.get("@type") in ["ImageObject", "Photograph", "CreativeWork"]:
                            data = item
                            break
                if data.get("@type") in ["ImageObject", "Photograph", "CreativeWork"]:
                    author = data.get("author") or data.get("creator")
                    if isinstance(author, dict):
                        result["creator"] = self._clean_text(author.get("name"))
                        result["creator_url"] = author.get("url")
                    elif isinstance(author, str):
                        result["creator"] = self._clean_text(author)
                    result["title"] = self._clean_text(data.get("name") or data.get("headline"))
                    result["description"] = data.get("description")
                    
                    date_created = data.get("dateCreated") or data.get("uploadDate") or data.get("datePublished")
                    if date_created:
                        result["date_created"] = date_created[:10]
                    
                    keywords = data.get("keywords")
                    if isinstance(keywords, list):
                        result["keywords"] = keywords[:20]
                    elif isinstance(keywords, str):
                        result["keywords"] = [k.strip() for k in keywords.split(",")][:20]
                    
                    loc = data.get("contentLocation")
                    if isinstance(loc, dict):
                        result["location"] = loc.get("name")
                    elif isinstance(loc, str):
                        result["location"] = loc
                    
                    license_info = data.get("license")
                    if license_info:
                        result["license"] = license_info if isinstance(license_info, str) else str(license_info)
                    break
            except:
                pass
        
        if not result["title"]:
            og = soup.find("meta", {"property": "og:title"})
            if og:
                result["title"] = self._clean_text(og.get("content", ""))
        
        if not result["creator"]:
            author = soup.find("meta", {"name": "author"})
            if author:
                result["creator"] = self._clean_text(author.get("content", ""))
        
        if not result["creator"]:
            dc = soup.find("meta", {"name": "DC.creator"})
            if dc:
                result["creator"] = self._clean_text(dc.get("content", ""))
        
        if not result["description"]:
            result["description"] = self._extract_description(soup)
        
        if not result["keywords"]:
            result["keywords"] = self._extract_keywords(soup)
        
        if not result["date_created"]:
            result["date_created"] = self._extract_date(soup)
        
        if not result["location"]:
            result["location"] = self._extract_location(soup)
        
        year = result["date_created"][:4] if result["date_created"] else None
        result["copyright"] = self._build_copyright(result["creator"], year)
        
        return result


def get_scraper_for_url(url: str) -> BaseScraper:
    try:
        domain = urlparse(url).netloc.lower().replace("www.", "")
        if "pexels.com" in domain or "images.pexels.com" in domain:
            return PexelsScraper()
        if "pixabay.com" in domain or "cdn.pixabay.com" in domain:
            return PixabayScraper()
        if "unsplash.com" in domain or "images.unsplash.com" in domain:
            return UnsplashScraper()
        if "flickr.com" in domain:
            return FlickrScraper()
        if "shutterstock.com" in domain:
            return ShutterstockScraper()
        if "gettyimages.com" in domain:
            return GettyImagesScraper()
        return GenericScraper()
    except:
        return GenericScraper()


# ============== REVERSE SEARCH ENGINES ==============

@dataclass
class SearchResult:
    urls: list[str] = field(default_factory=list)
    engines_used: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    page_matches: list[dict] = field(default_factory=list)


class ReverseImageSearch:
    def __init__(self):
        self.user_agent = get_random_user_agent()
        self.timeout = aiohttp.ClientTimeout(total=30)
    
    async def search(self, image_url: str = None, image_bytes: bytes = None, 
                     max_results: int = 10, timeout: int = 30,
                     engines: list[str] = None) -> SearchResult:
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        result = SearchResult()
        
        if engines is None:
            engines = ["yandex", "bing"]  # Google Lens is harder to scrape reliably
        
        tasks = []
        if "google" in engines:
            tasks.append(self._search_google(image_url, image_bytes))
        if "yandex" in engines:
            tasks.append(self._search_yandex(image_url, image_bytes))
        if "bing" in engines:
            tasks.append(self._search_bing(image_url, image_bytes))
        
        search_results = await asyncio.gather(*tasks, return_exceptions=True)
        
        seen = set()
        for sr in search_results:
            if isinstance(sr, Exception):
                result.errors.append(str(sr))
                logger.warning(f"Search engine error: {sr}")
                continue
            engine, urls, matches = sr
            result.engines_used.append(engine)
            result.page_matches.extend(matches)
            for url in urls:
                if url not in seen and len(result.urls) < max_results * 3:
                    seen.add(url)
                    result.urls.append(url)
        
        logger.info(f"Found {len(result.urls)} URLs from {result.engines_used}")
        return result
    
    async def _search_google(self, image_url: str = None, image_bytes: bytes = None) -> tuple[str, list[str], list[dict]]:
        urls = []
        matches = []
        
        if image_url:
            encoded = quote_plus(image_url)
            search_url = f"https://lens.google.com/uploadbyurl?url={encoded}"
        else:
            raise Exception("Google: File upload requires temp hosting")
        
        headers = {
            "User-Agent": self.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }
        
        try:
            async with aiohttp.ClientSession(timeout=self.timeout) as session:
                async with session.get(search_url, headers=headers, allow_redirects=True) as resp:
                    if resp.status != 200:
                        raise Exception(f"Google Lens: HTTP {resp.status}")
                    html = await resp.text()
            
            soup = BeautifulSoup(html, "html.parser")
            
            for link in soup.find_all("a", href=True):
                href = link["href"]
                if any(x in href.lower() for x in ["google.com", "google.co", "gstatic.com", "googleapis.com"]):
                    continue
                if "/url?q=" in href:
                    parsed = parse_qs(urlparse(href).query)
                    if "q" in parsed:
                        actual_url = parsed["q"][0]
                        if actual_url.startswith("http"):
                            urls.append(actual_url)
                            parent = link.find_parent(["div", "li"])
                            if parent:
                                text = parent.get_text(strip=True)[:200]
                                matches.append({"url": actual_url, "context": text, "engine": "google"})
                elif href.startswith("http"):
                    urls.append(href)
            
        except Exception as e:
            logger.error(f"Google search error: {e}")
            raise Exception(f"Google: {str(e)}")
        
        return ("google", list(dict.fromkeys(urls))[:25], matches)
    
    async def _search_yandex(self, image_url: str = None, image_bytes: bytes = None) -> tuple[str, list[str], list[dict]]:
        urls = []
        matches = []
        
        headers = {
            "User-Agent": self.user_agent,
            "Accept": "text/html,application/xhtml+xml",
        }
        
        try:
            if image_url:
                encoded = quote_plus(image_url)
                url = f"https://yandex.com/images/search?rpt=imageview&url={encoded}"
                
                async with aiohttp.ClientSession(timeout=self.timeout) as session:
                    async with session.get(url, headers=headers) as resp:
                        if resp.status != 200:
                            raise Exception(f"Yandex: HTTP {resp.status}")
                        html = await resp.text()
            
            elif image_bytes:
                url = "https://yandex.com/images/search"
                form = aiohttp.FormData()
                form.add_field('upfile', image_bytes, filename='image.jpg', content_type='image/jpeg')
                form.add_field('rpt', 'imageview')
                
                async with aiohttp.ClientSession(timeout=self.timeout) as session:
                    async with session.post(url, data=form, headers=headers, allow_redirects=True) as resp:
                        if resp.status != 200:
                            raise Exception(f"Yandex upload: HTTP {resp.status}")
                        html = await resp.text()
            else:
                raise Exception("Yandex: No image provided")
            
            soup = BeautifulSoup(html, "html.parser")
            
            for link in soup.find_all("a", href=True):
                href = link["href"]
                if href.startswith("http") and "yandex" not in href.lower():
                    urls.append(href)
                    text = link.get_text(strip=True)[:200]
                    if text:
                        matches.append({"url": href, "context": text, "engine": "yandex"})
            
        except Exception as e:
            logger.error(f"Yandex error: {e}")
            raise Exception(f"Yandex: {str(e)}")
        
        return ("yandex", list(dict.fromkeys(urls))[:25], matches)
    
    async def _search_bing(self, image_url: str = None, image_bytes: bytes = None) -> tuple[str, list[str], list[dict]]:
        urls = []
        matches = []
        
        headers = {
            "User-Agent": self.user_agent,
            "Accept": "text/html,application/xhtml+xml",
        }
        
        try:
            if image_url:
                encoded = quote_plus(image_url)
                url = f"https://www.bing.com/images/search?view=detailv2&iss=sbi&q=imgurl:{encoded}"
                
                async with aiohttp.ClientSession(timeout=self.timeout) as session:
                    async with session.get(url, headers=headers, allow_redirects=True) as resp:
                        if resp.status != 200:
                            raise Exception(f"Bing: HTTP {resp.status}")
                        html = await resp.text()
            
            elif image_bytes:
                url = "https://www.bing.com/images/search?view=detailv2&iss=sbiupload"
                form = aiohttp.FormData()
                form.add_field('imageBin', base64.b64encode(image_bytes).decode('utf-8'))
                
                async with aiohttp.ClientSession(timeout=self.timeout) as session:
                    async with session.post(url, data=form, headers=headers, allow_redirects=True) as resp:
                        if resp.status != 200:
                            raise Exception(f"Bing upload: HTTP {resp.status}")
                        html = await resp.text()
            else:
                raise Exception("Bing: No image provided")
            
            soup = BeautifulSoup(html, "html.parser")
            
            for link in soup.find_all("a", href=True):
                href = link["href"]
                if href.startswith("http"):
                    if not any(x in href.lower() for x in ["bing.com", "microsoft.com", "msn.com"]):
                        urls.append(href)
                        text = link.get_text(strip=True)[:200]
                        if text:
                            matches.append({"url": href, "context": text, "engine": "bing"})
            
        except Exception as e:
            logger.error(f"Bing error: {e}")
            raise Exception(f"Bing: {str(e)}")
        
        return ("bing", list(dict.fromkeys(urls))[:25], matches)


# ============== PEXELS API DIRECT SEARCH ==============

class PexelsApiSearch:
    """
    Direct Pexels API search with round-robin key rotation.
    Uses alternating API keys to maximize throughput for batch processing.
    Designed for processing 400+ images per hour.
    """
    
    def __init__(self):
        self.base_url = "https://api.pexels.com/v1"
        self.timeout = aiohttp.ClientTimeout(total=15)
    
    async def search_by_id(self, photo_id: str) -> Optional[dict]:
        """Get photo details directly from Pexels API by photo ID"""
        if not pexels_key_rotator.has_keys():
            logger.warning("No Pexels API keys available")
            return None
        
        api_key, key_index = pexels_key_rotator.get_next_key()
        logger.info(f"Using Pexels API key #{key_index + 1} for photo {photo_id}")
        
        headers = {
            "Authorization": api_key,
            "User-Agent": get_random_user_agent()
        }
        
        try:
            async with aiohttp.ClientSession(timeout=self.timeout) as session:
                async with session.get(
                    f"{self.base_url}/photos/{photo_id}",
                    headers=headers
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return self._parse_photo_response(data)
                    elif resp.status == 429:
                        logger.warning(f"Rate limited on Pexels API key #{key_index + 1}")
                        return None
                    else:
                        logger.warning(f"Pexels API returned {resp.status} for photo {photo_id}")
                        return None
        except Exception as e:
            logger.error(f"Pexels API error for photo {photo_id}: {e}")
            return None
    
    async def search_similar(self, query: str, per_page: int = 10) -> list[dict]:
        """Search Pexels for similar images by keyword"""
        if not pexels_key_rotator.has_keys():
            return []
        
        api_key, key_index = pexels_key_rotator.get_next_key()
        logger.info(f"Using Pexels API key #{key_index + 1} for search: {query}")
        
        headers = {
            "Authorization": api_key,
            "User-Agent": get_random_user_agent()
        }
        
        params = {
            "query": query,
            "per_page": per_page
        }
        
        try:
            async with aiohttp.ClientSession(timeout=self.timeout) as session:
                async with session.get(
                    f"{self.base_url}/search",
                    headers=headers,
                    params=params
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return [self._parse_photo_response(photo) for photo in data.get("photos", [])]
                    elif resp.status == 429:
                        logger.warning(f"Rate limited on Pexels API key #{key_index + 1}")
                        return []
                    else:
                        return []
        except Exception as e:
            logger.error(f"Pexels search error: {e}")
            return []
    
    def _parse_photo_response(self, data: dict) -> dict:
        """Parse Pexels API response into our metadata format"""
        return {
            "type": "image",
            "id": str(data.get("id", "")),
            "title": data.get("alt", "") or f"Pexels Photo {data.get('id', '')}",
            "filename": None,
            "creator": data.get("photographer", ""),
            "creator_url": data.get("photographer_url", ""),
            "date_created": None,
            "description": data.get("alt", ""),
            "keywords": [],
            "location": None,
            "copyright": f"© {data.get('photographer', 'Unknown')}",
            "license": "Pexels License",
            "source_url": data.get("url", ""),
            "source_domain": "pexels",
            "confidence": 0.95,  # High confidence for direct API lookup
            "scrape_status": "success",
            "avg_color": data.get("avg_color"),
            "width": data.get("width"),
            "height": data.get("height"),
            "src": data.get("src", {})
        }
    
    def extract_pexels_id(self, url: str) -> Optional[str]:
        """Extract Pexels photo ID from various URL formats"""
        patterns = [
            r"pexels\.com/photo/[^/]+\-(\d+)",  # www.pexels.com/photo/title-12345/
            r"pexels\.com/photo/(\d+)",         # www.pexels.com/photo/12345/
            r"images\.pexels\.com/photos/(\d+)/",  # images.pexels.com/photos/12345/...
            r"pexels-photo-(\d+)",              # pexels-photo-12345.jpeg
        ]
        
        for pattern in patterns:
            match = re.search(pattern, url, re.IGNORECASE)
            if match:
                return match.group(1)
        return None


# Global Pexels API instance
pexels_api = PexelsApiSearch()


# ============== ENDPOINTS ==============

@app.get("/")
async def root():
    # Build list of available key labels
    pexels_keys_labels = []
    if PEXELS_API_KEY_PRIMARY:
        pexels_keys_labels.append("primary")
    if PEXELS_API_KEY_BACKUP:
        pexels_keys_labels.append("backup")
    
    return {
        "status": "healthy", 
        "service": "reverse-image-attribution", 
        "version": "5.2.1",
        "optimizations": [
            "Pexels PRIORITIZED in domain order and result sorting",
            "Pexels filename detection in any URL (GCS, S3, etc.)",
            "Parallel scraping with asyncio.gather",
            "Early exit when creator found",
            "Reduced scrape limit (4 URLs instead of 8)"
        ],
        "cloudscraper_available": HAS_CLOUDSCRAPER,
        "pexels_api_keys": pexels_keys_labels,
        "pexels_key_rotation": "round-robin" if len(pexels_keys_labels) > 1 else "single",
        "env_vars_needed": ["PEXELS_API_KEY", "PEXELS_API_KEY_BACKUP"]
    }

@app.get("/health")
async def health():
    return {
        "status": "ok", 
        "cloudscraper": HAS_CLOUDSCRAPER,
        "pexels_keys_available": pexels_key_rotator.has_keys(),
        "pexels_key_stats": pexels_key_rotator.get_stats()
    }


@app.post("/reverse-search", response_model=SearchResponse)
async def reverse_search(request: SearchRequest):
    image_url = str(request.image_url)
    logger.info(f"Reverse search for URL: {image_url}")
    
    return await _perform_search(
        image_url=image_url,
        max_results=request.max_results,
        timeout=request.timeout,
        engines=request.engines
    )


@app.post("/reverse-search/upload", response_model=SearchResponse)
async def reverse_search_upload(
    file: UploadFile = File(...),
    max_results: int = Form(default=10),
    timeout: int = Form(default=30),
    engines: str = Form(default="yandex,bing")
):
    logger.info(f"Reverse search for uploaded file: {file.filename}")
    
    image_bytes = await file.read()
    
    if len(image_bytes) > 10 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="File too large (max 10MB)")
    
    content_type = file.content_type or ""
    if not content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="File must be an image")
    
    engine_list = [e.strip() for e in engines.split(",") if e.strip()]
    
    return await _perform_search(
        image_bytes=image_bytes,
        max_results=max_results,
        timeout=timeout,
        engines=engine_list
    )


async def _perform_search(
    image_url: str = None,
    image_bytes: bytes = None,
    max_results: int = 10,
    timeout: int = 30,
    engines: list[str] = None
) -> SearchResponse:
    
    try:
        # ============== OPTIMIZATION 1: Check for Pexels ID in input URL FIRST ==============
        # This catches filenames like "pexels-photo-6349487.jpg" in any URL (GCS, S3, etc.)
        if image_url:
            pexels_id = pexels_api.extract_pexels_id(image_url)
            if pexels_id:
                logger.info(f"Detected Pexels image from input URL: {pexels_id}")
                pexels_result = await pexels_api.search_by_id(pexels_id)
                
                if pexels_result and pexels_result.get("creator"):
                    logger.info(f"Fast path: Got Pexels metadata for {pexels_id} - {pexels_result.get('creator')}")
                    # Return immediately - no need for slow reverse search!
                    return SearchResponse(
                        found=True,
                        image_url=image_url,
                        results=[ImageMetadata(
                            type="image",
                            id=pexels_result.get("id"),
                            title=pexels_result.get("title"),
                            filename=pexels_result.get("filename"),
                            creator=pexels_result.get("creator"),
                            creator_url=pexels_result.get("creator_url"),
                            date_created=pexels_result.get("date_created"),
                            description=pexels_result.get("description"),
                            keywords=pexels_result.get("keywords", []),
                            location=pexels_result.get("location"),
                            copyright=pexels_result.get("copyright"),
                            license=pexels_result.get("license"),
                            source_url=pexels_result.get("source_url"),
                            source_domain="pexels",
                            confidence=0.95,
                            scrape_status="success"
                        )],
                        matched_urls=[],
                        search_engines_used=["pexels_api_direct"],
                        total_matches_found=1
                    )
                else:
                    logger.info(f"Pexels API lookup failed for {pexels_id}, falling back to reverse search")
        
        # ============== Standard reverse search path ==============
        search_engine = ReverseImageSearch()
        search_results = await search_engine.search(
            image_url=image_url,
            image_bytes=image_bytes,
            max_results=max_results,
            timeout=timeout,
            engines=engines
        )
        
        # Store matched URLs before deduplication
        raw_matched_urls = list(search_results.urls)
        
        if not search_results.urls:
            return SearchResponse(
                found=False, 
                image_url=image_url or "uploaded_file",
                results=[], 
                matched_urls=raw_matched_urls,
                search_engines_used=search_results.engines_used,
                error="; ".join(search_results.errors) if search_results.errors else None
            )
        
        # Deduplicate URLs
        unique_urls = deduplicate_urls(search_results.urls)
        
        # Prioritize known stock photo domains - PEXELS IS FIRST
        prioritized = []
        for url in unique_urls:
            priority = 0
            for i, domain in enumerate(PRIORITY_DOMAINS):
                if domain in url.lower():
                    priority = len(PRIORITY_DOMAINS) - i
                    break
            prioritized.append((url, priority))
        
        prioritized.sort(key=lambda x: -x[1])
        
        # ============== OPTIMIZATION 2: Reduced scrape limit (8 -> 4) ==============
        scrape_limit = min(4, len(prioritized))
        
        # ============== OPTIMIZATION 3: Parallel scraping with asyncio.gather ==============
        async def scrape_one(url: str, priority: int) -> Optional[ImageMetadata]:
            scraper = get_scraper_for_url(url)
            try:
                metadata = await scraper.scrape(url)
                
                if metadata:
                    # Calculate confidence score
                    score = 0.0
                    score += (priority / len(PRIORITY_DOMAINS)) * 0.3
                    score += 0.3 if metadata.get("creator") else 0
                    score += 0.15 if metadata.get("license") else 0
                    score += 0.1 if metadata.get("title") else 0
                    score += 0.05 if metadata.get("date_created") else 0
                    score += 0.05 if metadata.get("keywords") else 0
                    score += 0.05 if metadata.get("location") else 0
                    
                    # Minimum confidence for matched URLs
                    score = max(score, 0.1)
                    
                    url_hash = hashlib.md5(url.encode()).hexdigest()[:8]
                    
                    return ImageMetadata(
                        type="image",
                        id=f"img_{url_hash}",
                        title=metadata.get("title"),
                        filename=metadata.get("filename"),
                        creator=metadata.get("creator"),
                        creator_url=metadata.get("creator_url"),
                        date_created=metadata.get("date_created"),
                        description=metadata.get("description"),
                        keywords=metadata.get("keywords", []),
                        location=metadata.get("location"),
                        copyright=metadata.get("copyright"),
                        license=metadata.get("license"),
                        source_url=metadata.get("source_url", url),
                        source_domain=scraper.source_name,
                        confidence=min(score, 1.0),
                        scrape_status=metadata.get("scrape_status", "unknown")
                    )
            except Exception as e:
                logger.warning(f"Failed to scrape {url}: {e}")
                # Still add a result even if scraping failed
                url_hash = hashlib.md5(url.encode()).hexdigest()[:8]
                return ImageMetadata(
                    type="image",
                    id=f"img_{url_hash}",
                    source_url=url,
                    source_domain=get_scraper_for_url(url).source_name,
                    confidence=0.1,
                    scrape_status="failed"
                )
            return None
        
        # Run all scrapes in parallel
        scrape_tasks = [scrape_one(url, priority) for url, priority in prioritized[:scrape_limit]]
        scrape_results = await asyncio.gather(*scrape_tasks, return_exceptions=True)
        
        results = []
        for result in scrape_results:
            if isinstance(result, Exception):
                logger.warning(f"Scrape task failed: {result}")
                continue
            if result is not None:
                results.append(result)
        
        # ============== OPTIMIZATION 4: Sort results - PRIORITIZE PEXELS ==============
        # Sort by: 1) Pexels first, 2) Has creator, 3) Confidence
        def sort_key(x):
            is_pexels = 1 if x.source_domain == "pexels" else 0
            has_creator = 1 if x.creator else 0
            return (is_pexels, has_creator, x.confidence)
        
        results.sort(key=sort_key, reverse=True)
        
        return SearchResponse(
            found=len(results) > 0,
            image_url=image_url or "uploaded_file",
            results=results[:max_results],
            matched_urls=raw_matched_urls,
            search_engines_used=search_results.engines_used,
            total_matches_found=len(raw_matched_urls)
        )
    
    except Exception as e:
        logger.error(f"Search error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ============== BATCH PROCESSING ==============

class BatchSearchRequest(BaseModel):
    image_urls: list[HttpUrl]
    max_results_per_image: int = 5
    timeout_per_image: int = 20


class BatchSearchResponse(BaseModel):
    results: list[SearchResponse]
    total_processed: int
    total_found: int


@app.post("/reverse-search/batch", response_model=BatchSearchResponse)
async def batch_reverse_search(request: BatchSearchRequest):
    if len(request.image_urls) > 50:
        raise HTTPException(status_code=400, detail="Maximum 50 images per batch")
    
    results = []
    semaphore = asyncio.Semaphore(3)
    
    async def process_one(url: str) -> SearchResponse:
        async with semaphore:
            try:
                req = SearchRequest(
                    image_url=url,
                    max_results=request.max_results_per_image,
                    timeout=request.timeout_per_image
                )
                return await reverse_search(req)
            except Exception as e:
                return SearchResponse(
                    found=False,
                    image_url=url,
                    results=[],
                    matched_urls=[],
                    search_engines_used=[],
                    error=str(e)
                )
    
    tasks = [process_one(str(url)) for url in request.image_urls]
    results = await asyncio.gather(*tasks)
    
    found_count = sum(1 for r in results if r.found)
    
    return BatchSearchResponse(
        results=results,
        total_processed=len(results),
        total_found=found_count
    )


# ============== PEXELS DIRECT API ENDPOINTS (High Throughput) ==============

class PexelsDirectRequest(BaseModel):
    """Request for direct Pexels API lookup - uses rotating API keys"""
    image_url: HttpUrl
    
class PexelsDirectResponse(BaseModel):
    """Response from direct Pexels API lookup"""
    found: bool
    photo_id: Optional[str] = None
    metadata: Optional[dict] = None
    api_key_used: int = 0  # Which key in rotation was used (1 or 2)
    error: Optional[str] = None

class PexelsBatchRequest(BaseModel):
    """
    High-throughput batch request for Pexels images.
    Designed for processing 400+ images per hour with rotating API keys.
    """
    image_urls: list[HttpUrl]
    
class PexelsBatchResponse(BaseModel):
    """Response from batch Pexels lookup"""
    results: list[PexelsDirectResponse]
    total_processed: int
    total_found: int
    api_key_stats: dict


@app.get("/api-key-stats")
async def get_api_key_stats():
    """Get current API key rotation statistics"""
    return {
        "pexels": pexels_key_rotator.get_stats(),
        "keys_available": pexels_key_rotator.has_keys()
    }


@app.post("/pexels/lookup", response_model=PexelsDirectResponse)
async def pexels_direct_lookup(request: PexelsDirectRequest):
    """
    Direct Pexels API lookup with round-robin key rotation.
    Much faster than reverse search - use for known Pexels URLs.
    """
    url = str(request.image_url)
    photo_id = pexels_api.extract_pexels_id(url)
    
    if not photo_id:
        return PexelsDirectResponse(
            found=False,
            error="Could not extract Pexels photo ID from URL"
        )
    
    if not pexels_key_rotator.has_keys():
        return PexelsDirectResponse(
            found=False,
            photo_id=photo_id,
            error="No Pexels API keys configured"
        )
    
    # The API call will use the next key in rotation
    stats_before = pexels_key_rotator.get_stats()
    metadata = await pexels_api.search_by_id(photo_id)
    stats_after = pexels_key_rotator.get_stats()
    
    # Calculate which key was used
    key_used = (stats_after["total_requests"] % max(stats_after["total_keys"], 1))
    
    if metadata:
        return PexelsDirectResponse(
            found=True,
            photo_id=photo_id,
            metadata=metadata,
            api_key_used=key_used + 1
        )
    else:
        return PexelsDirectResponse(
            found=False,
            photo_id=photo_id,
            error="Failed to fetch from Pexels API"
        )


@app.post("/pexels/batch", response_model=PexelsBatchResponse)
async def pexels_batch_lookup(request: PexelsBatchRequest):
    """
    High-throughput batch Pexels lookup with alternating API keys.
    
    Designed for batch processing 400 images/hour:
    - Uses round-robin key rotation between primary and backup keys
    - Parallel processing with concurrency control
    - Optimized for Pexels rate limits (200 requests/hour per key)
    
    With 2 keys: 400 requests/hour capacity
    """
    if len(request.image_urls) > 100:
        raise HTTPException(status_code=400, detail="Maximum 100 images per batch for Pexels direct lookup")
    
    # Higher concurrency for direct API calls (they're faster than scraping)
    semaphore = asyncio.Semaphore(5)
    
    async def process_one(url: str) -> PexelsDirectResponse:
        async with semaphore:
            photo_id = pexels_api.extract_pexels_id(url)
            
            if not photo_id:
                return PexelsDirectResponse(
                    found=False,
                    error="Not a Pexels URL"
                )
            
            metadata = await pexels_api.search_by_id(photo_id)
            
            if metadata:
                return PexelsDirectResponse(
                    found=True,
                    photo_id=photo_id,
                    metadata=metadata
                )
            else:
                return PexelsDirectResponse(
                    found=False,
                    photo_id=photo_id,
                    error="API lookup failed"
                )
    
    tasks = [process_one(str(url)) for url in request.image_urls]
    results = await asyncio.gather(*tasks)
    
    found_count = sum(1 for r in results if r.found)
    
    return PexelsBatchResponse(
        results=results,
        total_processed=len(results),
        total_found=found_count,
        api_key_stats=pexels_key_rotator.get_stats()
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
