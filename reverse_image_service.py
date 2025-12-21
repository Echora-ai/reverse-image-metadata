"""Enhanced Reverse Image Attribution Service with Google, File Uploads, and Scale Support"""

import asyncio
import logging
import re
import json
import aiohttp
import hashlib
import base64
import tempfile
import os
from abc import ABC, abstractmethod
from typing import Optional
from urllib.parse import quote_plus, urlencode, urlparse, parse_qs
from dataclasses import dataclass, field
from io import BytesIO

from iptc_extractor import extract_iptc_metadata
from fastapi import FastAPI, HTTPException, UploadFile, File, Form, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, HttpUrl
from bs4 import BeautifulSoup

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Reverse Image Attribution API", version="2.2.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

# ============== MODELS ==============

class SearchRequest(BaseModel):
    image_url: HttpUrl
    max_results: Optional[int] = 10
    timeout: Optional[int] = 30
    engines: Optional[list[str]] = None  # ["google", "yandex", "bing"]

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
    scrape_status: str = "success"  # "success", "partial", "failed"

class SearchResponse(BaseModel):
    found: bool
    image_url: str
    results: list[ImageMetadata]
    matched_urls: list[str] = []  # Raw URLs found by reverse search
    search_engines_used: list[str]
    total_matches_found: int = 0
    error: Optional[str] = None

# ============== SCRAPERS ==============

PRIORITY_DOMAINS = [
    "gettyimages.com", "shutterstock.com", "unsplash.com", "pexels.com", 
    "pixabay.com", "flickr.com", "alamy.com", "istockphoto.com",
    "stock.adobe.com", "500px.com", "depositphotos.com", "dreamstime.com",
    "stocksy.com", "eyeem.com", "twenty20.com", "foap.com"
]

class BaseScraper(ABC):
    source_name: str = "unknown"
    
    def __init__(self):
        self.user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        self.timeout = aiohttp.ClientTimeout(total=15)
    
    def _empty_metadata(self, url: str) -> dict:
        """Return empty metadata structure with all fields."""
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
            "scrape_status": "partial",
        }
    
    def _extract_filename(self, url: str) -> Optional[str]:
        """Extract filename from URL."""
        try:
            path = urlparse(url).path
            filename = path.split("/")[-1]
            if "." in filename and len(filename) < 200:
                # Remove query params from filename
                filename = filename.split("?")[0]
                return filename
        except:
            pass
        return None
    
    async def scrape(self, url: str) -> Optional[dict]:
        """Scrape a URL - ALWAYS returns a result, even if partial."""
        result = self._empty_metadata(url)
        
        try:
            headers = {
                "User-Agent": self.user_agent, 
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Accept-Encoding": "gzip, deflate",
                "Connection": "keep-alive",
            }
            async with aiohttp.ClientSession(timeout=self.timeout) as session:
                async with session.get(url, headers=headers, allow_redirects=True) as response:
                    if response.status != 200:
                        logger.warning(f"HTTP {response.status} for {url}")
                        result["scrape_status"] = "failed"
                        return result  # Still return with URL info
                    html = await response.text()
            
            soup = BeautifulSoup(html, "html.parser")
            extracted = await self._extract_metadata(soup, url)
            
            if extracted:
                # Merge extracted data into result
                for key, value in extracted.items():
                    if value is not None and value != [] and value != "":
                        result[key] = value
                
                # Check how much we got
                important_fields = ["creator", "title", "description", "license"]
                found_count = sum(1 for f in important_fields if result.get(f))
                result["scrape_status"] = "success" if found_count >= 2 else "partial"
            
            return result
            
        except asyncio.TimeoutError:
            logger.warning(f"Timeout scraping {url}")
            result["scrape_status"] = "failed"
            return result
        except Exception as e:
            logger.error(f"Error scraping {url}: {e}")
            result["scrape_status"] = "failed"
            return result
    
    @abstractmethod
    async def _extract_metadata(self, soup: BeautifulSoup, url: str) -> Optional[dict]:
        pass
    
    def _clean_text(self, text: Optional[str]) -> Optional[str]:
        if not text:
            return None
        cleaned = " ".join(text.split())
        for prefix in ["Photo by ", "By ", "Credit: ", "Image by ", "© ", "Photography by ", "Photograph by "]:
            if cleaned.startswith(prefix):
                cleaned = cleaned[len(prefix):]
        # Remove trailing site names
        for suffix in [" - Pexels", " | Pexels", " - Unsplash", " | Unsplash", " - Pixabay", " | Getty Images", " - Shutterstock"]:
            if cleaned.endswith(suffix):
                cleaned = cleaned[:-len(suffix)]
        return cleaned.strip() if cleaned else None
    
    def _extract_keywords(self, soup: BeautifulSoup) -> list[str]:
        """Extract keywords from meta tags."""
        keywords = []
        
        # Try meta keywords
        meta_kw = soup.find("meta", {"name": "keywords"})
        if meta_kw:
            content = meta_kw.get("content", "")
            keywords.extend([k.strip() for k in content.split(",") if k.strip()])
        
        # Try article:tag
        for tag in soup.find_all("meta", {"property": "article:tag"}):
            kw = tag.get("content", "").strip()
            if kw and kw not in keywords:
                keywords.append(kw)
        
        return keywords[:20]
    
    def _extract_date(self, soup: BeautifulSoup) -> Optional[str]:
        """Extract date from various meta tags."""
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
        """Extract description from meta tags."""
        for prop in ["og:description", "description", "twitter:description"]:
            meta = soup.find("meta", {"property": prop}) or soup.find("meta", {"name": prop})
            if meta:
                desc = meta.get("content", "").strip()
                if desc and len(desc) > 10:
                    return desc[:500]
        return None
    
    def _extract_title(self, soup: BeautifulSoup) -> Optional[str]:
        """Extract title from various sources."""
        # Try og:title first
        og = soup.find("meta", {"property": "og:title"})
        if og:
            title = og.get("content", "").strip()
            if title:
                return self._clean_text(title)
        
        # Try regular title
        title_tag = soup.find("title")
        if title_tag:
            return self._clean_text(title_tag.get_text())
        
        # Try h1
        h1 = soup.find("h1")
        if h1:
            return self._clean_text(h1.get_text())
        
        return None
    
    def _build_copyright(self, creator: Optional[str], year: Optional[str] = None) -> Optional[str]:
        """Build copyright string."""
        if not creator:
            return None
        if year:
            return f"© {year} {creator}"
        return f"© {creator}"


