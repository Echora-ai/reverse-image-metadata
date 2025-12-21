"""Reverse Image Attribution Service with Playwright v4.7.0"""

import asyncio
import logging
import re
import json
import os
import aiohttp
import hashlib
from typing import Optional, List, Tuple
from urllib.parse import quote_plus, urlparse, unquote
from dataclasses import dataclass, field

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, HttpUrl
from bs4 import BeautifulSoup

# Playwright for Cloudflare bypass
from playwright.async_api import async_playwright, Browser

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Reverse Image Attribution API", version="4.7.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

# ============== API KEYS ==============
# Primary and backup Pexels API keys
# Set these in Cloud Run environment variables:
#   PEXELS_API_KEY - Primary key (used first)
#   PEXELS_API_KEY_BACKUP - Backup key (used if primary fails/rate limited)
PEXELS_API_KEY = os.environ.get("PEXELS_API_KEY", "")
PEXELS_API_KEY_BACKUP = os.environ.get("PEXELS_API_KEY_BACKUP", "")
UNSPLASH_ACCESS_KEY = os.environ.get("UNSPLASH_ACCESS_KEY", "")

# Global browser instance
_browser: Optional[Browser] = None
_playwright = None


async def get_browser() -> Browser:
    """Get or create browser instance."""
    global _browser, _playwright
    if _browser is None:
        _playwright = await async_playwright().start()
        _browser = await _playwright.chromium.launch(
            headless=True,
            args=[
                '--no-sandbox',
                '--disable-setuid-sandbox',
                '--disable-dev-shm-usage',
                '--disable-gpu',
                '--disable-blink-features=AutomationControlled',
            ]
        )
        logger.info("Browser launched successfully")
    return _browser


@app.on_event("shutdown")
async def shutdown():
    global _browser, _playwright
    if _browser:
        await _browser.close()
    if _playwright:
        await _playwright.stop()


# ============== MODELS ==============

class SearchRequest(BaseModel):
    image_url: HttpUrl
    max_results: Optional[int] = 10
    timeout: Optional[int] = 30
    engines: Optional[List[str]] = None


class DebugRequest(BaseModel):
    url: str
    timeout: Optional[int] = 30


class ImageMetadata(BaseModel):
    type: str = "image"
    id: Optional[str] = None
    title: Optional[str] = None
    filename: Optional[str] = None
    creator: Optional[str] = None
    creator_url: Optional[str] = None
    date_created: Optional[str] = None
    description: Optional[str] = None
    keywords: List[str] = []
    location: Optional[str] = None
    copyright: Optional[str] = None
    license: Optional[str] = None
    source_url: Optional[str] = None
    source_domain: Optional[str] = None
    confidence: float = 0.0
    scrape_status: str = "pending"


class SearchResponse(BaseModel):
    found: bool
    image_url: str
    results: List[ImageMetadata]
    matched_urls: List[str] = []
    search_engines_used: List[str]
    total_matches_found: int = 0
    error: Optional[str] = None


# ============== PEXELS API ==============

