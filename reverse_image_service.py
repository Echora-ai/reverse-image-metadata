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

app = FastAPI(title="Reverse Image Attribution API", version="5.1.0")

# ============== PEXELS API KEY MANAGEMENT ==============
# Load API keys from environment
PEXELS_API_KEY_PRIMARY = os.getenv("PEXELS_API_KEY", "")
PEXELS_API_KEY_BACKUP = os.getenv("PEXELS_API_KEY_BACKUP", "")

# Build list of available keys (filter out empty ones)
PEXELS_API_KEYS = [k for k in [PEXELS_API_KEY_PRIMARY, PEXELS_API_KEY_BACKUP] if k]

class ApiKeyManager:
    """
    API key manager that allows explicit key selection.
    Xano controls which key to use via api_key_index parameter.
    
    - api_key_index=0: Use primary key (PEXELS_API_KEY)
    - api_key_index=1: Use backup key (PEXELS_API_KEY_BACKUP)
    """
    
    def __init__(self, keys: list[str]):
        self._keys = keys if keys else []
        self._lock = Lock()
        self._request_counts = {i: 0 for i in range(len(keys))} if keys else {}
    
    def get_key_by_index(self, index: int) -> tuple[str, int]:
        """
        Get API key by explicit index (0 or 1).
        Returns (key, actual_index_used).
        If index is out of range, defaults to 0.
        """
        if not self._keys:
            return ("", -1)
        
        with self._lock:
            # Clamp index to valid range
            actual_index = index if 0 <= index < len(self._keys) else 0
            self._request_counts[actual_index] = self._request_counts.get(actual_index, 0) + 1
            return (self._keys[actual_index], actual_index)
    
    def get_stats(self) -> dict:
        """Get usage stats for each key"""
        with self._lock:
            total = sum(self._request_counts.values())
            return {
                "total_keys": len(self._keys),
                "total_requests": total,
                "requests_per_key": dict(self._request_counts)
            }
    
    def has_keys(self) -> bool:
        return len(self._keys) > 0
    
    def key_count(self) -> int:
        return len(self._keys)

# Global key manager instance
pexels_key_manager = ApiKeyManager(PEXELS_API_KEYS)

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
    api_key_index: Optional[int] = None  # 0=primary, 1=backup - Xano controls this

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
    api_key_used: Optional[int] = None  # Which key was used (0 or 1)
    error: Optional[str] = None


# ============== RESULT SORTING HELPERS ==============

def count_non_null_fields(result: ImageMetadata) -> int:
    """
    Count the number of non-null/non-empty important fields in a result.
    Higher count = more complete metadata = better result.
    """
    count = 0
    
    # Check string fields
    if result.creator and result.creator.strip():
        count += 3  # Creator is extra important, weight it more
    if result.title and result.title.strip():
        count += 2
    if result.description and result.description.strip():
        count += 1
    if result.creator_url and result.creator_url.strip():
        count += 1
    if result.date_created and result.date_created.strip():
        count += 1
    if result.location and result.location.strip():
        count += 1
    if result.copyright and result.copyright.strip():
        count += 1
    if result.license and result.license.strip():
        count += 1
    if result.source_url and result.source_url.strip():
        count += 1
    
    # Check list fields
    if result.keywords and len(result.keywords) > 0:
        count += 1
    
    return count


def sort_results_by_quality(results: list[ImageMetadata]) -> list[ImageMetadata]:
    """
    Sort results with the following priority:
    1. Results with creator (not null/empty) come first
    2. Among those, sort by count of non-null fields (most complete first)
    3. Finally, sort by confidence score
    
    This ensures the best, most complete results with creator info are at the top.
    """
    def sort_key(result: ImageMetadata) -> tuple:
        has_creator = bool(result.creator and result.creator.strip())
        non_null_count = count_non_null_fields(result)
        confidence = result.confidence
        
        # Return tuple: (has_creator DESC, non_null_count DESC, confidence DESC)
        # We negate values because Python sorts ascending by default
        return (
            -int(has_creator),      # True (1) comes before False (0) when negated
            -non_null_count,        # Higher counts first
            -confidence             # Higher confidence first
        )
    
    return sorted(results, key=sort_key)


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