class UnsplashScraper(BaseScraper):
    source_name = "unsplash"
    
    async def _extract_metadata(self, soup: BeautifulSoup, url: str) -> Optional[dict]:
        result = {}
        result["license"] = "Unsplash License"
        
        # JSON-LD
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
        
        # Fallback: meta tags
        if not result.get("creator"):
            meta = soup.find("meta", {"name": "twitter:creator"})
            if meta:
                result["creator"] = self._clean_text(meta.get("content", "").replace("@", ""))
        
        if not result.get("title"):
            result["title"] = self._extract_title(soup)
        
        if not result.get("description"):
            result["description"] = self._extract_description(soup)
        
        if not result.get("keywords"):
            result["keywords"] = self._extract_keywords(soup)
        
        year = result.get("date_created", "")[:4] if result.get("date_created") else None
        result["copyright"] = self._build_copyright(result.get("creator"), year)
        
        return result


class PexelsScraper(BaseScraper):
    source_name = "pexels"
    
    async def _extract_metadata(self, soup: BeautifulSoup, url: str) -> Optional[dict]:
        result = {}
        result["license"] = "Pexels License"
        
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
                    
                    keywords = data.get("keywords")
                    if isinstance(keywords, list):
                        result["keywords"] = keywords[:20]
                    elif isinstance(keywords, str):
                        result["keywords"] = [k.strip() for k in keywords.split(",")][:20]
                    break
            except:
                pass
        
        if not result.get("creator"):
            link = soup.find("a", href=re.compile(r"/@[a-zA-Z0-9_-]+"))
            if link:
                result["creator"] = self._clean_text(link.get_text())
                href = link.get("href", "")
                result["creator_url"] = f"https://www.pexels.com{href}" if href.startswith("/") else href
        
        if not result.get("title"):
            result["title"] = self._extract_title(soup)
        
        if not result.get("description"):
            result["description"] = self._extract_description(soup)
        
        if not result.get("keywords"):
            result["keywords"] = self._extract_keywords(soup)
        
        if not result.get("date_created"):
            result["date_created"] = self._extract_date(soup)
        
        year = result.get("date_created", "")[:4] if result.get("date_created") else None
        result["copyright"] = self._build_copyright(result.get("creator"), year)
        
        return result


