# Reverse Image Attribution Service

A Python service that performs reverse image search and extracts photographer attribution from known stock photo sites.

## Features

- **Reverse Image Search**: Uses Google Lens (via SerpAPI), Yandex, and Bing to find image sources
- **Attribution Extraction**: Scrapes photographer credits from:
  - Getty Images
  - Shutterstock
  - Unsplash (with free API support)
  - Flickr (with free API support)
  - Alamy
  - News sites (AP, Reuters, NYT)
- **Confidence Scoring**: Ranks results by source reliability and data completeness
- **Rate Limiting**: Built-in delays to avoid getting blocked

## API Usage

### POST /reverse-search

Find photographer attribution for an image.

**Request:**
```json
{
  "image_url": "https://example.com/photo.jpg",
  "max_results": 10,
  "timeout": 30
}
```

**Response:**
```json
{
  "found": true,
  "image_url": "https://example.com/photo.jpg",
  "results": [
    {
      "source": "getty",
      "source_url": "https://gettyimages.com/detail/1234567890",
      "photographer": "John Smith",
      "license": "Rights Managed",
      "title": "Sunset over mountains",
      "confidence": 0.95
    }
  ],
  "search_engines_used": ["google_lens", "yandex", "bing"]
}
```

### GET /health

Health check endpoint for Cloud Run.

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `SERPAPI_KEY` | No | SerpAPI key for Google Lens (improves results) |
| `UNSPLASH_ACCESS_KEY` | No | Unsplash API key for better Unsplash extraction |
| `FLICKR_API_KEY` | No | Flickr API key for better Flickr extraction |

## Local Development

```bash
# Install dependencies
pip install -r requirements.txt

# Run the server
uvicorn main:app --reload --port 8080

# Or with optional API keys
SERPAPI_KEY=xxx uvicorn main:app --reload --port 8080
```

## Docker

```bash
# Build
docker build -t reverse-image-attribution .

# Run
docker run -p 8080:8080 reverse-image-attribution

# With API keys
docker run -p 8080:8080 \
  -e SERPAPI_KEY=xxx \
  -e UNSPLASH_ACCESS_KEY=xxx \
  reverse-image-attribution
```

## Deploy to Cloud Run

```bash
# Build and push to Google Container Registry
gcloud builds submit --tag gcr.io/YOUR_PROJECT/reverse-image-attribution

# Deploy to Cloud Run
gcloud run deploy reverse-image-attribution \
  --image gcr.io/YOUR_PROJECT/reverse-image-attribution \
  --platform managed \
  --region us-central1 \
  --allow-unauthenticated
```

## Architecture

```
Image URL
    ↓
Reverse Search (Google Lens / Yandex / Bing)
    ↓
Filter for known stock/news sites
    ↓
Scrape attribution from matched sites
    ↓
Return structured results with confidence scores
```

## Supported Sources

| Source | Method | Attribution Fields |
|--------|--------|--------------------|
| Getty Images | Scraping | photographer, license, title |
| Shutterstock | Scraping | photographer, license, title |
| Unsplash | API + Scraping | photographer, license, title |
| Flickr | API + Scraping | photographer, license, title |
| Alamy | Scraping | photographer, license, title |
| AP Images | Scraping | photographer, title |
| Reuters | Scraping | photographer, title |
| NYT | Scraping | photographer, title |

## Rate Limits

- Built-in 0.5s delay between scrapes
- Yandex/Bing free tier: ~1-2 requests/second
- SerpAPI: depends on your plan (100 free searches/month)
- Unsplash API: 50 requests/hour (demo), unlimited (production)
- Flickr API: 3600 requests/hour

## Integration with Xano

Call this service from a Xano task:

```javascript
// In Xano function stack
var response = external_api_request({
  url: "https://your-cloudrun-url/reverse-search",
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: { image_url: input.asset_url }
});

if (response.found) {
  // Update asset with attribution
  var photographer = response.results[0].photographer;
  var source_url = response.results[0].source_url;
}
```