async def call_pexels_api(photo_id: str, api_key: str, key_name: str = "primary") -> Tuple[Optional[dict], bool]:
    """
    Call Pexels API with a specific key.
    
    Returns: (result_dict, should_try_backup)
    - result_dict: The metadata if successful, None otherwise
    - should_try_backup: True if we should try the backup key (rate limit/error)
    """
    api_url = f"https://api.pexels.com/v1/photos/{photo_id}"
    headers = {
        "Authorization": api_key,
        "User-Agent": "Echora Image Attribution Service"
    }
    
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
            async with session.get(api_url, headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    logger.info(f"Pexels API success ({key_name} key) for photo {photo_id}")
                    
                    result = {
                        "title": data.get("alt") or f"Photo by {data.get('photographer', 'Unknown')}",
                        "creator": data.get("photographer"),
                        "creator_url": data.get("photographer_url"),
                        "description": data.get("alt"),
                        "keywords": [],
                        "location": None,
                        "license": "Pexels License",
                        "date_created": None,
                        "copyright": f"© {data.get('photographer')}" if data.get('photographer') else None,
                        "source_url": data.get("url"),
                        "scrape_status": "success"
                    }
                    
                    logger.info(f"Pexels API returned: creator={result['creator']}, title={result['title']}")
                    return result, False
                    
                elif resp.status == 429:
                    # Rate limited - try backup
                    logger.warning(f"Pexels API rate limited ({key_name} key)")
                    return None, True
                    
                elif resp.status == 404:
                    logger.warning(f"Pexels photo {photo_id} not found")
                    return None, False  # Don't retry - photo doesn't exist
                    
                elif resp.status == 401:
                    logger.error(f"Pexels API unauthorized ({key_name} key) - invalid API key")
                    return None, True  # Try backup in case primary is bad
                    
                else:
                    logger.warning(f"Pexels API returned status {resp.status} ({key_name} key)")
                    return None, True
                    
    except asyncio.TimeoutError:
        logger.error(f"Pexels API timeout ({key_name} key)")
        return None, True
    except Exception as e:
        logger.error(f"Pexels API error ({key_name} key): {e}")
        return None, True


async def fetch_pexels_via_api(photo_id: str) -> Optional[dict]:
    """
    Fetch photo metadata from Pexels API.
    Tries primary key first, falls back to backup if needed.
    """
    # Check if we have any API keys
    if not PEXELS_API_KEY and not PEXELS_API_KEY_BACKUP:
        logger.info("No Pexels API keys configured (set PEXELS_API_KEY and/or PEXELS_API_KEY_BACKUP)")
        return None
    
    # Try primary key first
    if PEXELS_API_KEY:
        result, should_try_backup = await call_pexels_api(photo_id, PEXELS_API_KEY, "primary")
        if result:
            return result
        if not should_try_backup:
            return None  # Photo doesn't exist, no point trying backup
    else:
        should_try_backup = True
    
    # Try backup key if needed
    if should_try_backup and PEXELS_API_KEY_BACKUP:
        logger.info("Trying backup Pexels API key...")
        result, _ = await call_pexels_api(photo_id, PEXELS_API_KEY_BACKUP, "backup")
        if result:
            return result
    
    return None


def extract_pexels_info_from_url(url: str) -> Tuple[Optional[str], Optional[str]]:
    """Extract photo ID and title from Pexels URL patterns.
    
    Returns: (photo_id, title)
    """
    photo_id = None
    title = None
    
    # Extract photo ID
    id_patterns = [
        r'/photos?/(\d+)',           # /photos/15647646 or /photo/15647646
        r'-(\d+)/?$',                # slug-15647646/
        r'pexels-photo-(\d+)',       # pexels-photo-15647646.jpeg
        r'/(\d+)(?:\?|$|/)',         # /15647646/ or /15647646?
    ]
    for pattern in id_patterns:
        match = re.search(pattern, url)
        if match:
            photo_id = match.group(1)
            break
    
    # Extract title from "free-photo-of-{slug}" pattern
    slug_match = re.search(r'free-photo-of-([a-z0-9-]+)\.', url, re.IGNORECASE)
    if slug_match:
        slug = slug_match.group(1)
        # Convert slug to title: "a-man-in-tank-top" -> "A Man In Tank Top"
        title = slug.replace('-', ' ').title()
        logger.info(f"Extracted title from URL slug: {title}")
    
    # Also check URL path for title slug at the end
    if not title:
        # Pattern: /photo/a-man-in-a-tank-top-and-pants-standing-outside-15647646/
        path_match = re.search(r'/photo/([a-z0-9-]+)-\d+/?$', url, re.IGNORECASE)
        if path_match:
            slug = path_match.group(1)
            title = slug.replace('-', ' ').title()
            logger.info(f"Extracted title from page URL: {title}")
    
    return photo_id, title


def extract_pexels_metadata_from_urls(urls: List[str]) -> dict:
    """Extract whatever metadata we can from Pexels CDN URLs without scraping.
    
    This is a fallback when Cloudflare blocks us and no API key is available.
    """
    result = {
        "title": None,
        "creator": None,
        "creator_url": None,
        "photo_id": None,
        "source_url": None,
    }
    
    for url in urls:
        if "pexels.com" not in url.lower():
            continue
            
        photo_id, title = extract_pexels_info_from_url(url)
        
        if photo_id and not result["photo_id"]:
            result["photo_id"] = photo_id
            result["source_url"] = f"https://www.pexels.com/photo/{photo_id}/"
        
        if title and not result["title"]:
            result["title"] = title
        
        # If we found both, we're done
        if result["photo_id"] and result["title"]:
            break
    
    # Build better source URL if we have title
    if result["photo_id"] and result["title"]:
        slug = result["title"].lower().replace(' ', '-')
        result["source_url"] = f"https://www.pexels.com/photo/{slug}-{result['photo_id']}/"
    
    return result


# ============== URL TRANSFORMATION ==============

def transform_url_to_page(url: str) -> str:
    """Transform CDN image URLs to their corresponding page URLs."""
    try:
        parsed = urlparse(url)
        domain = parsed.netloc.lower()
        path = parsed.path
        
        # Pexels
        if "pexels.com" in domain:
            photo_id, title = extract_pexels_info_from_url(url)
            if photo_id:
                if title:
                    slug = title.lower().replace(' ', '-')
                    return f"https://www.pexels.com/photo/{slug}-{photo_id}/"
                return f"https://www.pexels.com/photo/{photo_id}/"
        
        # Unsplash
        if "unsplash.com" in domain:
            match = re.search(r'photo-([a-zA-Z0-9_-]+)', path)
            if match:
                return f"https://unsplash.com/photos/{match.group(1)}"
        
        # Pixabay
        if "pixabay.com" in domain and "/photo/" in path:
            match = re.search(r'-(\d+)\.[a-z]+$', path, re.IGNORECASE)
            if match:
                return f"https://pixabay.com/photos/id-{match.group(1)}/"
    except Exception as e:
        logger.warning(f"URL transform failed: {e}")
    
    return url


PRIORITY_DOMAINS = [
    "pexels.com", "unsplash.com", "pixabay.com", "flickr.com",
    "gettyimages.com", "shutterstock.com", "alamy.com", "istockphoto.com",
    "stock.adobe.com", "500px.com", "depositphotos.com"
]


# ============== SCRAPER ==============

async def scrape_with_playwright(url: str, all_matched_urls: List[str] = None, timeout: int = 25) -> dict:
    """Scrape a page using Playwright. Falls back to URL parsing if blocked."""
    page_url = transform_url_to_page(url)
    domain = urlparse(url).netloc.lower()
    
    # Determine license based on domain
    license_info = None
    if "pexels.com" in domain:
        license_info = "Pexels License"
    elif "unsplash.com" in domain:
        license_info = "Unsplash License"
    elif "pixabay.com" in domain:
        license_info = "Pixabay License"
    elif "shutterstock.com" in domain:
        license_info = "Shutterstock License (Paid)"
    elif "gettyimages.com" in domain:
        license_info = "Getty Images License (Paid)"
    
    # ===== TRY PEXELS API FIRST =====
    if "pexels.com" in url.lower():
        photo_id, url_title = extract_pexels_info_from_url(url)
        if photo_id:
            # Try API first (with automatic fallback to backup key)
            api_result = await fetch_pexels_via_api(photo_id)
            if api_result:
                return api_result
            
            # No API key or API failed - extract what we can from URLs
            logger.info("Pexels API unavailable, extracting metadata from URLs")
            
            # Use all matched URLs to find metadata
            urls_to_check = [url] + (all_matched_urls or [])
            url_metadata = extract_pexels_metadata_from_urls(urls_to_check)
            
            result = {
                "title": url_metadata.get("title"),
                "creator": None,  # Can't get without API
                "creator_url": None,
                "description": url_metadata.get("title"),
                "keywords": [],
                "location": None,
                "license": "Pexels License",
                "date_created": None,
                "copyright": None,
                "source_url": url_metadata.get("source_url") or page_url,
                "scrape_status": "partial" if url_metadata.get("title") else "failed"
            }
            
            # Add a note about needing API key
            if result["title"]:
                logger.info(f"Extracted from URL - title: {result['title']} (photographer requires PEXELS_API_KEY)")
            
            return result
    
    # For non-Pexels or if Pexels extraction failed, try regular scraping
    result = {
        "title": None,
        "creator": None,
        "creator_url": None,
        "description": None,
        "keywords": [],
        "location": None,
        "license": license_info,
        "date_created": None,
        "copyright": None,
        "source_url": page_url,
        "scrape_status": "pending"
    }
    
    context = None
    try:
        browser = await get_browser()
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1920, "height": 1080},
        )
        page = await context.new_page()
        
        response = await page.goto(page_url, wait_until="networkidle", timeout=timeout * 1000)
        
        result["source_url"] = page.url
        
        if response and response.status >= 400:
            result["scrape_status"] = "failed"
            await context.close()
            return result
        
        await asyncio.sleep(2)
        html = await page.content()
        await context.close()
        context = None
        
        if not html or len(html) < 500:
            result["scrape_status"] = "failed"
            return result
        
        # Check for Cloudflare
        if "Just a moment" in html or "Checking your browser" in html:
            logger.warning("Cloudflare challenge detected")
            result["scrape_status"] = "failed"
            return result
        
        soup = BeautifulSoup(html, "html.parser")
        
        # JSON-LD extraction
        for script in soup.find_all("script", {"type": "application/ld+json"}):
            try:
                data = json.loads(script.string) if script.string else {}
                if isinstance(data, list):
                    data = data[0] if data else {}
                
                if data.get("@type") in ["ImageObject", "Photograph", "CreativeWork"]:
                    author = data.get("author") or data.get("creator")
                    if isinstance(author, dict):
                        result["creator"] = author.get("name")
                        result["creator_url"] = author.get("url")
                    elif isinstance(author, str):
                        result["creator"] = author
                    
                    if not result["title"]:
                        result["title"] = data.get("name") or data.get("headline")
                    if not result["description"]:
                        result["description"] = data.get("description")
                    break
            except:
                pass
        
        # Meta tags fallback
        if not result["title"]:
            og = soup.find("meta", {"property": "og:title"})
            if og:
                result["title"] = og.get("content", "").strip()
        
        if not result["creator"]:
            author = soup.find("meta", {"name": "author"})
            if author:
                result["creator"] = author.get("content", "").strip()
        
        # Status
        if result["creator"] and result["title"]:
            result["scrape_status"] = "success"
        elif result["creator"] or result["title"]:
            result["scrape_status"] = "partial"
        else:
            result["scrape_status"] = "failed"
        
        if result["creator"]:
            result["copyright"] = f"© {result['creator']}"
        
        return result
        
    except Exception as e:
        logger.error(f"Scrape error: {e}")
        result["scrape_status"] = "failed"
        if context:
            try:
                await context.close()
            except:
                pass
        return result


