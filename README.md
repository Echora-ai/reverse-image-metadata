# Reverse Image Metadata Service

Fast, scalable reverse image attribution service that extracts photographer credits and metadata from images.

## Features

### ✅ **IPTC Metadata Extraction (FAST - First Priority)**
- **Checks embedded IPTC/EXIF metadata FIRST** (< 1 second)
- Extracts: Creator, Copyright, Title, Description, Keywords, Date, Location
- **If metadata found → Returns immediately** (no reverse search needed)
- Works with professional stock photos that have embedded credits

### ✅ **Reverse Image Search (Fallback)**
Only runs if IPTC metadata is missing or incomplete:
- Multi-engine support: Google Lens, Yandex, Bing
- Finds where image appears online
- Scrapes photographer credits from web pages

### ✅ **Smart Scraping**
- Prioritizes known stock sites (Getty, Shutterstock, Unsplash, Pexels, Pixabay, Flickr)
- Extracts structured metadata from each source
- Confidence scoring

### ✅ **Multiple Input Methods**
- URL-based search
- File upload
- Batch processing

## How It Works

```
1. Image URL/File received
   ↓
2. Check embedded IPTC/EXIF metadata (< 1s)
   ├─ Creator found? → Return immediately ✅
   └─ No metadata → Continue to step 3
   ↓
3. Reverse Image Search (Google/Yandex/Bing)
   ↓
4. Scrape top results for photographer credits
   ↓
5. Return structured metadata
```

## API Endpoints

### `POST /reverse-search`
Reverse search by image URL

**Request:**
```json
{
  "image_url": "https://example.com/photo.jpg",
  "max_results": 5,
  "timeout": 30,
  "engines": ["google", "yandex", "bing"]
}
```

**Response:**
```json
{
  "found": true,
  "image_url": "https://example.com/photo.jpg",
  "results": [
    {
      "type": "image",
      "id": "img_abc123_iptc",
      "creator": "John Doe",
      "copyright": "© 2024 John Doe",
      "title": "Sunset Over Mountains",
      "description": "Beautiful sunset...",
      "keywords": ["sunset", "mountains", "nature"],
      "date_created": "2024-03-15",
      "location": "Colorado, USA",
      "license": "Creative Commons BY 4.0",
      "source_url": "https://example.com/photo.jpg",
      "source_domain": "iptc_embedded",
      "confidence": 1.0
    }
  ],
  "search_engines_used": ["iptc_embedded"],
  "total_matches_found": 1
}
```

### `POST /reverse-search/upload`
Reverse search by uploading a file

**Form Data:**
- `file`: Image file (max 10MB)
- `max_results`: Number of results (default: 10)
- `timeout`: Timeout in seconds (default: 30)
- `engines`: Comma-separated list (default: "google,yandex,bing")

### `POST /reverse-search/batch`
Batch process multiple images (max 50)

## Installation

```bash
pip install -r requirements.txt
```

**Dependencies:**
- `fastapi` - API framework
- `aiohttp` - Async HTTP client
- `beautifulsoup4` - HTML parsing
- `Pillow` - Image processing
- `iptcinfo3` - IPTC metadata extraction

## Running Locally

```bash
python reverse_image_service.py
```

Service runs on `http://localhost:8080`

## Deployment

### Google Cloud Run
```bash
gcloud run deploy reverse-image-metadata \
  --source . \
  --region us-central1 \
  --allow-unauthenticated
```

## Performance

| Scenario | Time |
|----------|------|
| IPTC metadata found | < 1 second |
| Reverse search (no IPTC) | 5-15 seconds |
| Batch (10 images with IPTC) | < 10 seconds |
| Batch (10 images, reverse search) | 60-120 seconds |

## Supported Image Formats

- JPEG/JPG (best for IPTC)
- PNG
- WebP
- GIF
- TIFF

## Limitations

- **IPTC**: Only works if photographer embedded metadata in the file
- **Reverse Search**: Subject to rate limits from search engines
- **Scraping**: May break if stock sites change their HTML structure
- **Upload**: 10MB file size limit

## Tips for Best Results

1. **Professional photos** from Getty, Shutterstock, etc. usually have IPTC metadata
2. **Social media images** (Instagram, Facebook) typically strip metadata
3. **Stock photos** are more likely to return results than personal photos
4. **Higher resolution** images get better reverse search results

## API Response Fields

| Field | Description |
|-------|-------------|
| `creator` | Photographer/creator name |
| `creator_url` | Link to creator's profile |
| `copyright` | Copyright notice |
| `title` | Image title |
| `description` | Image description/caption |
| `keywords` | Tags/keywords |
| `date_created` | When photo was taken/created |
| `location` | Where photo was taken |
| `license` | Usage license |
| `source_url` | Where we found the info |
| `source_domain` | Site name (e.g. "iptc_embedded", "unsplash", "pexels") |
| `confidence` | 0.0-1.0 score (1.0 = IPTC embedded) |

## Version

**v2.1.0** - IPTC-first extraction strategy

## License

MIT
