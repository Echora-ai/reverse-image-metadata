"""Example client for the Reverse Image Attribution Service"""

import asyncio
import aiohttp
import sys
from pathlib import Path


async def search_by_url(api_base: str, image_url: str):
    """Search using an image URL."""
    async with aiohttp.ClientSession() as session:
        payload = {
            "image_url": image_url,
            "max_results": 10,
            "timeout": 30,
            "engines": ["google", "yandex", "bing"]
        }
        
        async with session.post(f"{api_base}/reverse-search", json=payload) as resp:
            result = await resp.json()
            return result


async def search_by_file(api_base: str, file_path: str):
    """Search by uploading a file."""
    async with aiohttp.ClientSession() as session:
        with open(file_path, "rb") as f:
            form = aiohttp.FormData()
            form.add_field("file", f, filename=Path(file_path).name)
            form.add_field("max_results", "10")
            form.add_field("engines", "google,yandex,bing")
            
            async with session.post(f"{api_base}/reverse-search/upload", data=form) as resp:
                result = await resp.json()
                return result


async def batch_search(api_base: str, image_urls: list[str]):
    """Search multiple images in batch."""
    async with aiohttp.ClientSession() as session:
        payload = {
            "image_urls": image_urls,
            "max_results_per_image": 5,
            "timeout_per_image": 20
        }
        
        async with session.post(f"{api_base}/reverse-search/batch", json=payload) as resp:
            result = await resp.json()
            return result


def print_results(result: dict):
    """Pretty print search results."""
    print(f"\n{'='*60}")
    print(f"Image: {result.get('image_url', 'N/A')}")
    print(f"Found: {result.get('found', False)}")
    print(f"Engines used: {', '.join(result.get('search_engines_used', []))}")
    print(f"Total matches: {result.get('total_matches_found', 0)}")
    
    if result.get('error'):
        print(f"Errors: {result['error']}")
    
    for i, meta in enumerate(result.get('results', []), 1):
        print(f"\n--- Result {i} (confidence: {meta.get('confidence', 0):.2f}) ---")
        print(f"  Type: {meta.get('type', 'image')}")
        print(f"  ID: {meta.get('id')}")
        print(f"  Title: {meta.get('title')}")
        print(f"  Filename: {meta.get('filename')}")
        print(f"  Creator: {meta.get('creator')}")
        if meta.get('creator_url'):
            print(f"  Creator URL: {meta['creator_url']}")
        print(f"  Date Created: {meta.get('date_created')}")
        print(f"  Description: {meta.get('description')[:100] + '...' if meta.get('description') and len(meta.get('description', '')) > 100 else meta.get('description')}")
        print(f"  Keywords: {meta.get('keywords', [])[:5]}{'...' if len(meta.get('keywords', [])) > 5 else ''}")
        print(f"  Location: {meta.get('location')}")
        print(f"  Copyright: {meta.get('copyright')}")
        print(f"  License: {meta.get('license')}")
        print(f"  Source URL: {meta.get('source_url')}")
        print(f"  Source Domain: {meta.get('source_domain')}")


async def main():
    API_BASE = "http://localhost:8080"
    
    # Example 1: Search by URL
    print("\n" + "="*60)
    print("EXAMPLE 1: Search by URL")
    print("="*60)
    
    test_url = "https://images.unsplash.com/photo-1506905925346-21bda4d32df4"
    result = await search_by_url(API_BASE, test_url)
    print_results(result)
    
    # Example 2: Search by file upload
    # print("\n" + "="*60)
    # print("EXAMPLE 2: Search by file upload")
    # print("="*60)
    # result = await search_by_file(API_BASE, "/path/to/your/image.jpg")
    # print_results(result)
    
    # Example 3: Batch search
    print("\n" + "="*60)
    print("EXAMPLE 3: Batch search")
    print("="*60)
    
    test_urls = [
        "https://images.pexels.com/photos/1287145/pexels-photo-1287145.jpeg",
        "https://images.unsplash.com/photo-1472214103451-9374bd1c798e",
    ]
    result = await batch_search(API_BASE, test_urls)
    print(f"Processed: {result.get('total_processed', 0)}")
    print(f"Found: {result.get('total_found', 0)}")
    for r in result.get('results', []):
        print_results(r)


if __name__ == "__main__":
    asyncio.run(main())