def get_source_domain(url: str) -> str:
    domain = urlparse(url).netloc.lower().replace("www.", "")
    for d in ["pexels", "unsplash", "pixabay", "flickr", "shutterstock", "gettyimages"]:
        if d in domain:
            return d
    return "generic"


# ============== SEARCH ==============

BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

@dataclass
class SearchResult:
    urls: List[str] = field(default_factory=list)
    engines_used: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)


async def search_yandex(image_url: str, timeout: int = 30) -> tuple:
    urls = []
    try:
        encoded = quote_plus(image_url)
        search_url = f"https://yandex.com/images/search?rpt=imageview&url={encoded}"
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout), headers=BROWSER_HEADERS) as session:
            async with session.get(search_url) as resp:
                if resp.status != 200:
                    return ("yandex", [], f"HTTP {resp.status}")
                html = await resp.text()
        soup = BeautifulSoup(html, "html.parser")
        for link in soup.find_all("a", href=True):
            href = link["href"]
            if href.startswith("http") and "yandex" not in href.lower():
                urls.append(href)
        return ("yandex", list(dict.fromkeys(urls))[:25], None)
    except Exception as e:
        return ("yandex", [], str(e))


async def search_bing(image_url: str, timeout: int = 30) -> tuple:
    urls = []
    try:
        encoded = quote_plus(image_url)
        search_url = f"https://www.bing.com/images/search?view=detailv2&iss=sbi&q=imgurl:{encoded}"
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout), headers=BROWSER_HEADERS) as session:
            async with session.get(search_url, allow_redirects=True) as resp:
                if resp.status != 200:
                    return ("bing", [], f"HTTP {resp.status}")
                html = await resp.text()
        soup = BeautifulSoup(html, "html.parser")
        for link in soup.find_all("a", href=True):
            href = link["href"]
            if href.startswith("http") and not any(x in href.lower() for x in ["bing.com", "microsoft.com"]):
                urls.append(href)
        return ("bing", list(dict.fromkeys(urls))[:25], None)
    except Exception as e:
        return ("bing", [], str(e))


