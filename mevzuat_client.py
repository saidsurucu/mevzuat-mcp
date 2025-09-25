# mevzuat_client.py
"""
API Client for interacting with the Adalet Bakanlığı Mevzuat API (bedesten.adalet.gov.tr).
This client handles the business logic of making HTTP requests and parsing responses.
"""

import httpx
import logging
import base64
import io
import time
from bs4 import BeautifulSoup
from markitdown import MarkItDown
from typing import Dict, List, Optional, Any, NamedTuple
from mevzuat_models import (
    MevzuatSearchRequest, MevzuatSearchResult, MevzuatDocument, MevzuatTur,
    MevzuatArticleNode, MevzuatArticleContent
)
logger = logging.getLogger(__name__)

class CacheEntry(NamedTuple):
    """Cache entry with content and expiration time."""
    content: str
    expires_at: float

class MarkdownCache:
    """Simple in-memory cache for markdown content with TTL."""

    def __init__(self, default_ttl: int = 3600):  # 1 hour default
        self._cache: Dict[str, CacheEntry] = {}
        self._default_ttl = default_ttl

    def get(self, key: str) -> Optional[str]:
        """Get cached content if not expired."""
        if key not in self._cache:
            return None

        entry = self._cache[key]
        if time.time() > entry.expires_at:
            del self._cache[key]
            return None

        return entry.content

    def put(self, key: str, content: str, ttl: Optional[int] = None) -> None:
        """Store content in cache with TTL."""
        ttl = ttl or self._default_ttl
        expires_at = time.time() + ttl
        self._cache[key] = CacheEntry(content=content, expires_at=expires_at)

    def clear(self) -> None:
        """Clear all cached content."""
        self._cache.clear()

    def size(self) -> int:
        """Get current cache size."""
        return len(self._cache)

    def cleanup_expired(self) -> int:
        """Remove expired entries and return count of removed entries."""
        current_time = time.time()
        expired_keys = [key for key, entry in self._cache.items() if current_time > entry.expires_at]
        for key in expired_keys:
            del self._cache[key]
        return len(expired_keys)

logger = logging.getLogger(__name__)