class PixabayScraper(BaseScraper):
    source_name = "pixabay"
    
    async def _extract_metadata(self, soup: BeautifulSoup, url: str) -> Optional[dict]:
        result = {}
        result["license"] = "Pixabay License"
        
        for link in soup.find_all("a", href=re.compile(r"/users/")):
            text = link.get_text().strip()
            if text and len(text) < 50:
                result["creator"] = self._clean_text(text)
                result["creator_url"] = f"https://pixabay.com{link.get('href', '')}"
                break
        
        result["title"] = self._extract_title(soup)
        result["description"] = self._extract_description(soup)
        result["keywords"] = self._extract_keywords(soup)
        result["date_created"] = self._extract_date(soup)
        
        year = result.get("date_created", "")[:4] if result.get("date_created") else None
        result["copyright"] = self._build_copyright(result.get("creator"), year)
        
        return result


class FlickrScraper(BaseScraper):
    source_name = "flickr"
    
    async def _extract_metadata(self, soup: BeautifulSoup, url: str) -> Optional[dict]:
        result = {}
        
        owner_link = soup.find("a", class_=re.compile(r"owner-name|attribution"))
        if owner_link:
            result["creator"] = self._clean_text(owner_link.get_text())
            href = owner_link.get("href", "")
            if href:
                result["creator_url"] = f"https://www.flickr.com{href}" if href.startswith("/") else href
        
        title_tag = soup.find("h1", class_=re.compile(r"photo-title"))
        if title_tag:
            result["title"] = self._clean_text(title_tag.get_text())
        else:
            result["title"] = self._extract_title(soup)
        
        license_link = soup.find("a", href=re.compile(r"creativecommons.org"))
        if license_link:
            result["license"] = self._clean_text(license_link.get_text()) or "Creative Commons"
        
        result["description"] = self._extract_description(soup)
        result["keywords"] = self._extract_keywords(soup)
        result["date_created"] = self._extract_date(soup)
        
        year = result.get("date_created", "")[:4] if result.get("date_created") else None
        result["copyright"] = self._build_copyright(result.get("creator"), year)
        
        return result


class ShutterstockScraper(BaseScraper):
    source_name = "shutterstock"
    
    async def _extract_metadata(self, soup: BeautifulSoup, url: str) -> Optional[dict]:
        result = {}
        result["license"] = "Shutterstock License (Paid)"
        
        contrib = soup.find("a", href=re.compile(r"/g/[^/]+"))
        if contrib:
            result["creator"] = self._clean_text(contrib.get_text())
            result["creator_url"] = f"https://www.shutterstock.com{contrib.get('href', '')}"
        
        result["title"] = self._extract_title(soup)
        result["description"] = self._extract_description(soup)
        result["keywords"] = self._extract_keywords(soup)
        result["date_created"] = self._extract_date(soup)
        
        year = result.get("date_created", "")[:4] if result.get("date_created") else None
        result["copyright"] = self._build_copyright(result.get("creator"), year)
        
        return result


class GettyImagesScraper(BaseScraper):
    source_name = "gettyimages"
    
    async def _extract_metadata(self, soup: BeautifulSoup, url: str) -> Optional[dict]:
        result = {}
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
        
        if not result.get("title"):
            result["title"] = self._extract_title(soup)
        
        if not result.get("description"):
            result["description"] = self._extract_description(soup)
        
        if not result.get("keywords"):
            result["keywords"] = self._extract_keywords(soup)
        
        year = result.get("date_created", "")[:4] if result.get("date_created") else None
        result["copyright"] = self._build_copyright(result.get("creator"), year)
        
        return result