async def perform_search(image_url: str, timeout: int = 30) -> SearchResult:
    result = SearchResult()
    tasks = [search_yandex(image_url, timeout), search_bing(image_url, timeout)]
    search_results = await asyncio.gather(*tasks, return_exceptions=True)
    seen = set()
    for sr in search_results:
        if isinstance(sr, Exception):
            result.errors.append(str(sr))
            continue
        engine, urls, error = sr
        result.engines_used.append(engine)
        if error:
            result.errors.append(f"{engine}: {error}")
        for url in urls:
            if url not in seen:
                seen.add(url)
                result.urls.append(url)
    return result


def deduplicate_urls(urls: List[str]) -> List[str]:
    seen_ids = set()
    unique = []
    for url in urls:
        photo_id = None
        if "pexels.com" in url:
            match = re.search(r'/photos?/(\d+)', url)
            if not match:
                match = re.search(r'pexels-photo-(\d+)', url)
            if match:
                photo_id = f"pexels_{match.group(1)}"
        elif "unsplash.com" in url:
            match = re.search(r'photo[s-]?([a-zA-Z0-9_-]+)', url)
            if match:
                photo_id = f"unsplash_{match.group(1)}"
        if photo_id:
            if photo_id in seen_ids:
                continue
            seen_ids.add(photo_id)
        unique.append(url)
    return unique


