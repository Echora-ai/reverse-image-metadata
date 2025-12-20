# Reverse Image Attribution Service

A Python service that extracts photographer attribution from stock photo sites. **Scraping only - no API calls, no rate limits.**

## Two Modes

### 1. Direct URL Lookup (Recommended)

If you already have a URL from Pexels, Pixabay, Getty, etc., use this endpoint. **No reverse search needed.**

### 2. Reverse Image Search

If you have an image and need to find where it came from, use reverse search.

## Features

- **Direct Scraping**: No API keys needed, no rate limits
- **10 Sources Supported**: Getty, Shutterstock, Unsplash, Pexels, Pixabay, Flickr, Alamy, AP, Reuters, NYT
- **Extracts**: Photographer name, license, title, location
- **Ready for Cloud Run**: Dockerfile included

---

## API Usage

### POST /get-attribution (Direct URL Lookup)

**Best for**: When you already have a stock photo URL.

**Request:**
```json
{
  "url": "https://www.pexels.com/photo/green-hill-near-body-of-water-462162/"
}
```

**Response:**
```json
{
  "found": true,
  "url": "https://www.pexels.com/photo/green-hill-near-body-of-water-462162/",
  "attribution": {
    "source": "pexels",
    "source_url": "https://www.pexels.com/photo/green-hill-near-body-of-water-462162/",
    "photographer": "Pixabay",
    "photographer_url": "https://www.pexels.com/@pixabay",
    "license": "CC0 (Public Domain)",
    "title": "Green Hill Near Body of Water",
    "location": "United Kingdom",
    "confidence": 1.0
  }
}
```

**Pixabay Example:**
```json
{
  "url": "https://pixabay.com/photos/beach-sea-sunset-sun-sunlight-1751455/"
}
```

**Response:**
```json
{
  "found": true,
  "url": "https://pixabay.com/photos/beach-sea-sunset-sun-sunlight-1751455/",
  "attribution": {
    "source": "pixabay",
    "photographer": "12019",
    "photographer_url": "https://pixabay.com/users/12019/",
    "license": "Pixabay License",
    "title": "Beach Sea Sunset Sun Sunlight",
    "confidence": 1.0
  }
}
```

### POST /reverse-search

**Best for**: When you have an image URL and need to find the source.

**Request:**
```json
{
  "image_url": "https://example.com/unknown-photo.jpg",
  "max_results": 10,
  "timeout": 30
}
```

**Response:**
```json
{
  "found": true,
  "image_url": "https://example.com/unknown-photo.jpg",
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

---

## Supported Sources

| Source | Scraping | Fields Extracted |
|--------|----------|------------------|
| **Pexels** | ✅ Page only | photographer, title, license (CC0/Pexels), location |
| **Pixabay** | ✅ Page only | photographer, title, license (Pixabay License) |
| **Unsplash** | ✅ Page only | photographer, title, license |
| **Getty Images** | ✅ Page only | photographer, title, license |
| **Shutterstock** | ✅ Page only | photographer, title, license |
| **Flickr** | ✅ Page only | photographer, title, license |
| **Alamy** | ✅ Page only | photographer, title, license |
| **AP Images** | ✅ Page only | photographer, title |
| **Reuters** | ✅ Page only | photographer, title |
| **NYT** | ✅ Page only | photographer, title |

**No API keys required.** We scrape pages directly.

---

## Local Development

```bash
# Install dependencies
pip install -r requirements.txt

# Run the server
uvicorn main:app --reload --port 8080
```

## Docker

```bash
# Build
docker build -t reverse-image-attribution .

# Run
docker run -p 8080:8080 reverse-image-attribution
```

## Deploy to Cloud Run

```bash
# Build and push
gcloud builds submit --tag gcr.io/YOUR_PROJECT/reverse-image-attribution

# Deploy
gcloud run deploy reverse-image-attribution \
  --image gcr.io/YOUR_PROJECT/reverse-image-attribution \
  --platform managed \
  --region us-central1 \
  --allow-unauthenticated
```

---

## Integration with Xano

### Direct URL Lookup (Recommended)

```javascript
// In Xano function stack
// Use when you already have a Pexels/Pixabay/etc URL

var response = external_api_request({
  url: "https://your-cloudrun-url/get-attribution",
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: { url: input.image_url }
});

if (response.found) {
  var photographer = response.attribution.photographer;
  var license = response.attribution.license;
  var title = response.attribution.title;
}
```

### Reverse Search

```javascript
// In Xano function stack
// Use when you need to find where an image came from

var response = external_api_request({
  url: "https://your-cloudrun-url/reverse-search",
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: { image_url: input.asset_url }
});

if (response.found && response.results.length > 0) {
  var best_match = response.results[0];
  var photographer = best_match.photographer;
  var source_url = best_match.source_url;
}
```

---

## Rate Limits

**None!** We scrape pages directly, no APIs.

Just be reasonable:
- Built-in 0.5s delay between scrapes
- Don't hammer sites with 1000 requests/second
- That's it

## Optional: Improve Reverse Search

For better reverse search results, you can optionally add:

| Variable | Purpose | Free Tier |
|----------|---------|-----------|
| `SERPAPI_KEY` | Google Lens reverse search | 100 searches/month |

Without SerpAPI, we use Yandex and Bing (free, but less accurate).
