"""Reverse Image Attribution Service"""

import asyncio
import logging
import re
import json
import os
import aiohttp
import hashlib
import random
from typing import Optional, List
from urllib.parse import quote_plus, urlparse, parse_qs
from dataclasses import dataclass, field

from fastapi import FastAPI, HTTPException, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, HttpUrl
from bs4 import BeautifulSoup

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Reverse Image Attribution API", version="3.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

# Realistic browser headers
BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Cache-Control": "max-age=0",
}


# ============== MODELS ==============

class SearchRequest(BaseModel):
    image_url: HttpUrl
    max_results: Optional[int] = 10
    timeout: Optional[int] = 30
    engines: Optional[List[str]] = None


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


# ============== URL TRANSFORMATION ==============

def transform_url_to_page(url: str) -> str:
    """Transform CDN image URLs to their corresponding page URLs."""
    try:
        parsed = urlparse(url)
        domain = parsed.netloc.lower()
        path = parsed.path
        
        # Pexels: images.pexels.com/photos/ID/... -> www.pexels.com/photo/ID/
        if "images.pexels.com" in domain or "pexels.com" in domain:
            match = re.search(r'/photos?/(\d+)', path)
            if match:
                photo_id = match.group(1)
                logger.info(f"Pexels transform: {url} -> https://www.pexels.com/photo/{photo_id}/")
                return f"https://www.pexels.com/photo/{photo_id}/"
        
        # Unsplash: images.unsplash.com/photo-ID... -> unsplash.com/photos/ID
        if "images.unsplash.com" in domain or "unsplash.com" in domain:
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
    except Exception as e:
        logger.warning(f"URL transform failed: {e}")
    
    return url


PRIORITY_DOMAINS = [
    "gettyimages.com", "shutterstock.com", "unsplash.com", "pexels.com", 
    "pixabay.com", "flickr.com", "alamy.com", "istockphoto.com",
    "stock.adobe.com", "500px.com", "depositphotos.com"
]


# ============== SCRAPER ==============

async def fetch_page(url: str, timeout: int = 15, retry: int = 0) -> Optional[str]:
    """Fetch a page with retries and delays."""
    try:
        # Add small random delay to be polite
        if retry > 0:
            await asyncio.sleep(1 + random.random())
        
        connector = aiohttp.TCPConnector(ssl=False)  # Sometimes helps with SSL issues
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=timeout),
            connector=connector,
            headers=BROWSER_HEADERS
        ) as session:
            async with session.get(url, allow_redirects=True) as response:
                logger.info(f"Fetch {url} -> Status: {response.status}")
                
                if response.status == 403:
                    logger.warning(f"403 Forbidden for {url}")
                    return None
                
                if response.status != 200:
                    logger.warning(f"HTTP {response.status} for {url}")
                    return None
                
                content_type = response.headers.get("Content-Type", "")
                if "image/" in content_type:
                    logger.warning(f"Got image content-type for {url}")
                    return None
                
                html = await response.text()
                
                # Check for Cloudflare challenge
                if "Just a moment" in html[:1000] or "_cf_chl_opt" in html[:2000]:
                    logger.warning(f"Cloudflare challenge detected for {url}")
                    if retry < 2:
                        await asyncio.sleep(2)
                        return await fetch_page(url, timeout, retry + 1)
                    return None
                
                return html
                
    except asyncio.TimeoutError:
        logger.warning(f"Timeout fetching {url}")
        return None
    except Exception as e:
        logger.error(f"Fetch error for {url}: {e}")
        return None


async def scrape_page(url: str, timeout: int = 15) -> dict:
    """Scrape metadata from a page URL."""
    # Always transform the URL first
    page_url = transform_url_to_page(url)
    domain = urlparse(page_url).netloc.lower()
    
    # Determine license based on domain
    license_info = None
    if "pexels.com" in domain:
        license_info = "Pexels License"
    elif "unsplash.com" in domain:
        license_info = "Unsplash License"
    elif "pixabay.com" in domain:
        license_info = "Pixabay License"
    elif "flickr.com" in domain:
        license_info = "Various (check source)"
    elif "shutterstock.com" in domain:
        license_info = "Shutterstock License (Paid)"
    elif "gettyimages.com" in domain:
        license_info = "Getty Images License (Paid)"
    
    result = {
        "type": "image",
        "title": None,
        "creator": None,
        "creator_url": None,
        "description": None,
        "keywords": [],
        "location": None,
        "license": license_info,
        "date_created": None,
        "copyright": None,
        "source_url": page_url,  # Always use the transformed URL
        "scrape_status": "pending"
    }
    
    html = await fetch_page(page_url, timeout)
    
    if not html:
        result["scrape_status"] = "failed"
        logger.warning(f"Failed to fetch {page_url}")
        return result
    
    try:
        soup = BeautifulSoup(html, "html.parser")
        
        # ===== Try JSON-LD first (most reliable) =====
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
                    result["title"] = data.get("name")
                    result["description"] = data.get("description")
                    date = data.get("dateCreated") or data.get("uploadDate")
                    if date:
                        result["date_created"] = date[:10]
                    loc = data.get("contentLocation")
                    if isinstance(loc, dict):
                        result["location"] = loc.get("name")
                    elif isinstance(loc, str):
                        result["location"] = loc
                    kw = data.get("keywords")
                    if isinstance(kw, list):
                        result["keywords"] = kw[:20]
                    elif isinstance(kw, str):
                        result["keywords"] = [k.strip() for k in kw.split(",")][:20]
                    logger.info(f"Extracted from JSON-LD: creator={result['creator']}, title={result['title']}")
                    break
            except:
                pass
        
        # ===== Pexels-specific extraction =====
        if "pexels.com" in domain and not result["creator"]:
            # Look for photographer link
            for link in soup.find_all("a", href=re.compile(r'/@[a-zA-Z0-9_-]+')):
                text = link.get_text().strip()
                if text and len(text) < 100:
                    result["creator"] = text
                    href = link.get("href", "")
                    if href.startswith("/"):
                        result["creator_url"] = f"https://www.pexels.com{href}"
                    else:
                        result["creator_url"] = href
                    logger.info(f"Found Pexels photographer: {text}")
                    break
        
        # ===== Fallback to meta tags =====
        if not result["title"]:
            og = soup.find("meta", {"property": "og:title"})
            if og:
                title = og.get("content", "").strip()
                # Clean up common suffixes
                title = re.sub(r'\s*[·|\-]\s*(Pexels|Unsplash|Pixabay|Photo).*$', '', title, flags=re.IGNORECASE)
                result["title"] = title
        
        if not result["creator"]:
            author = soup.find("meta", {"name": "author"})
            if author:
                result["creator"] = author.get("content", "").strip()
        
        if not result["description"]:
            desc = soup.find("meta", {"property": "og:description"}) or soup.find("meta", {"name": "description"})
            if desc:
                result["description"] = desc.get("content", "").strip()[:500]
        
        # Build copyright
        if result["creator"]:
            year = result["date_created"][:4] if result["date_created"] else None
            if year:
                result["copyright"] = f"© {year} {result['creator']}"
            else:
                result["copyright"] = f"© {result['creator']}"
        
        # Determine status
        if result["creator"] and result["title"]:
            result["scrape_status"] = "success"
        elif result["creator"] or result["title"]:
            result["scrape_status"] = "partial"
        else:
            result["scrape_status"] = "failed"
        
        logger.info(f"Scrape result for {page_url}: status={result['scrape_status']}, creator={result['creator']}")
        return result
        
    except Exception as e:
        logger.error(f"Parse error for {page_url}: {e}")
        result["scrape_status"] = "failed"
        return result