class GenericScraper(BaseScraper):
    """Aggressive generic scraper - extracts ANYTHING useful from any page."""
    source_name = "generic"
    
    async def _extract_metadata(self, soup: BeautifulSoup, url: str) -> Optional[dict]:
        result = {}
        
        # Try JSON-LD first (most reliable)
        for script in soup.find_all("script", {"type": "application/ld+json"}):
            try:
                data = json.loads(script.string) if script.string else {}
                if isinstance(data, list):
                    for item in data:
                        if item.get("@type") in ["ImageObject", "Photograph", "CreativeWork", "Article", "WebPage"]:
                            data = item
                            break
                if data.get("@type") in ["ImageObject", "Photograph", "CreativeWork", "Article", "WebPage"]:
                    author = data.get("author") or data.get("creator")
                    if isinstance(author, dict):
                        result["creator"] = self._clean_text(author.get("name"))
                        result["creator_url"] = author.get("url")
                    elif isinstance(author, str):
                        result["creator"] = self._clean_text(author)
                    elif isinstance(author, list) and author:
                        first = author[0]
                        if isinstance(first, dict):
                            result["creator"] = self._clean_text(first.get("name"))
                            result["creator_url"] = first.get("url")
                        elif isinstance(first, str):
                            result["creator"] = self._clean_text(first)
                    
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
                    
                    license_info = data.get("license")
                    if license_info:
                        result["license"] = license_info if isinstance(license_info, str) else str(license_info)
                    
                    copyright_info = data.get("copyrightHolder") or data.get("copyrightNotice")
                    if copyright_info:
                        if isinstance(copyright_info, dict):
                            result["copyright"] = copyright_info.get("name")
                        else:
                            result["copyright"] = str(copyright_info)
                    break
            except:
                pass
        
        # Fallback: og:title
        if not result.get("title"):
            result["title"] = self._extract_title(soup)
        
        # Fallback: author meta tags (try many variations)
        if not result.get("creator"):
            author_metas = [
                ("meta", {"name": "author"}),
                ("meta", {"name": "Author"}),
                ("meta", {"name": "dc.creator"}),
                ("meta", {"name": "DC.creator"}),
                ("meta", {"property": "article:author"}),
                ("meta", {"name": "twitter:creator"}),
                ("meta", {"name": "photographer"}),
                ("meta", {"name": "artist"}),
            ]
            for tag, attrs in author_metas:
                elem = soup.find(tag, attrs)
                if elem:
                    creator = self._clean_text(elem.get("content", ""))
                    if creator and len(creator) < 100:
                        result["creator"] = creator
                        break
        
        # Try to find photographer in page text
        if not result.get("creator"):
            patterns = [
                r"[Pp]hoto(?:graph)?(?:y)?\s*(?:by|:)\s*([A-Z][a-zA-Z\s\-\.]+)",
                r"[Cc]redit\s*(?::|to)?\s*([A-Z][a-zA-Z\s\-\.]+)",
                r"[Ii]mage\s*(?:by|:)\s*([A-Z][a-zA-Z\s\-\.]+)",
                r"©\s*\d{4}\s*([A-Z][a-zA-Z\s\-\.]+)",
                r"[Aa]rtist\s*(?::|by)?\s*([A-Z][a-zA-Z\s\-\.]+)",
            ]
            page_text = soup.get_text()[:5000]  # First 5000 chars
            for pattern in patterns:
                match = re.search(pattern, page_text)
                if match:
                    creator = match.group(1).strip()
                    if len(creator) < 50 and len(creator) > 2:
                        result["creator"] = creator
                        break
        
        if not result.get("description"):
            result["description"] = self._extract_description(soup)
        
        if not result.get("keywords"):
            result["keywords"] = self._extract_keywords(soup)
        
        if not result.get("date_created"):
            result["date_created"] = self._extract_date(soup)
        
        # Try to extract copyright from page
        if not result.get("copyright"):
            copyright_patterns = [
                r"(©\s*\d{4}\s*[^<\n]{3,50})",
                r"[Cc]opyright\s*(©?\s*\d{4}[^<\n]{3,50})",
            ]
            page_text = soup.get_text()[:3000]
            for pattern in copyright_patterns:
                match = re.search(pattern, page_text)
                if match:
                    result["copyright"] = match.group(1).strip()[:100]
                    break
        
        if not result.get("copyright"):
            year = result.get("date_created", "")[:4] if result.get("date_created") else None
            result["copyright"] = self._build_copyright(result.get("creator"), year)
        
        return result


