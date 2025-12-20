"""Reverse Image Search Module

Supports multiple search engines:
- Google Lens (via SerpAPI or direct scraping)
- Yandex Images
- Bing Visual Search
"""

import asyncio
import aiohttp
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import quote_plus, urlencode
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)


@dataclass
class SearchResult:
    """Results from reverse image search"""
    urls: list[str] = field(default_factory=list)
    engines_used: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


class ReverseImageSearch:
    """
    Performs reverse image search across multiple engines.
    
    Supports:
    - SerpAPI (Google Lens) - if API key is set
    - Yandex Images - free, scraping-based
    - Bing Visual Search - free, scraping-based
    """
    
    def __init__(self):
        self.serpapi_key = os.environ.get("SERPAPI_KEY")
        self.user_agent = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        )
        self.timeout = aiohttp.ClientTimeout(total=30)
    
    async def search(
        self,
        image_url: str,
        max_results: int = 10,
        timeout: int = 30
    ) -> SearchResult:
        """
        Perform reverse image search across multiple engines.
        
        Args:
            image_url: URL of the image to search
            max_results: Maximum number of result URLs to return
            timeout: Request timeout in seconds
            
        Returns:
            SearchResult with URLs and metadata
        """
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        result = SearchResult()
        
        # Run searches in parallel
        tasks = []
        
        # Always try Yandex (free)
        tasks.append(self._search_yandex(image_url))
        
        # Try SerpAPI if key is available
        if self.serpapi_key:
            tasks.append(self._search_serpapi(image_url))
        
        # Try Bing (free)
        tasks.append(self._search_bing(image_url))
        
        # Gather results
        search_results = await asyncio.gather(*tasks, return_exceptions=True)
        
        seen_urls = set()
        for search_result in search_results:
            if isinstance(search_result, Exception):
                result.errors.append(str(search_result))
                continue
            
            engine_name, urls = search_result
            result.engines_used.append(engine_name)
            
            for url in urls:
                if url not in seen_urls and len(result.urls) < max_results:
                    seen_urls.add(url)
                    result.urls.append(url)
        
        logger.info(f"Found {len(result.urls)} URLs from {result.engines_used}")
        return result
    
    async def _search_serpapi(self, image_url: str) -> tuple[str, list[str]]:
        """
        Search using SerpAPI's Google Lens endpoint.
        Requires SERPAPI_KEY environment variable.
        """
        if not self.serpapi_key:
            return ("serpapi", [])
        
        params = {
            "engine": "google_lens",
            "url": image_url,
            "api_key": self.serpapi_key,
        }
        
        url = f"https://serpapi.com/search?{urlencode(params)}"
        
        async with aiohttp.ClientSession(timeout=self.timeout) as session:
            async with session.get(url) as response:
                if response.status != 200:
                    raise Exception(f"SerpAPI error: {response.status}")
                
                data = await response.json()
                urls = []
                
                # Extract visual matches
                visual_matches = data.get("visual_matches", [])
                for match in visual_matches:
                    if "link" in match:
                        urls.append(match["link"])
                
                # Extract knowledge graph if present
                knowledge_graph = data.get("knowledge_graph", {})
                if "source" in knowledge_graph:
                    source = knowledge_graph["source"]
                    if "link" in source:
                        urls.insert(0, source["link"])  # Prioritize
                
                return ("google_lens", urls)
    
    async def _search_yandex(self, image_url: str) -> tuple[str, list[str]]:
        """
        Search using Yandex Images reverse search.
        Free, no API key required.
        """
        encoded_url = quote_plus(image_url)
        search_url = f"https://yandex.com/images/search?rpt=imageview&url={encoded_url}"
        
        headers = {
            "User-Agent": self.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
        }
        
        async with aiohttp.ClientSession(timeout=self.timeout) as session:
            async with session.get(search_url, headers=headers) as response:
                if response.status != 200:
                    raise Exception(f"Yandex error: {response.status}")
                
                html = await response.text()
                soup = BeautifulSoup(html, "html.parser")
                urls = []
                
                # Look for "Sites with this image" section
                # Yandex uses data-bem attributes for structured data
                for link in soup.find_all("a", href=True):
                    href = link["href"]
                    # Filter for external links (not Yandex internal)
                    if (
                        href.startswith("http")
                        and "yandex" not in href.lower()
                        and not href.endswith((".jpg", ".jpeg", ".png", ".gif", ".webp"))
                    ):
                        # Skip search result artifacts
                        if "url=" not in href and "redirect" not in href.lower():
                            urls.append(href)
                
                # Also check for cbir-similar links
                for item in soup.find_all(class_=re.compile(r"CbirSimilar|other-sites")):
                    for link in item.find_all("a", href=True):
                        href = link["href"]
                        if href.startswith("http") and "yandex" not in href.lower():
                            urls.append(href)
                
                # Deduplicate while preserving order
                seen = set()
                unique_urls = []
                for url in urls:
                    if url not in seen:
                        seen.add(url)
                        unique_urls.append(url)
                
                return ("yandex", unique_urls[:20])
    
    async def _search_bing(self, image_url: str) -> tuple[str, list[str]]:
        """
        Search using Bing Visual Search.
        Free, no API key required.
        """
        encoded_url = quote_plus(image_url)
        search_url = f"https://www.bing.com/images/search?view=detailv2&iss=sbi&q=imgurl:{encoded_url}"
        
        headers = {
            "User-Agent": self.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
        }
        
        async with aiohttp.ClientSession(timeout=self.timeout) as session:
            async with session.get(search_url, headers=headers, allow_redirects=True) as response:
                if response.status != 200:
                    raise Exception(f"Bing error: {response.status}")
                
                html = await response.text()
                soup = BeautifulSoup(html, "html.parser")
                urls = []
                
                # Bing shows "Pages that include this image"
                for link in soup.find_all("a", href=True):
                    href = link["href"]
                    if (
                        href.startswith("http")
                        and "bing.com" not in href.lower()
                        and "microsoft.com" not in href.lower()
                        and not href.endswith((".jpg", ".jpeg", ".png", ".gif", ".webp"))
                    ):
                        urls.append(href)
                
                # Deduplicate
                seen = set()
                unique_urls = []
                for url in urls:
                    if url not in seen:
                        seen.add(url)
                        unique_urls.append(url)
                
                return ("bing", unique_urls[:20])
