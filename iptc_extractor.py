"""IPTC/EXIF Metadata Extractor - checks embedded metadata FIRST before reverse search"""

import logging
import aiohttp
from typing import Optional
from io import BytesIO
from PIL import Image
from PIL.ExifTags import TAGS
import iptcinfo3

logger = logging.getLogger(__name__)


async def extract_iptc_metadata(image_url: str = None, image_bytes: bytes = None) -> Optional[dict]:
    """
    Extract IPTC/EXIF metadata from an image BEFORE doing reverse search.
    This is fast and checks embedded creator/copyright info first.
    
    Returns dict with:
    - creator
    - copyright
    - title
    - description
    - keywords
    - date_created
    - location
    """
    try:
        # Get image bytes if URL provided
        if image_url and not image_bytes:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
                async with session.get(image_url) as resp:
                    if resp.status == 200:
                        image_bytes = await resp.read()
                    else:
                        logger.warning(f"Failed to fetch image: HTTP {resp.status}")
                        return None
        
        if not image_bytes:
            return None
        
        metadata = {}
        
        # Try IPTC first (most likely to have creator info)
        try:
            iptc = iptcinfo3.IPTCInfo(BytesIO(image_bytes))
            
            # Creator/Photographer
            if iptc.get('by-line'):
                creator = iptc['by-line'].decode('utf-8') if isinstance(iptc['by-line'], bytes) else str(iptc['by-line'])
                metadata['creator'] = creator.strip()
            
            # Credit line
            if iptc.get('credit'):
                credit = iptc['credit'].decode('utf-8') if isinstance(iptc['credit'], bytes) else str(iptc['credit'])
                metadata['credit'] = credit.strip()
            
            # Copyright
            if iptc.get('copyright notice'):
                copyright_text = iptc['copyright notice'].decode('utf-8') if isinstance(iptc['copyright notice'], bytes) else str(iptc['copyright notice'])
                metadata['copyright'] = copyright_text.strip()
            
            # Title
            if iptc.get('object name'):
                title = iptc['object name'].decode('utf-8') if isinstance(iptc['object name'], bytes) else str(iptc['object name'])
                metadata['title'] = title.strip()
            
            # Description/Caption
            if iptc.get('caption/abstract'):
                desc = iptc['caption/abstract'].decode('utf-8') if isinstance(iptc['caption/abstract'], bytes) else str(iptc['caption/abstract'])
                metadata['description'] = desc.strip()
            
            # Keywords
            if iptc.get('keywords'):
                kw = iptc['keywords']
                if isinstance(kw, list):
                    keywords = [k.decode('utf-8') if isinstance(k, bytes) else str(k) for k in kw]
                    metadata['keywords'] = [k.strip() for k in keywords if k.strip()]
                else:
                    kw_str = kw.decode('utf-8') if isinstance(kw, bytes) else str(kw)
                    metadata['keywords'] = [k.strip() for k in kw_str.split(',') if k.strip()]
            
            # Date
            if iptc.get('date created'):
                date_created = iptc['date created']
                if isinstance(date_created, bytes):
                    date_created = date_created.decode('utf-8')
                metadata['date_created'] = str(date_created)
            
            # Location
            if iptc.get('city') or iptc.get('province/state') or iptc.get('country/primary location name'):
                location_parts = []
                for key in ['city', 'province/state', 'country/primary location name']:
                    if iptc.get(key):
                        val = iptc[key].decode('utf-8') if isinstance(iptc[key], bytes) else str(iptc[key])
                        location_parts.append(val.strip())
                metadata['location'] = ', '.join(location_parts)
            
            logger.info(f"IPTC metadata found: {list(metadata.keys())}")
        
        except Exception as e:
            logger.debug(f"No IPTC data or error: {e}")
        
        # Try EXIF as fallback/supplement
        try:
            img = Image.open(BytesIO(image_bytes))
            exif_data = img.getexif()
            
            if exif_data:
                # Artist (photographer)
                if 315 in exif_data and not metadata.get('creator'):
                    metadata['creator'] = str(exif_data[315]).strip()
                
                # Copyright
                if 33432 in exif_data and not metadata.get('copyright'):
                    metadata['copyright'] = str(exif_data[33432]).strip()
                
                # Image Description
                if 270 in exif_data and not metadata.get('description'):
                    metadata['description'] = str(exif_data[270]).strip()
                
                # Date Taken
                if 36867 in exif_data and not metadata.get('date_created'):
                    date_str = str(exif_data[36867])
                    # Format: "YYYY:MM:DD HH:MM:SS" -> "YYYY-MM-DD"
                    if ':' in date_str:
                        date_str = date_str.split()[0].replace(':', '-')
                    metadata['date_created'] = date_str
                
                logger.info(f"EXIF metadata found: {list(metadata.keys())}")
        
        except Exception as e:
            logger.debug(f"No EXIF data or error: {e}")
        
        # Return metadata if we found creator or copyright
        if metadata.get('creator') or metadata.get('copyright'):
            logger.info(f"Found embedded metadata - creator: {metadata.get('creator')}, copyright: {metadata.get('copyright')}")
            return metadata
        
        logger.info("No embedded creator metadata found")
        return None
    
    except Exception as e:
        logger.error(f"Error extracting IPTC/EXIF: {e}")
        return None