class MevzuatApiClient:
    BASE_URL = "https://bedesten.adalet.gov.tr/mevzuat"
    HEADERS = {
        'Accept': '*/*',
        'Content-Type': 'application/json; charset=utf-8',
        'AdaletApplicationName': 'UyapMevzuat',
        'Origin': 'https://mevzuat.adalet.gov.tr',
        'Referer': 'https://mevzuat.adalet.gov.tr/',
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    }
    def __init__(self, timeout: float = 30.0, cache_ttl: int = 3600, enable_cache: bool = True):
        self._http_client = httpx.AsyncClient(headers=self.HEADERS, timeout=timeout, follow_redirects=True)
        self._md_converter = MarkItDown()
        self._cache = MarkdownCache(default_ttl=cache_ttl) if enable_cache else None
        self._cache_enabled = enable_cache

    async def close(self):
        await self._http_client.aclose()

    def get_cache_stats(self) -> Dict[str, Any]:
        """Get cache statistics for monitoring."""
        if not self._cache_enabled or not self._cache:
            return {"cache_enabled": False}

        return {
            "cache_enabled": True,
            "cache_size": self._cache.size(),
            "default_ttl": self._cache._default_ttl
        }

    def clear_cache(self) -> None:
        """Clear all cached content."""
        if self._cache_enabled and self._cache:
            self._cache.clear()
            logger.info("Cache cleared manually")

    def cleanup_expired_cache(self) -> int:
        """Clean up expired cache entries and return count of removed entries."""
        if not self._cache_enabled or not self._cache:
            return 0

        removed_count = self._cache.cleanup_expired()
        if removed_count > 0:
            logger.info(f"Removed {removed_count} expired cache entries")
        return removed_count

    def _html_from_base64(self, b64_string: str) -> str:
        try:
            decoded_bytes = base64.b64decode(b64_string)
            return decoded_bytes.decode('utf-8')
        except Exception: return ""

    def _markdown_from_html(self, html_content: str, cache_key: Optional[str] = None) -> str:
        if not html_content: return ""

        # Check cache first if enabled and cache_key provided
        if self._cache_enabled and cache_key and self._cache:
            cached_result = self._cache.get(cache_key)
            if cached_result is not None:
                logger.debug(f"Cache hit for key: {cache_key[:50]}...")
                return cached_result

        try:
            html_bytes = html_content.encode('utf-8')
            html_io = io.BytesIO(html_bytes)
            conv_res = self._md_converter.convert(html_io)
            if conv_res and conv_res.text_content:
                markdown_result = conv_res.text_content.strip()
            else:
                markdown_result = ""
        except Exception:
            soup = BeautifulSoup(html_content, 'lxml')
            markdown_result = soup.get_text(separator='\n', strip=True)

        # Store in cache if enabled and cache_key provided
        if self._cache_enabled and cache_key and self._cache and markdown_result:
            self._cache.put(cache_key, markdown_result)
            logger.debug(f"Cached result for key: {cache_key[:50]}...")

        return markdown_result

    async def search_documents(self, request: MevzuatSearchRequest) -> MevzuatSearchResult:
        """Performs a detailed search for legislation documents."""
        payload = {
            "data": {
                "pageSize": request.page_size,
                "pageNumber": request.page_number,
                "mevzuatTurList": request.mevzuat_tur_list,
                "sortFields": [request.sort_field],
                "sortDirection": request.sort_direction,
            },
            "applicationName": "UyapMevzuat",
            "paging": True
        }
        
        if request.mevzuat_adi:
            payload["data"]["mevzuatAdi"] = request.mevzuat_adi
        if request.phrase:
            payload["data"]["phrase"] = request.phrase
        if request.mevzuat_no:
            payload["data"]["mevzuatNo"] = request.mevzuat_no
        if request.resmi_gazete_sayisi:
            payload["data"]["resmiGazeteSayi"] = request.resmi_gazete_sayisi
            
        try:
            response = await self._http_client.post(f"{self.BASE_URL}/searchDocuments", json=payload)
            response.raise_for_status()
            data = response.json()
            if data.get("metadata", {}).get("FMTY") != "SUCCESS":
                error_msg = data.get("metadata", {}).get("FMTE", "Unknown API error")
                return MevzuatSearchResult(documents=[], total_results=0, current_page=request.page_number, page_size=request.page_size, total_pages=0, query_used=request.model_dump(), error_message=error_msg)
            result_data = data.get("data", {})
            total_results = result_data.get("total", 0)
            return MevzuatSearchResult(
                documents=[MevzuatDocument.model_validate(doc) for doc in result_data.get("mevzuatList", [])],
                total_results=total_results, current_page=request.page_number, page_size=request.page_size,
                total_pages=(total_results + request.page_size - 1) // request.page_size if request.page_size > 0 else 0,
                query_used=request.model_dump()
            )
        except httpx.HTTPStatusError as e:
            return MevzuatSearchResult(documents=[], total_results=0, current_page=request.page_number, page_size=request.page_size, total_pages=0, query_used=request.model_dump(), error_message=f"API request failed: {e.response.status_code}")
        except Exception as e:
            return MevzuatSearchResult(documents=[], total_results=0, current_page=request.page_number, page_size=request.page_size, total_pages=0, query_used=request.model_dump(), error_message=f"An unexpected error occurred: {e}")

    async def get_article_tree(self, mevzuat_id: str) -> List[MevzuatArticleNode]:
        payload = { "data": {"mevzuatId": mevzuat_id}, "applicationName": "UyapMevzuat" }
        try:
            response = await self._http_client.post(f"{self.BASE_URL}/mevzuatMaddeTree", json=payload)
            response.raise_for_status()
            data = response.json()
            if data.get("metadata", {}).get("FMTY") != "SUCCESS": return []
            root_node = data.get("data", {})
            return [MevzuatArticleNode.model_validate(child) for child in root_node.get("children", [])]
        except Exception as e:
            logger.exception(f"Error fetching article tree for mevzuatId {mevzuat_id}")
            return []

    async def get_article_content(self, madde_id: str, mevzuat_id: str) -> MevzuatArticleContent:
        # Create cache key for article content
        cache_key = f"article:{madde_id}:{mevzuat_id}" if self._cache_enabled else None

        # Check if full result is cached
        if cache_key and self._cache:
            cached_content = self._cache.get(cache_key)
            if cached_content is not None:
                logger.debug(f"Full cache hit for article: {madde_id}")
                return MevzuatArticleContent(madde_id=madde_id, mevzuat_id=mevzuat_id, markdown_content=cached_content)

        payload = {"data": {"id": madde_id, "documentType": "MADDE"}, "applicationName": "UyapMevzuat"}
        try:
            response = await self._http_client.post(f"{self.BASE_URL}/getDocumentContent", json=payload)
            response.raise_for_status()
            data = response.json()
            if data.get("metadata", {}).get("FMTY") != "SUCCESS":
                return MevzuatArticleContent(madde_id=madde_id, mevzuat_id=mevzuat_id, markdown_content="", error_message=data.get("metadata", {}).get("FMTE", "Failed to retrieve content."))
            content_data = data.get("data", {})
            b64_content = content_data.get("content", "")
            html_content = self._html_from_base64(b64_content)

            # Use cache for HTML to markdown conversion
            html_cache_key = f"html_md:{hash(html_content)}" if html_content else None
            markdown_content = self._markdown_from_html(html_content, html_cache_key)

            # Cache the full result
            if cache_key and self._cache and markdown_content:
                self._cache.put(cache_key, markdown_content)
                logger.debug(f"Cached full result for article: {madde_id}")

            return MevzuatArticleContent(madde_id=madde_id, mevzuat_id=mevzuat_id, markdown_content=markdown_content)
        except Exception as e:
            logger.exception(f"Error fetching content for maddeId {madde_id}")
            return MevzuatArticleContent(madde_id=madde_id, mevzuat_id=mevzuat_id, markdown_content="", error_message=f"An unexpected error occurred: {e}")
    
    async def get_full_document_content(self, mevzuat_id: str) -> MevzuatArticleContent:
        """Retrieves the full content of a legislation document as a single unit."""
        # Create cache key for full document
        cache_key = f"full_doc:{mevzuat_id}" if self._cache_enabled else None

        # Check if full result is cached
        if cache_key and self._cache:
            cached_content = self._cache.get(cache_key)
            if cached_content is not None:
                logger.debug(f"Full cache hit for document: {mevzuat_id}")
                return MevzuatArticleContent(madde_id=mevzuat_id, mevzuat_id=mevzuat_id, markdown_content=cached_content)

        payload = {"data": {"id": mevzuat_id, "documentType": "MEVZUAT"}, "applicationName": "UyapMevzuat"}
        try:
            response = await self._http_client.post(f"{self.BASE_URL}/getDocumentContent", json=payload)
            response.raise_for_status()
            data = response.json()
            if data.get("metadata", {}).get("FMTY") != "SUCCESS":
                return MevzuatArticleContent(
                    madde_id=mevzuat_id, mevzuat_id=mevzuat_id,
                    markdown_content="",
                    error_message=data.get("metadata", {}).get("FMTE", "Failed to retrieve full document content.")
                )

            content_data = data.get("data", {})
            b64_content = content_data.get("content", "")

            # Handle PDF content - try to extract if it's a PDF
            if b64_content.startswith("JVBERi0"):  # PDF header in base64
                try:
                    import base64
                    pdf_bytes = base64.b64decode(b64_content)
                    # Create cache key for PDF processing
                    pdf_cache_key = f"pdf_md:{hash(b64_content)}" if self._cache_enabled else None

                    # Check PDF cache first
                    if pdf_cache_key and self._cache:
                        cached_pdf_md = self._cache.get(pdf_cache_key)
                        if cached_pdf_md is not None:
                            markdown_content = cached_pdf_md
                            logger.debug(f"PDF cache hit for document: {mevzuat_id}")
                        else:
                            # Use markitdown to convert PDF to markdown
                            from markitdown import MarkItDown
                            md = MarkItDown()
                            result = md.convert_stream(pdf_bytes, file_extension=".pdf")
                            markdown_content = result.text_content
                            # Cache the PDF result
                            if markdown_content:
                                self._cache.put(pdf_cache_key, markdown_content)
                                logger.debug(f"Cached PDF result for document: {mevzuat_id}")
                    else:
                        # No cache, process directly
                        from markitdown import MarkItDown
                        md = MarkItDown()
                        result = md.convert_stream(pdf_bytes, file_extension=".pdf")
                        markdown_content = result.text_content

                except Exception as pdf_error:
                    logger.warning(f"PDF extraction failed for {mevzuat_id}: {pdf_error}")
                    markdown_content = f"PDF content available but could not be extracted. Content length: {len(b64_content)} characters."
            else:
                # Handle HTML content with caching
                html_content = self._html_from_base64(b64_content)
                html_cache_key = f"html_md:{hash(html_content)}" if html_content else None
                markdown_content = self._markdown_from_html(html_content, html_cache_key)

            # Cache the full result
            if cache_key and self._cache and markdown_content:
                self._cache.put(cache_key, markdown_content)
                logger.debug(f"Cached full result for document: {mevzuat_id}")

            return MevzuatArticleContent(
                madde_id=mevzuat_id, mevzuat_id=mevzuat_id,
                markdown_content=markdown_content
            )
        except Exception as e:
            logger.exception(f"Error fetching full document content for mevzuatId {mevzuat_id}")
            return MevzuatArticleContent(
                madde_id=mevzuat_id, mevzuat_id=mevzuat_id,
                markdown_content="",
                error_message=f"An unexpected error occurred: {str(e)}"
            )