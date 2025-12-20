"""Reverse Image Attribution Service

A FastAPI service that performs reverse image search and extracts
photographer attribution from known stock photo sites.
"""

import asyncio
import logging
from typing import Optional
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, HttpUrl

from search.reverse_search import ReverseImageSearch
from scrapers import get_scraper_for_url, PRIORITY_DOMAINS

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Reverse Image Attribution API",
    description="Find photographer credits for images via reverse image search",
    version="1.0.0"
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class SearchRequest(BaseModel):
    """Request model for reverse image search"""
    image_url: HttpUrl
    max_results: Optional[int] = 10
    timeout: Optional[int] = 30


class AttributionResult(BaseModel):
    """Single attribution result"""
    source: str
    source_url: str
    photographer: Optional[str] = None
    license: Optional[str] = None
    title: Optional[str] = None
    confidence: float = 0.0


class SearchResponse(BaseModel):
    """Response model for reverse image search"""
    found: bool
    image_url: str
    results: list[AttributionResult]
    search_engines_used: list[str]
    error: Optional[str] = None


@app.get("/")
async def root():
    """Health check endpoint"""
    return {
        "status": "healthy",
        "service": "reverse-image-attribution",
        "version": "1.0.0"
    }


@app.get("/health")
async def health_check():
    """Health check for Cloud Run"""
    return {"status": "ok"}


@app.post("/reverse-search", response_model=SearchResponse)
async def reverse_search(request: SearchRequest):
    """
    Perform reverse image search and extract photographer attribution.
    
    1. Runs reverse image search via Google Lens / Yandex
    2. Filters results for known stock photo sites
    3. Scrapes photographer credit from those pages
    """
    image_url = str(request.image_url)
    logger.info(f"Processing reverse search for: {image_url}")
    
    try:
        # Step 1: Reverse image search
        search_engine = ReverseImageSearch()
        search_results = await search_engine.search(
            image_url,
            max_results=request.max_results,
            timeout=request.timeout
        )
        
        if not search_results.urls:
            return SearchResponse(
                found=False,
                image_url=image_url,
                results=[],
                search_engines_used=search_results.engines_used
            )
        
        # Step 2: Filter and prioritize known sources
        prioritized_urls = _prioritize_urls(search_results.urls)
        
        # Step 3: Scrape attribution from each source
        attribution_results = []
        for url, priority in prioritized_urls[:5]:  # Limit to top 5 sources
            scraper = get_scraper_for_url(url)
            if scraper:
                try:
                    attribution = await scraper.scrape(url)
                    if attribution:
                        attribution_results.append(AttributionResult(
                            source=scraper.source_name,
                            source_url=url,
                            photographer=attribution.get("photographer"),
                            license=attribution.get("license"),
                            title=attribution.get("title"),
                            confidence=_calculate_confidence(attribution, priority)
                        ))
                except Exception as e:
                    logger.warning(f"Failed to scrape {url}: {e}")
                    continue
                
                # Rate limiting between scrapes
                await asyncio.sleep(0.5)
        
        # Sort by confidence
        attribution_results.sort(key=lambda x: x.confidence, reverse=True)
        
        return SearchResponse(
            found=len(attribution_results) > 0,
            image_url=image_url,
            results=attribution_results,
            search_engines_used=search_results.engines_used
        )
        
    except Exception as e:
        logger.error(f"Error processing {image_url}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


def _prioritize_urls(urls: list[str]) -> list[tuple[str, int]]:
    """
    Prioritize URLs based on known reliable sources.
    Returns list of (url, priority) tuples sorted by priority.
    """
    prioritized = []
    
    for url in urls:
        url_lower = url.lower()
        priority = 0
        
        for i, domain in enumerate(PRIORITY_DOMAINS):
            if domain in url_lower:
                priority = len(PRIORITY_DOMAINS) - i  # Higher priority for earlier domains
                break
        
        prioritized.append((url, priority))
    
    # Sort by priority (highest first), then by URL for consistency
    prioritized.sort(key=lambda x: (-x[1], x[0]))
    return prioritized


def _calculate_confidence(attribution: dict, priority: int) -> float:
    """
    Calculate confidence score based on data completeness and source priority.
    """
    score = 0.0
    
    # Source priority contributes 40%
    score += (priority / 10) * 0.4
    
    # Photographer found contributes 30%
    if attribution.get("photographer"):
        score += 0.3
    
    # License info contributes 15%
    if attribution.get("license"):
        score += 0.15
    
    # Title found contributes 15%
    if attribution.get("title"):
        score += 0.15
    
    return min(score, 1.0)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
