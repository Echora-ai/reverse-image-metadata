# Reverse Image Attribution Service

A TinEye-like reverse image search service for finding image sources and metadata at scale.

## Features

- **Multiple search engines**: Google Lens, Yandex, Bing Visual Search
- **File upload support**: Search by uploading images directly
- **URL-based search**: Search using image URLs
- **Batch processing**: Process multiple images in one request
- **Standardized metadata**: Returns consistent metadata format across all sources
- **Confidence scoring**: Ranks results by reliability

## Response Format

Each result returns metadata in this standardized format:

```json
{
  "type": "image",
  "id": "img_a1b2c3d4",
  "title": "Sunset at Malibu",
  "filename": "sunset_malibu.jpg",
  "creator": "Jane Doe",
  "creator_url": "https://unsplash.com/@janedoe",
  "date_created": "2024-08-15",
  "description": "A beautiful sunset over the Pacific Ocean at Malibu Beach",
  "keywords": ["sunset", "beach", "malibu", "ocean", "california"],
  "location": "Malibu, California",
  "copyright": "© 2024 Jane Doe",
  "license": "Unsplash License",
  "source_url": "https://unsplash.com/photos/abc123",
  "source_domain": "unsplash",
  "confidence": 0.85
}
```

All fields are nullable except `type` (always "image") and `keywords` (empty array if none).

## Quick Start

```bash
# Install dependencies
pip install fastapi uvicorn aiohttp beautifulsoup4 pydantic

# Run the server
python reverse_image_service.py

# Test it
curl -X POST http://localhost:8080/reverse-search \
  -H "Content-Type: application/json" \
  -d '{"image_url": "https://images.unsplash.com/photo-1506905925346-21bda4d32df4"}'
```

## API Endpoints

### `POST /reverse-search`
Search using an image URL.

```json
{
  "image_url": "https://example.com/image.jpg",
  "max_results": 10,
  "timeout": 30,
  "engines": ["google", "yandex", "bing"]
}
```

### `POST /reverse-search/upload`
Search by uploading an image file (multipart form data).

### `POST /reverse-search/batch`
Process multiple images at once (max 50).

---

## Important Limitations & Considerations

### 1. Search Engine Scraping is Fragile

The search engines (Google, Yandex, Bing) don't have official reverse image search APIs. This service scrapes their HTML responses, which means:

- **They can break at any time** when the search engine updates their HTML structure
- **Rate limiting** - you'll get blocked if you search too frequently
- **CAPTCHAs** - heavy usage will trigger bot detection
- **Legal gray area** - check ToS for each service

### 2. Google Lens Challenges

Google Lens is particularly tricky:
- No official API
- Heavy JavaScript rendering (the simple HTTP approach may not work reliably)
- For production, consider using Playwright/Selenium for browser automation

### 3. Alternatives to Consider

| Service | Pros | Cons |
|---------|------|------|
| **TinEye API** | Official API, reliable | $200/mo for 5000 searches |
| **Google Cloud Vision** | Official, includes labels | Doesn't find source URLs |
| **SerpAPI** | Reliable scraping | $50/mo+ |
| **Bing Visual Search API** | Official Microsoft API | Limited free tier |

---

## Scaling for Production

### Level 1: Basic (100s of searches/day)

What you have now. Add:
- Redis caching for repeat searches
- Rate limiting per IP
- Basic retry logic

### Level 2: Medium (1000s of searches/day)

- **Proxy rotation** - Essential to avoid IP blocks
- **Queue-based processing** - Redis Queue or Celery
- **Multiple workers** - Horizontal scaling

### Level 3: High Scale (10,000s+ searches/day)

At this scale, scraping becomes expensive and unreliable. Consider:

1. **Hybrid approach**: Use official APIs where available + scraping for gaps
2. **Build your own index**: 
   - Crawl and index stock photo sites directly
   - Use perceptual hashing (pHash, dHash) for matching
   - Store in a vector database like Milvus or Pinecone

---

## Cost Comparison

| Approach | Monthly Cost @ 10k searches | Reliability |
|----------|----------------------------|-------------|
| TinEye API | ~$400 | ★★★★★ |
| SerpAPI | ~$100 | ★★★★☆ |
| This service (self-hosted) | $50-100 (proxies) | ★★★☆☆ |
| Build your own index | $200+ (infra) | ★★★★☆ |

---

## Files

- `reverse_image_service.py` - Main FastAPI service
- `example_client.py` - Example usage

## License

MIT