# ============== ENDPOINTS ==============

@app.get("/")
async def root():
    pexels_status = []
    if PEXELS_API_KEY:
        pexels_status.append("primary")
    if PEXELS_API_KEY_BACKUP:
        pexels_status.append("backup")
    
    return {
        "status": "healthy", 
        "service": "reverse-image-attribution", 
        "version": "4.7.0", 
        "pexels_api_keys": pexels_status if pexels_status else ["none configured"],
        "env_vars_needed": ["PEXELS_API_KEY", "PEXELS_API_KEY_BACKUP"]
    }

@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/debug-scrape")
async def debug_scrape(request: DebugRequest):
    """Debug endpoint."""
    url = request.url
    
    # For Pexels, show what we can extract without scraping
    if "pexels.com" in url.lower():
        photo_id, title = extract_pexels_info_from_url(url)
        return {
            "url": url,
            "extracted_photo_id": photo_id,
            "extracted_title": title,
            "pexels_api_primary": bool(PEXELS_API_KEY),
            "pexels_api_backup": bool(PEXELS_API_KEY_BACKUP),
            "env_vars": {
                "PEXELS_API_KEY": "set" if PEXELS_API_KEY else "NOT SET",
                "PEXELS_API_KEY_BACKUP": "set" if PEXELS_API_KEY_BACKUP else "NOT SET"
            }
        }
    
    return {"url": url, "note": "Use /reverse-search for full results"}


@app.post("/reverse-search", response_model=SearchResponse)
async def reverse_search(request: SearchRequest):
    image_url = str(request.image_url)
    logger.info(f"Reverse search for: {image_url}")
    
    try:
        search_result = await perform_search(image_url, request.timeout or 30)
        raw_urls = list(search_result.urls)
        
        if not search_result.urls:
            return SearchResponse(
                found=False,
                image_url=image_url,
                results=[],
                matched_urls=raw_urls,
                search_engines_used=search_result.engines_used,
                error="; ".join(search_result.errors) if search_result.errors else "No matches found"
            )
        
        # Deduplicate
        unique_urls = deduplicate_urls(search_result.urls)
        
        # Prioritize stock photo domains
        prioritized = []
        for url in unique_urls:
            priority = 0
            for i, domain in enumerate(PRIORITY_DOMAINS):
                if domain in url.lower():
                    priority = len(PRIORITY_DOMAINS) - i
                    break
            prioritized.append((url, priority))
        prioritized.sort(key=lambda x: -x[1])
        
        results = []
        max_scrape = min(request.max_results or 10, 3)
        
        for url, priority in prioritized[:max_scrape]:
            # Pass all URLs so we can extract metadata from any of them
            metadata = await scrape_with_playwright(url, all_matched_urls=raw_urls)
            
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
                creator=metadata.get("creator"),
                creator_url=metadata.get("creator_url"),
                date_created=metadata.get("date_created"),
                description=metadata.get("description"),
                keywords=metadata.get("keywords", []),
                location=metadata.get("location"),
                copyright=metadata.get("copyright"),
                license=metadata.get("license"),
                source_url=metadata.get("source_url", url),
                source_domain=get_source_domain(url),
                confidence=min(score, 1.0),
                scrape_status=metadata.get("scrape_status", "unknown")
            ))
        
        results.sort(key=lambda x: x.confidence, reverse=True)
        
        # Add error message if we couldn't get photographer
        error_msg = None
        if results and not results[0].creator and "pexels" in (results[0].source_domain or ""):
            error_msg = "Photographer requires PEXELS_API_KEY and/or PEXELS_API_KEY_BACKUP environment variables"
        
        return SearchResponse(
            found=len(results) > 0,
            image_url=image_url,
            results=results,
            matched_urls=raw_urls,
            search_engines_used=search_result.engines_used,
            total_matches_found=len(raw_urls),
            error=error_msg
        )
        
    except Exception as e:
        logger.error(f"Search error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)