PRIORITY_DOMAINS = [
    "gettyimages.com", "shutterstock.com", "unsplash.com", "pexels.com", 
    "pixabay.com", "flickr.com", "alamy.com", "istockphoto.com",
    "stock.adobe.com", "500px.com", "depositphotos.com"
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
    
    async def scrape(self, url: str, api_key_index: Optional[int] = None) -> Optional[dict]:
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
    
    async def scrape(self, url: str, api_key_index: Optional[int] = None) -> Optional[dict]:
        """Override scrape to try Pexels API first with explicit key selection"""
        # Try to extract Pexels photo ID
        photo_id = pexels_api.extract_pexels_id(url)
        
        if photo_id and pexels_key_manager.has_keys():
            logger.info(f"Attempting Pexels API lookup for photo ID: {photo_id} with key_index={api_key_index}")
            api_result = await pexels_api.search_by_id(photo_id, key_index=api_key_index)
            
            if api_result and api_result.get("creator"):
                logger.info(f"Got Pexels metadata via API for {photo_id}: {api_result.get('creator')}")
                return api_result
            else:
                logger.info(f"Pexels API lookup failed for {photo_id}, falling back to scraping")
        
        # Fall back to regular scraping if API didn't work
        return await super().scrape(url, api_key_index)
    
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
            
            if not result["creator"]:
                patterns = [
                    r"^/@([a-zA-Z0-9_-]+)$",
                    r"^/@([a-zA-Z0-9_-]+)/$",
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
        
        if not result["creator"]:
            for elem in soup.find_all(["span", "div", "a", "p"]):
                text = elem.get_text().strip()
                match = re.search(r"(?:Photo|Image|Taken)\s+by\s+([A-Za-z][A-Za-z0-9\s_-]{1,50})", text, re.IGNORECASE)
                if match:
                    result["creator"] = self._clean_text(match.group(1))
                    break
        
        if not result["title"]:
            og = soup.find("meta", {"property": "og:title"})
            if og:
                title = og.get("content", "")
                title = re.sub(r"\s*[·|]\s*Free.*$", "", title, flags=re.IGNORECASE)
                title = re.sub(r"\s*[·|]\s*Pexels.*$", "", title, flags=re.IGNORECASE)
                result["title"] = self._clean_text(title)
        
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
            engines = ["yandex", "bing"]
        
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
    Direct Pexels API search with explicit key selection.
    Xano controls which key to use via api_key_index parameter.
    """
    
    def __init__(self):
        self.base_url = "https://api.pexels.com/v1"
        self.timeout = aiohttp.ClientTimeout(total=15)
    
    async def search_by_id(self, photo_id: str, key_index: Optional[int] = None) -> Optional[dict]:
        """
        Get photo details directly from Pexels API by photo ID.
        
        Args:
            photo_id: The Pexels photo ID
            key_index: Which API key to use (0=primary, 1=backup). 
                      If None, defaults to 0 (primary).
        """
        if not pexels_key_manager.has_keys():
            logger.warning("No Pexels API keys available")
            return None
        
        # Use specified key or default to 0
        actual_key_index = key_index if key_index is not None else 0
        api_key, used_index = pexels_key_manager.get_key_by_index(actual_key_index)
        
        logger.info(f"Using Pexels API key #{used_index} (requested: {key_index}) for photo {photo_id}")
        
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
                        result = self._parse_photo_response(data)
                        result["api_key_used"] = used_index
                        return result
                    elif resp.status == 429:
                        logger.warning(f"Rate limited on Pexels API key #{used_index}")
                        return None
                    else:
                        logger.warning(f"Pexels API returned {resp.status} for photo {photo_id}")
                        return None
        except Exception as e:
            logger.error(f"Pexels API error for photo {photo_id}: {e}")
            return None
    
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
            "confidence": 0.95,
            "scrape_status": "success",
            "avg_color": data.get("avg_color"),
            "width": data.get("width"),
            "height": data.get("height"),
            "src": data.get("src", {})
        }
    
    def extract_pexels_id(self, url: str) -> Optional[str]:
        """Extract Pexels photo ID from various URL formats"""
        patterns = [
            r"pexels\.com/photo/[^/]+\-(\d+)",
            r"pexels\.com/photo/(\d+)",
            r"images\.pexels\.com/photos/(\d+)/",
            r"pexels-photo-(\d+)",
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
    pexels_keys_labels = []
    if PEXELS_API_KEY_PRIMARY:
        pexels_keys_labels.append("primary (index 0)")
    if PEXELS_API_KEY_BACKUP:
        pexels_keys_labels.append("backup (index 1)")
    
    return {
        "status": "healthy", 
        "service": "reverse-image-attribution", 
        "version": "5.1.0",
        "cloudscraper_available": HAS_CLOUDSCRAPER,
        "pexels_api_keys": pexels_keys_labels,
        "pexels_key_selection": "explicit via api_key_index parameter (0 or 1)",
        "env_vars_needed": ["PEXELS_API_KEY", "PEXELS_API_KEY_BACKUP"],
        "usage": "Pass api_key_index=0 for primary key, api_key_index=1 for backup key",
        "result_sorting": "Prioritizes results with creator, then by most non-null fields, then by confidence"
    }

@app.get("/health")
async def health():
    return {
        "status": "ok", 
        "cloudscraper": HAS_CLOUDSCRAPER,
        "pexels_keys_available": pexels_key_manager.has_keys(),
        "pexels_key_count": pexels_key_manager.key_count(),
        "pexels_key_stats": pexels_key_manager.get_stats()
    }


@app.get("/api-key-stats")
async def get_api_key_stats():
    """Get current API key usage statistics"""
    return {
        "pexels": pexels_key_manager.get_stats(),
        "keys_available": pexels_key_manager.has_keys(),
        "key_count": pexels_key_manager.key_count()
    }


@app.post("/reverse-search", response_model=SearchResponse)
async def reverse_search(request: SearchRequest):
    """
    Reverse image search with optional explicit API key selection.
    
    Pass api_key_index=0 for primary Pexels key, api_key_index=1 for backup.
    If not specified, defaults to primary key (0).
    
    Results are sorted by:
    1. Has creator (non-null) - results with creator come first
    2. Count of non-null fields - more complete results come first
    3. Confidence score - higher confidence comes first
    """
    image_url = str(request.image_url)
    logger.info(f"Reverse search for URL: {image_url}, api_key_index={request.api_key_index}")
    
    return await _perform_search(
        image_url=image_url,
        max_results=request.max_results,
        timeout=request.timeout,
        engines=request.engines,
        api_key_index=request.api_key_index
    )


async def _perform_search(
    image_url: str = None,
    image_bytes: bytes = None,
    max_results: int = 10,
    timeout: int = 30,
    engines: list[str] = None,
    api_key_index: Optional[int] = None
) -> SearchResponse:
    
    key_used = None
    
    try:
        search_engine = ReverseImageSearch()
        search_results = await search_engine.search(
            image_url=image_url,
            image_bytes=image_bytes,
            max_results=max_results,
            timeout=timeout,
            engines=engines
        )
        
        raw_matched_urls = list(search_results.urls)
        
        if not search_results.urls:
            return SearchResponse(
                found=False, 
                image_url=image_url or "uploaded_file",
                results=[], 
                matched_urls=raw_matched_urls,
                search_engines_used=search_results.engines_used,
                api_key_used=key_used,
                error="; ".join(search_results.errors) if search_results.errors else None
            )
        
        unique_urls = deduplicate_urls(search_results.urls)
        
        prioritized = []
        for url in unique_urls:
            priority = 0
            for i, domain in enumerate(PRIORITY_DOMAINS):
                if domain in url.lower():
                    priority = len(PRIORITY_DOMAINS) - i
                    break
            prioritized.append((url, priority))
        
        prioritized.sort(key=lambda x: -x[1])
        
        scrape_limit = min(8, len(prioritized))
        results = []
        
        for url, priority in prioritized[:scrape_limit]:
            scraper = get_scraper_for_url(url)
            try:
                metadata = await scraper.scrape(url, api_key_index=api_key_index)
                
                if metadata:
                    # Track which key was used if available
                    if metadata.get("api_key_used") is not None:
                        key_used = metadata.get("api_key_used")
                    
                    score = 0.0
                    score += (priority / len(PRIORITY_DOMAINS)) * 0.3
                    score += 0.3 if metadata.get("creator") else 0
                    score += 0.15 if metadata.get("license") else 0
                    score += 0.1 if metadata.get("title") else 0
                    score += 0.05 if metadata.get("date_created") else 0
                    score += 0.05 if metadata.get("keywords") else 0
                    score += 0.05 if metadata.get("location") else 0
                    
                    score = max(score, 0.1)
                    
                    url_hash = hashlib.md5(url.encode()).hexdigest()[:8]
                    
                    results.append(ImageMetadata(
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
                    ))
            except Exception as e:
                logger.warning(f"Failed to scrape {url}: {e}")
                url_hash = hashlib.md5(url.encode()).hexdigest()[:8]
                results.append(ImageMetadata(
                    type="image",
                    id=f"img_{url_hash}",
                    source_url=url,
                    source_domain=get_scraper_for_url(url).source_name,
                    confidence=0.1,
                    scrape_status="failed"
                ))
            
            await asyncio.sleep(0.2)
        
        # Sort results by quality: creator first, then non-null count, then confidence
        sorted_results = sort_results_by_quality(results)
        
        return SearchResponse(
            found=len(sorted_results) > 0,
            image_url=image_url or "uploaded_file",
            results=sorted_results[:max_results],
            matched_urls=raw_matched_urls,
            search_engines_used=search_results.engines_used,
            total_matches_found=len(raw_matched_urls),
            api_key_used=key_used
        )
    
    except Exception as e:
        logger.error(f"Search error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