def get_source_domain(url: str) -> str:
    """Extract source domain from URL."""
    try:
        domain = urlparse(url).netloc.lower().replace("www.", "")
        if "pexels.com" in domain:
            return "pexels"
        if "unsplash.com" in domain:
            return "unsplash"
        if "pixabay.com" in domain:
            return "pixabay"
        if "flickr.com" in domain:
            return "flickr"
        if "shutterstock.com" in domain:
            return "shutterstock"
        if "gettyimages.com" in domain:
            return "gettyimages"
        return "generic"
    except:
        return "generic"


# ============== SEARCH ENGINES ==============

@dataclass
class SearchResult:
    urls: List[str] = field(default_factory=list)
    engines_used: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)


async def search_yandex(image_url: str, timeout: int = 30) -> tuple:
    """Search using Yandex Images."""
    urls = []
    
    try:
        encoded = quote_plus(image_url)
        search_url = f"https://yandex.com/images/search?rpt=imageview&url={encoded}"
        
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=timeout),
            headers=BROWSER_HEADERS
        ) as session:
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
    """Search using Bing Images."""
    urls = []
    
    try:
        encoded = quote_plus(image_url)
        search_url = f"https://www.bing.com/images/search?view=detailv2&iss=sbi&q=imgurl:{encoded}"
        
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=timeout),
            headers=BROWSER_HEADERS
        ) as session:
            async with session.get(search_url, allow_redirects=True) as resp:
                if resp.status != 200:
                    return ("bing", [], f"HTTP {resp.status}")
                html = await resp.text()
        
        soup = BeautifulSoup(html, "html.parser")
        for link in soup.find_all("a", href=True):
            href = link["href"]
            if href.startswith("http") and not any(x in href.lower() for x in ["bing.com", "microsoft.com", "msn.com"]):
                urls.append(href)
        
        return ("bing", list(dict.fromkeys(urls))[:25], None)
    except Exception as e:
        return ("bing", [], str(e))


async def perform_search(image_url: str, timeout: int = 30) -> SearchResult:
    """Perform reverse image search using multiple engines."""
    result = SearchResult()
    
    tasks = [
        search_yandex(image_url, timeout),
        search_bing(image_url, timeout),
    ]
    
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
    """Deduplicate URLs by their photo ID."""
    seen_ids = set()
    unique = []
    
    for url in urls:
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
        
        unique.append(url)
    
    return unique


# ============== ENDPOINTS ==============

@app.get("/")
async def root():
    return {"status": "healthy", "service": "reverse-image-attribution", "version": "3.1.0"}


@app.get("/health")
async def health():
    return {"status": "ok"}


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
        
        # Deduplicate URLs
        unique_urls = deduplicate_urls(search_result.urls)
        
        # Prioritize known stock photo domains
        prioritized = []
        for url in unique_urls:
            priority = 0
            for i, domain in enumerate(PRIORITY_DOMAINS):
                if domain in url.lower():
                    priority = len(PRIORITY_DOMAINS) - i
                    break
            prioritized.append((url, priority))
        prioritized.sort(key=lambda x: -x[1])
        
        # Scrape top results (only unique photos)
        results = []
        max_scrape = min(request.max_results or 10, 5)  # Limit to reduce timeouts
        
        for url, priority in prioritized[:max_scrape]:
            metadata = await scrape_page(url)
            
            # Calculate confidence
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
            
            # Small delay between requests
            await asyncio.sleep(0.5)
        
        results.sort(key=lambda x: x.confidence, reverse=True)
        
        return SearchResponse(
            found=len(results) > 0,
            image_url=image_url,
            results=results,
            matched_urls=raw_urls,
            search_engines_used=search_result.engines_used,
            total_matches_found=len(raw_urls)
        )
        
    except Exception as e:
        logger.error(f"Search error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)