def get_scraper_for_url(url: str) -> BaseScraper:
    try:
        domain = urlparse(url).netloc.lower().replace("www.", "")
        if "pexels.com" in domain:
            return PexelsScraper()
        if "pixabay.com" in domain:
            return PixabayScraper()
        if "unsplash.com" in domain:
            return UnsplashScraper()
        if "flickr.com" in domain:
            return FlickrScraper()
        if "shutterstock.com" in domain:
            return ShutterstockScraper()
        if "gettyimages.com" in domain or "gettyimages." in domain:
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
        self.user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        self.timeout = aiohttp.ClientTimeout(total=30)
    
    async def search(self, image_url: str = None, image_bytes: bytes = None, 
                     max_results: int = 10, timeout: int = 30,
                     engines: list[str] = None) -> SearchResult:
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        result = SearchResult()
        
        if engines is None:
            engines = ["yandex", "bing"]  # Google Lens requires JS, skip by default
        
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
        """Google Lens - note: limited without JS execution."""
        urls = []
        matches = []
        
        if not image_url:
            raise Exception("Google: URL required")
        
        encoded = quote_plus(image_url)
        search_url = f"https://lens.google.com/uploadbyurl?url={encoded}"
        
        headers = {
            "User-Agent": self.user_agent,
            "Accept": "text/html,application/xhtml+xml",
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
                elif href.startswith("http") and not href.endswith((".jpg", ".png", ".gif", ".webp")):
                    urls.append(href)
            
        except Exception as e:
            logger.error(f"Google search error: {e}")
            raise Exception(f"Google: {str(e)}")
        
        return ("google", list(dict.fromkeys(urls))[:25], matches)
    
    async def _search_yandex(self, image_url: str = None, image_bytes: bytes = None) -> tuple[str, list[str], list[dict]]:
        """Yandex reverse image search."""
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
                    if not any(href.lower().endswith(ext) for ext in [".jpg", ".jpeg", ".png", ".gif", ".webp"]):
                        urls.append(href)
                        text = link.get_text(strip=True)[:200]
                        if text:
                            matches.append({"url": href, "context": text, "engine": "yandex"})
            
        except Exception as e:
            logger.error(f"Yandex error: {e}")
            raise Exception(f"Yandex: {str(e)}")
        
        return ("yandex", list(dict.fromkeys(urls))[:25], matches)
    
    async def _search_bing(self, image_url: str = None, image_bytes: bytes = None) -> tuple[str, list[str], list[dict]]:
        """Bing Visual Search."""
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
                        if not any(href.lower().endswith(ext) for ext in [".jpg", ".jpeg", ".png", ".gif", ".webp"]):
                            urls.append(href)
                            text = link.get_text(strip=True)[:200]
                            if text:
                                matches.append({"url": href, "context": text, "engine": "bing"})
            
        except Exception as e:
            logger.error(f"Bing error: {e}")
            raise Exception(f"Bing: {str(e)}")
        
        return ("bing", list(dict.fromkeys(urls))[:25], matches)


# ============== ENDPOINTS ==============

@app.get("/")
async def root():
    return {"status": "healthy", "service": "reverse-image-attribution", "version": "2.2.0"}

@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/reverse-search", response_model=SearchResponse)
async def reverse_search(request: SearchRequest):
    """Reverse image search using URL."""
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
    """Reverse image search by uploading a file."""
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
    """Core search logic - IPTC extraction first, then reverse search. ALWAYS returns results."""
    
    # ========== STEP 1: Check embedded IPTC/EXIF metadata FIRST ==========
    logger.info("Checking embedded IPTC/EXIF metadata...")
    iptc_meta = await extract_iptc_metadata(image_url=image_url, image_bytes=image_bytes)
    
    if iptc_meta and iptc_meta.get('creator'):
        logger.info(f"✓ IPTC creator found: {iptc_meta['creator']} - skipping reverse search")
        
        url_hash = hashlib.md5((image_url or "uploaded").encode()).hexdigest()[:8]
        
        result = ImageMetadata(
            type="image",
            id=f"img_{url_hash}_iptc",
            title=iptc_meta.get('title'),
            filename=None,
            creator=iptc_meta.get('creator'),
            creator_url=None,
            date_created=iptc_meta.get('date_created'),
            description=iptc_meta.get('description'),
            keywords=iptc_meta.get('keywords', []),
            location=iptc_meta.get('location'),
            copyright=iptc_meta.get('copyright'),
            license=None,
            source_url=image_url,
            source_domain="iptc_embedded",
            confidence=1.0,
            scrape_status="success"
        )
        
        return SearchResponse(
            found=True,
            image_url=image_url or "uploaded_file",
            results=[result],
            matched_urls=[],
            search_engines_used=["iptc_embedded"],
            total_matches_found=1
        )
    
    logger.info("No embedded creator metadata - proceeding to reverse search...")
    
    # ========== STEP 2: Reverse search ==========
    try:
        search_engine = ReverseImageSearch()
        search_results = await search_engine.search(
            image_url=image_url,
            image_bytes=image_bytes,
            max_results=max_results,
            timeout=timeout,
            engines=engines
        )
        
        # Store matched URLs for response
        matched_urls = search_results.urls.copy()
        
        if not search_results.urls:
            return SearchResponse(
                found=False, 
                image_url=image_url or "uploaded_file",
                results=[], 
                matched_urls=[],
                search_engines_used=search_results.engines_used,
                total_matches_found=0,
                error="; ".join(search_results.errors) if search_results.errors else "No matches found"
            )
        
        # Prioritize known stock photo domains
        prioritized = []
        for url in search_results.urls:
            priority = 0
            for i, domain in enumerate(PRIORITY_DOMAINS):
                if domain in url.lower():
                    priority = len(PRIORITY_DOMAINS) - i
                    break
            prioritized.append((url, priority))
        
        prioritized.sort(key=lambda x: -x[1])
        
        # Scrape ALL results - don't discard any!
        scrape_limit = min(15, len(prioritized))
        results = []
        
        for url, priority in prioritized[:scrape_limit]:
            scraper = get_scraper_for_url(url)
            try:
                metadata = await scraper.scrape(url)
                
                # ALWAYS add the result, even if scraping got minimal data
                if metadata:
                    # Calculate confidence score
                    score = 0.0
                    score += (priority / max(len(PRIORITY_DOMAINS), 1)) * 0.3
                    score += 0.25 if metadata.get("creator") else 0
                    score += 0.15 if metadata.get("license") else 0
                    score += 0.1 if metadata.get("title") else 0
                    score += 0.1 if metadata.get("description") else 0
                    score += 0.05 if metadata.get("date_created") else 0
                    score += 0.05 if metadata.get("keywords") else 0
                    
                    # Minimum score if we at least have the URL
                    if score < 0.1:
                        score = 0.1
                    
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
                        source_url=url,
                        source_domain=scraper.source_name,
                        confidence=min(score, 1.0),
                        scrape_status=metadata.get("scrape_status", "partial")
                    ))
                    
            except Exception as e:
                logger.warning(f"Failed to scrape {url}: {e}")
                # Still add a minimal result for this URL
                url_hash = hashlib.md5(url.encode()).hexdigest()[:8]
                domain = urlparse(url).netloc.replace("www.", "")
                results.append(ImageMetadata(
                    type="image",
                    id=f"img_{url_hash}",
                    title=None,
                    filename=None,
                    creator=None,
                    creator_url=None,
                    date_created=None,
                    description=None,
                    keywords=[],
                    location=None,
                    copyright=None,
                    license=None,
                    source_url=url,
                    source_domain=domain,
                    confidence=0.05,
                    scrape_status="failed"
                ))
            
            # Small delay to be polite
            await asyncio.sleep(0.2)
        
        # Sort by confidence
        results.sort(key=lambda x: x.confidence, reverse=True)
        
        return SearchResponse(
            found=len(matched_urls) > 0,  # Found = we found matching URLs
            image_url=image_url or "uploaded_file",
            results=results[:max_results],
            matched_urls=matched_urls[:20],  # Include raw URLs
            search_engines_used=search_results.engines_used,
            total_matches_found=len(matched_urls)
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
    """Process multiple images in batch."""
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


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
