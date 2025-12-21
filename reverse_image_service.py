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

app = FastAPI(title="Reverse Image Attribution API", version="3.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

# User agents for rotation
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
]

def get_random_user_agent():
    return random.choice(USER_AGENTS)


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
        
        if "images.pexels.com" in domain:
            match = re.search(r'/photos/(\d+)/', path)
            if match:
                return f"https://www.pexels.com/photo/{match.group(1)}/"
        
        if "images.unsplash.com" in domain:
            match = re.search(r'photo-([a-zA-Z0-9_-]+)', path)
            if match:
                return f"https://unsplash.com/photos/{match.group(1)}"
        
        if "pixabay.com" in domain and "/photo/" in path:
            match = re.search(r'-(\d+)\.[a-z]+$', path, re.IGNORECASE)
            if match:
                return f"https://pixabay.com/photos/id-{match.group(1)}/"
    except Exception as e:
        logger.warning(f"URL transform failed: {e}")
    
    return url


PRIORITY_DOMAINS = [
    "gettyimages.com", "shutterstock.com", "unsplash.com", "pexels.com", 
    "pixabay.com", "flickr.com", "alamy.com", "istockphoto.com",
    "stock.adobe.com", "500px.com", "depositphotos.com"
]


# ============== SCRAPER ==============

async def scrape_page(url: str, timeout: int = 15) -> dict:
    """Scrape metadata from a page URL."""
    result = {
        "type": "image",
        "title": None,
        "creator": None,
        "creator_url": None,
        "description": None,
        "keywords": [],
        "location": None,
        "license": None,
        "date_created": None,
        "copyright": None,
        "source_url": url,
        "scrape_status": "pending"
    }
    
    page_url = transform_url_to_page(url)
    
    try:
        headers = {
            "User-Agent": get_random_user_agent(),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }
        
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
            async with session.get(page_url, headers=headers, allow_redirects=True) as response:
                if response.status != 200:
                    result["scrape_status"] = "failed"
                    return result
                
                content_type = response.headers.get("Content-Type", "")
                if "image/" in content_type:
                    result["scrape_status"] = "failed"
                    return result
                
                html = await response.text()
        
        if not html or "Just a moment" in html[:500]:
            result["scrape_status"] = "failed"
            return result
        
        soup = BeautifulSoup(html, "html.parser")
        
        # Try JSON-LD first
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
                    break
            except:
                pass
        
        # Fallback to meta tags
        if not result["title"]:
            og = soup.find("meta", {"property": "og:title"})
            if og:
                result["title"] = og.get("content", "").strip()
        
        if not result["creator"]:
            author = soup.find("meta", {"name": "author"})
            if author:
                result["creator"] = author.get("content", "").strip()
        
        if not result["description"]:
            desc = soup.find("meta", {"property": "og:description"}) or soup.find("meta", {"name": "description"})
            if desc:
                result["description"] = desc.get("content", "").strip()[:500]
        
        # Determine domain-specific license
        domain = urlparse(page_url).netloc.lower()
        if "pexels.com" in domain:
            result["license"] = "Pexels License"
        elif "unsplash.com" in domain:
            result["license"] = "Unsplash License"
        elif "pixabay.com" in domain:
            result["license"] = "Pixabay License"
        elif "flickr.com" in domain:
            result["license"] = "Various (check source)"
        elif "shutterstock.com" in domain:
            result["license"] = "Shutterstock License (Paid)"
        elif "gettyimages.com" in domain:
            result["license"] = "Getty Images License (Paid)"
        
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
        
        result["source_url"] = page_url
        return result
        
    except Exception as e:
        logger.error(f"Scrape error for {url}: {e}")
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
    headers = {
        "User-Agent": get_random_user_agent(),
        "Accept": "text/html,application/xhtml+xml",
    }
    
    try:
        encoded = quote_plus(image_url)
        search_url = f"https://yandex.com/images/search?rpt=imageview&url={encoded}"
        
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
            async with session.get(search_url, headers=headers) as resp:
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
    headers = {
        "User-Agent": get_random_user_agent(),
        "Accept": "text/html,application/xhtml+xml",
    }
    
    try:
        encoded = quote_plus(image_url)
        search_url = f"https://www.bing.com/images/search?view=detailv2&iss=sbi&q=imgurl:{encoded}"
        
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
            async with session.get(search_url, headers=headers, allow_redirects=True) as resp:
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


# ============== ENDPOINTS ==============

@app.get("/")
async def root():
    return {"status": "healthy", "service": "reverse-image-attribution", "version": "3.0.0"}


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
        
        # Prioritize known stock photo domains
        prioritized = []
        for url in search_result.urls:
            priority = 0
            for i, domain in enumerate(PRIORITY_DOMAINS):
                if domain in url.lower():
                    priority = len(PRIORITY_DOMAINS) - i
                    break
            prioritized.append((url, priority))
        prioritized.sort(key=lambda x: -x[1])
        
        # Scrape top results
        results = []
        max_scrape = min(request.max_results or 10, 8)
        
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
            
            await asyncio.sleep(0.2)
        
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
