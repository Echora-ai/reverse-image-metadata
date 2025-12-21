"""Reverse Image Attribution Service with Playwright v4.5.0"""

import asyncio
import logging
import re
import json
import os
import aiohttp
import hashlib
from typing import Optional, List
from urllib.parse import quote_plus, urlparse
from dataclasses import dataclass, field

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, HttpUrl
from bs4 import BeautifulSoup

# Playwright for Cloudflare bypass
from playwright.async_api import async_playwright, Browser

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Reverse Image Attribution API", version="4.5.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

# API Keys from environment
PEXELS_API_KEY = os.environ.get("PEXELS_API_KEY", "")
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
                '--disable-infobars',
                '--window-size=1920,1080',
                '--start-maximized',
            ]
        )
        logger.info("Browser launched successfully")
    return _browser


# Stealth mode init script to hide automation
STEALTH_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', {
    get: () => undefined
});

Object.defineProperty(navigator, 'plugins', {
    get: () => [1, 2, 3, 4, 5]
});

Object.defineProperty(navigator, 'languages', {
    get: () => ['en-US', 'en']
});

window.chrome = {
    runtime: {}
};

Object.defineProperty(navigator, 'platform', {
    get: () => 'MacIntel'
});
"""


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

def extract_pexels_photo_id(url: str) -> Optional[str]:
    """Extract Pexels photo ID from various URL formats."""
    patterns = [
        r'/photos?/(\d+)',                          # /photos/15647646 or /photo/15647646
        r'-(\d+)/?$',                               # slug-15647646/
        r'pexels-photo-(\d+)',                      # pexels-photo-15647646.jpeg
        r'/(\d+)(?:\?|$|/)',                        # /15647646/ or /15647646?
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            photo_id = match.group(1)
            logger.info(f"Extracted Pexels photo ID: {photo_id} from {url}")
            return photo_id
    return None


async def fetch_pexels_via_api(photo_id: str) -> Optional[dict]:
    """Fetch photo metadata from Pexels API."""
    if not PEXELS_API_KEY:
        logger.info("No PEXELS_API_KEY set, skipping API lookup")
        return None
    
    api_url = f"https://api.pexels.com/v1/photos/{photo_id}"
    headers = {
        "Authorization": PEXELS_API_KEY,
        "User-Agent": "Echora Image Attribution Service"
    }
    
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
            async with session.get(api_url, headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    logger.info(f"Pexels API success for photo {photo_id}")
                    
                    # Extract metadata from API response
                    result = {
                        "title": data.get("alt") or f"Photo by {data.get('photographer', 'Unknown')}",
                        "creator": data.get("photographer"),
                        "creator_url": data.get("photographer_url"),
                        "description": data.get("alt"),
                        "keywords": [],
                        "location": None,  # Pexels API doesn't provide location directly
                        "license": "Pexels License",
                        "date_created": None,
                        "copyright": f"© {data.get('photographer')}" if data.get('photographer') else None,
                        "source_url": data.get("url"),
                        "scrape_status": "success"
                    }
                    
                    logger.info(f"Pexels API returned: creator={result['creator']}, title={result['title']}")
                    return result
                    
                elif resp.status == 404:
                    logger.warning(f"Pexels photo {photo_id} not found via API")
                else:
                    logger.warning(f"Pexels API returned status {resp.status}")
                    
    except Exception as e:
        logger.error(f"Pexels API error: {e}")
    
    return None


# ============== URL TRANSFORMATION ==============

def transform_url_to_page(url: str) -> str:
    """Transform CDN image URLs to their corresponding page URLs.
    
    For Pexels CDN URLs like:
    https://images.pexels.com/photos/15647646/pexels-photo-15647646/free-photo-of-a-man-in-a-tank-top-and-pants-standing-outside.jpeg
    
    We extract the slug and ID to build:
    https://www.pexels.com/photo/a-man-in-a-tank-top-and-pants-standing-outside-15647646/
    """
    try:
        parsed = urlparse(url)
        domain = parsed.netloc.lower()
        path = parsed.path
        
        # Pexels
        if "pexels.com" in domain:
            # First try to extract photo ID
            match = re.search(r'/photos?/(\d+)', path)
            if match:
                photo_id = match.group(1)
                
                # Try to extract the slug from "free-photo-of-{slug}" pattern
                slug_match = re.search(r'free-photo-of-([a-z0-9-]+)\.', path, re.IGNORECASE)
                if slug_match:
                    slug = slug_match.group(1)
                    full_url = f"https://www.pexels.com/photo/{slug}-{photo_id}/"
                    logger.info(f"Pexels: extracted slug '{slug}' and ID {photo_id} -> {full_url}")
                    return full_url
                
                # Fallback: just use the ID (Pexels will redirect)
                logger.info(f"Pexels: extracted ID {photo_id} (no slug found)")
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


# ============== LOCATION DETECTION HELPERS ==============

# US state codes
US_STATE_CODES = [
    "al", "ak", "az", "ar", "ca", "co", "ct", "de", "fl", "ga",
    "hi", "id", "il", "in", "ia", "ks", "ky", "la", "me", "md",
    "ma", "mi", "mn", "ms", "mo", "mt", "ne", "nv", "nh", "nj",
    "nm", "ny", "nc", "nd", "oh", "ok", "or", "pa", "ri", "sc",
    "sd", "tn", "tx", "ut", "vt", "va", "wa", "wv", "wi", "wy", "dc"
]

# Canadian provinces
CA_PROVINCE_CODES = ["ab", "bc", "mb", "nb", "nl", "ns", "nt", "nu", "on", "pe", "qc", "sk", "yt"]

# Common country indicators
COUNTRY_INDICATORS = [
    "united states", "usa", "u.s.a", "uk", "united kingdom", "canada", 
    "australia", "germany", "france", "italy", "spain", "japan", "china",
    "brazil", "mexico", "india", "netherlands", "sweden", "norway", "denmark",
    "switzerland", "austria", "belgium", "portugal", "poland", "czech",
    "greece", "turkey", "russia", "south korea", "singapore", "thailand",
    "vietnam", "indonesia", "philippines", "malaysia", "new zealand"
]


def looks_like_location(text: str) -> bool:
    """Check if text looks like a location (city, state, country format)."""
    if not text or len(text) > 100 or len(text) < 3:
        return False
    
    # Must contain comma for city, state/country format
    if "," not in text:
        return False
    
    parts = [p.strip() for p in text.split(",")]
    
    # Should have 2-4 parts (city, state) or (city, state, country) or (city, country)
    if not (2 <= len(parts) <= 4):
        return False
    
    # First part should be capitalized (city name)
    first_part = parts[0]
    if not first_part or not first_part[0].isupper():
        return False
    
    text_lower = text.lower()
    
    # Check for country indicators
    for country in COUNTRY_INDICATORS:
        if country in text_lower:
            return True
    
    # Check for US state codes (", XX" or ", XX,")
    for state in US_STATE_CODES:
        if f", {state}," in text_lower or text_lower.endswith(f", {state}"):
            return True
    
    # Check for Canadian province codes
    for prov in CA_PROVINCE_CODES:
        if f", {prov}," in text_lower or text_lower.endswith(f", {prov}"):
            return True
    
    # If second part is exactly 2 characters and uppercase, likely a state code
    if len(parts) >= 2:
        second_part = parts[1].strip()
        if len(second_part) == 2 and second_part.isalpha():
            return True
    
    return False


# ============== PLAYWRIGHT SCRAPER ==============

async def create_stealth_context(browser: Browser):
    """Create a browser context with stealth settings."""
    context = await browser.new_context(
        user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        viewport={"width": 1920, "height": 1080},
        java_script_enabled=True,
        locale="en-US",
        timezone_id="America/New_York",
        extra_http_headers={
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "Sec-Ch-Ua": '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": '"macOS"',
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
            "Upgrade-Insecure-Requests": "1",
        },
    )
    # Add stealth scripts
    await context.add_init_script(STEALTH_SCRIPT)
    return context


async def scrape_with_playwright(url: str, timeout: int = 25) -> dict:
    """Scrape a page using Playwright (bypasses Cloudflare)."""
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
    elif "shutterstock.com" in domain:
        license_info = "Shutterstock License (Paid)"
    elif "gettyimages.com" in domain:
        license_info = "Getty Images License (Paid)"
    
    # ===== TRY PEXELS API FIRST =====
    if "pexels.com" in url.lower():
        photo_id = extract_pexels_photo_id(url)
        if photo_id:
            api_result = await fetch_pexels_via_api(photo_id)
            if api_result:
                return api_result
            logger.info(f"Pexels API failed or unavailable, falling back to scraping")
    
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
    
    page = None
    context = None
    try:
        browser = await get_browser()
        context = await create_stealth_context(browser)
        page = await context.new_page()
        
        logger.info(f"Playwright: navigating to {page_url}")
        
        # Navigate and wait for network to be mostly idle
        response = await page.goto(page_url, wait_until="networkidle", timeout=timeout * 1000)
        
        # Log the final URL after any redirects
        final_url = page.url
        logger.info(f"Final URL after redirect: {final_url}")
        result["source_url"] = final_url
        
        if response:
            logger.info(f"Response status: {response.status}")
            if response.status >= 400:
                result["scrape_status"] = "failed"
                await context.close()
                return result
        
        # ===== PEXELS SPECIFIC =====
        if "pexels.com" in domain:
            logger.info("Pexels detected - waiting for page elements to load...")
            
            # Wait for the main content to be visible - increased timeout
            try:
                # Wait for photographer link to appear (this is key!)
                await page.wait_for_selector('a[href*="/@"]', timeout=10000)
                logger.info("Photographer link selector found")
            except Exception as e:
                logger.warning(f"Timeout waiting for photographer link: {e}")
            
            # Additional wait for JavaScript rendering - INCREASED from 2s to 5s
            await asyncio.sleep(5)
            logger.info("Completed 5 second wait for JS rendering")
            
            try:
                # ===== PHOTOGRAPHER EXTRACTION =====
                # Method 1: Look for heading elements inside links to /@username
                # Pexels structure: <a href="/@username"><h2>Photographer Name</h2></a>
                headings = await page.query_selector_all('h1, h2, h3, h4')
                for heading in headings:
                    try:
                        parent = await heading.evaluate_handle('el => el.parentElement')
                        if parent:
                            tag_name = await parent.evaluate('el => el.tagName')
                            if tag_name == 'A':
                                href = await parent.evaluate('el => el.getAttribute("href")')
                                if href and ("/@" in href or "/users/" in href):
                                    text = await heading.inner_text()
                                    if text and len(text.strip()) < 100:
                                        result["creator"] = text.strip()
                                        result["creator_url"] = f"https://www.pexels.com{href}" if href.startswith("/") else href
                                        logger.info(f"Found creator via heading in link: {result['creator']}")
                                        break
                    except Exception as inner_e:
                        logger.debug(f"Heading check error: {inner_e}")
                        continue
                
                # Method 2: Look for photographer link with /@username pattern
                if not result["creator"]:
                    photographer_links = await page.query_selector_all('a[href*="/@"]')
                    logger.info(f"Found {len(photographer_links)} links with /@")
                    for link in photographer_links:
                        try:
                            href = await link.get_attribute("href")
                            text = await link.inner_text()
                            text = text.strip() if text else ""
                            logger.info(f"Checking link: href={href}, text='{text}'")
                            if text and len(text) < 100 and "/@" in (href or ""):
                                # Skip navigation/menu links
                                if text.lower() not in ["login", "join", "home", "explore", "upload", "", "follow"]:
                                    result["creator"] = text
                                    if href:
                                        result["creator_url"] = f"https://www.pexels.com{href}" if href.startswith("/") else href
                                    logger.info(f"Found creator via /@link: {text}")
                                    break
                        except Exception as link_e:
                            logger.debug(f"Link check error: {link_e}")
                            continue
                
                # Method 3: Check for "Photo by" text pattern in page content
                if not result["creator"]:
                    page_text = await page.content()
                    photo_by_match = re.search(r'Photo\s+by\s+([^<>"\\n]{2,50})', page_text, re.IGNORECASE)
                    if photo_by_match:
                        creator = photo_by_match.group(1).strip()
                        # Clean up common suffixes
                        creator = re.sub(r'\s+on\s+Pexels.*$', '', creator, flags=re.IGNORECASE)
                        if len(creator) < 100 and len(creator) > 1:
                            result["creator"] = creator
                            logger.info(f"Found creator via 'Photo by': {creator}")
                
                # ===== LOCATION EXTRACTION =====
                # Method 1: Look for location in page text with comma-separated format
                all_elements = await page.query_selector_all('span, div, p, a')
                for elem in all_elements:
                    try:
                        text = await elem.inner_text()
                        text = text.strip() if text else ""
                        if looks_like_location(text):
                            result["location"] = text
                            logger.info(f"Found location via element text: {text}")
                            break
                    except:
                        continue
                
                # Method 2: Look for specific location patterns in full page content
                if not result["location"]:
                    page_content = await page.content()
                    # Pattern: "Tampa, FL, United States" or similar
                    location_patterns = [
                        r'([A-Z][a-zA-Z\s]+,\s*[A-Z]{2},\s*United States)',
                        r'([A-Z][a-zA-Z\s]+,\s*[A-Z]{2},\s*USA)',
                        r'([A-Z][a-zA-Z\s]+,\s*[A-Z][a-zA-Z\s]+,\s*[A-Z][a-zA-Z\s]+)',
                    ]
                    for pattern in location_patterns:
                        match = re.search(pattern, page_content)
                        if match:
                            loc = match.group(1).strip()
                            # Validate it's not too generic
                            if len(loc) > 5 and "," in loc:
                                result["location"] = loc
                                logger.info(f"Found location via regex: {loc}")
                                break
                
                # Method 3: Look for elements with location-related attributes
                if not result["location"]:
                    location_elems = await page.query_selector_all('[data-testid*="location"], [class*="location"], [class*="place"]')
                    for elem in location_elems:
                        try:
                            text = await elem.inner_text()
                            if text and len(text.strip()) < 100 and len(text.strip()) > 2:
                                result["location"] = text.strip()
                                logger.info(f"Found location via data-testid/class: {text.strip()}")
                                break
                        except:
                            continue
                
                # ===== TITLE from h1 =====
                if not result["title"]:
                    h1 = await page.query_selector('h1')
                    if h1:
                        h1_text = await h1.inner_text()
                        if h1_text:
                            result["title"] = h1_text.strip()
                            logger.info(f"Found title: {result['title']}")
                
            except Exception as e:
                logger.warning(f"Pexels-specific extraction error: {e}")
        else:
            # Non-Pexels sites - standard wait
            await asyncio.sleep(3)
        
        # Get full page HTML for further parsing
        html = await page.content()
        await context.close()
        context = None
        
        if not html or len(html) < 500:
            logger.warning(f"Page HTML too short: {len(html) if html else 0} bytes")
            result["scrape_status"] = "failed"
            return result
        
        # Check for Cloudflare challenge
        if "Just a moment" in html or "Checking your browser" in html:
            logger.warning("Cloudflare challenge detected")
            result["scrape_status"] = "failed"
            return result
        
        soup = BeautifulSoup(html, "html.parser")
        
        # ===== JSON-LD EXTRACTION =====
        for script in soup.find_all("script", {"type": "application/ld+json"}):
            try:
                data = json.loads(script.string) if script.string else {}
                if isinstance(data, list):
                    data = data[0] if data else {}
                
                obj_type = data.get("@type", "")
                if obj_type in ["ImageObject", "Photograph", "CreativeWork", "MediaObject"]:
                    logger.info(f"Found JSON-LD type: {obj_type}")
                    
                    # Author/Creator
                    if not result["creator"]:
                        author = data.get("author") or data.get("creator")
                        if isinstance(author, dict):
                            result["creator"] = author.get("name")
                            result["creator_url"] = author.get("url")
                        elif isinstance(author, str):
                            result["creator"] = author
                    
                    # Title
                    if not result["title"]:
                        result["title"] = data.get("name") or data.get("headline")
                    
                    # Description
                    if not result["description"]:
                        result["description"] = data.get("description")
                    
                    # Date
                    if not result["date_created"]:
                        date = data.get("dateCreated") or data.get("uploadDate") or data.get("datePublished")
                        if date:
                            result["date_created"] = date[:10]
                    
                    # Location
                    if not result["location"]:
                        loc = data.get("contentLocation") or data.get("locationCreated")
                        if isinstance(loc, dict):
                            result["location"] = loc.get("name") or loc.get("address")
                        elif isinstance(loc, str):
                            result["location"] = loc
                    
                    # Keywords
                    if not result["keywords"]:
                        kw = data.get("keywords")
                        if isinstance(kw, list):
                            result["keywords"] = kw[:20]
                        elif isinstance(kw, str):
                            result["keywords"] = [k.strip() for k in kw.split(",")][:20]
                    
                    logger.info(f"JSON-LD extracted: creator={result['creator']}, location={result['location']}")
                    break
            except Exception as e:
                logger.warning(f"JSON-LD parse error: {e}")
        
        # ===== META TAGS FALLBACK =====
        if not result["title"]:
            og_title = soup.find("meta", {"property": "og:title"})
            if og_title:
                title = og_title.get("content", "").strip()
                # Clean up "Photo by X on Pexels" patterns
                title = re.sub(r'\s*[·|\-–—]\s*(Pexels|Unsplash|Pixabay).*$', '', title, flags=re.IGNORECASE)
                result["title"] = title
        
        if not result["creator"]:
            # Check meta author
            author = soup.find("meta", {"name": "author"})
            if author:
                result["creator"] = author.get("content", "").strip()
            
            # Check for "Photo by" in og:title
            if not result["creator"]:
                og_title = soup.find("meta", {"property": "og:title"})
                if og_title:
                    content = og_title.get("content", "")
                    match = re.search(r'Photo\s+by\s+([^·|\-–—]+)', content, re.IGNORECASE)
                    if match:
                        result["creator"] = match.group(1).strip()
        
        if not result["description"]:
            desc = soup.find("meta", {"property": "og:description"}) or soup.find("meta", {"name": "description"})
            if desc:
                result["description"] = desc.get("content", "").strip()[:500]
        
        # ===== LOCATION FALLBACK from HTML =====
        if not result["location"]:
            # Try to find location in span/div elements
            for elem in soup.find_all(["span", "div", "p", "a"]):
                text = elem.get_text().strip()
                if looks_like_location(text):
                    result["location"] = text
                    logger.info(f"Found location via BeautifulSoup: {text}")
                    break
        
        # ===== COPYRIGHT =====
        if result["creator"]:
            year = result["date_created"][:4] if result["date_created"] else None
            result["copyright"] = f"© {year} {result['creator']}" if year else f"© {result['creator']}"
        
        # ===== STATUS =====
        if result["creator"] and result["title"]:
            result["scrape_status"] = "success"
        elif result["creator"] or result["title"]:
            result["scrape_status"] = "partial"
        else:
            result["scrape_status"] = "failed"
        
        logger.info(f"Scrape complete: status={result['scrape_status']}, creator={result['creator']}, location={result['location']}, title={result['title']}")
        return result
        
    except Exception as e:
        logger.error(f"Playwright error for {page_url}: {e}")
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


# ============== SEARCH (aiohttp is fine for search engines) ==============

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
    return {
        "status": "healthy", 
        "service": "reverse-image-attribution", 
        "version": "4.5.0", 
        "browser": "playwright",
        "pexels_api": "enabled" if PEXELS_API_KEY else "disabled"
    }

@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/debug-scrape")
async def debug_scrape(request: DebugRequest):
    """Debug endpoint to see what the scraper sees on a page."""
    url = request.url
    timeout = request.timeout or 30
    
    context = None
    try:
        browser = await get_browser()
        context = await create_stealth_context(browser)
        page = await context.new_page()
        
        response = await page.goto(url, wait_until="networkidle", timeout=timeout * 1000)
        
        # Wait a bit for JS
        await asyncio.sleep(3)
        
        final_url = page.url
        status = response.status if response else None
        
        html = await page.content()
        title = await page.title()
        
        # Count some useful elements
        at_links = await page.query_selector_all('a[href*="/@"]')
        h1_elements = await page.query_selector_all('h1')
        h2_elements = await page.query_selector_all('h2')
        
        # Get h1 text
        h1_text = None
        if h1_elements:
            h1_text = await h1_elements[0].inner_text()
        
        # Get first @ link
        first_at_link = None
        if at_links:
            href = await at_links[0].get_attribute("href")
            text = await at_links[0].inner_text()
            first_at_link = {"href": href, "text": text}
        
        # Check for cloudflare
        is_cloudflare = "Just a moment" in html or "Checking your browser" in html
        
        await context.close()
        
        return {
            "url_requested": url,
            "final_url": final_url,
            "status": status,
            "title": title,
            "html_length": len(html),
            "is_cloudflare_blocked": is_cloudflare,
            "h1_text": h1_text,
            "at_link_count": len(at_links),
            "first_at_link": first_at_link,
            "h1_count": len(h1_elements),
            "h2_count": len(h2_elements),
            "html_preview": html[:2000] if html else None,
        }
        
    except Exception as e:
        if context:
            try:
                await context.close()
            except:
                pass
        return {"error": str(e), "url": url}


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
        
        # Scrape with Playwright (limit to 3 to avoid timeout)
        results = []
        max_scrape = min(request.max_results or 10, 3)
        
        for url, priority in prioritized[:max_scrape]:
            metadata = await scrape_with_playwright(url)
            
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
