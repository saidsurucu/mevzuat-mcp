# mevzuat_client_new.py
"""
API Client for interacting with mevzuat.gov.tr directly.
This client handles search via DataTables API and content via PDF downloads.
"""

import httpx
import logging
import io
import time
import os
from pathlib import Path
from bs4 import BeautifulSoup
from markitdown import MarkItDown
from typing import Dict, Optional, Any, NamedTuple
from mevzuat_models import (
    MevzuatSearchRequestNew, MevzuatSearchResultNew, MevzuatDocumentNew,
    MevzuatArticleContent
)

# Load .env file if it exists
try:
    from dotenv import load_dotenv
    env_path = Path(__file__).parent / '.env'
    if env_path.exists():
        load_dotenv(dotenv_path=env_path)
except ImportError:
    pass  # python-dotenv not installed, will use os.environ only

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


class MevzuatApiClientNew:
    """Client for mevzuat.gov.tr - supports Kanun (laws) via DOC/PDF downloads."""

    BASE_URL = "https://www.mevzuat.gov.tr"
    SEARCH_ENDPOINT = f"{BASE_URL}/Anasayfa/MevzuatDatatable"
    DOC_URL_TEMPLATE = f"{BASE_URL}/MevzuatMetin/{{tur}}.{{tertip}}.{{no}}.doc"
    PDF_URL_TEMPLATE = f"{BASE_URL}/MevzuatMetin/{{tur}}.{{tertip}}.{{no}}.pdf"
    GENELGE_PDF_URL_TEMPLATE = f"{BASE_URL}/MevzuatMetin/CumhurbaskanligiGenelgeleri/{{date}}-{{no}}.pdf"

    # API expects different formats for mevzuat type in search API
    MEVZUAT_TUR_API_MAPPING = {
        "Kurum Yönetmeliği": "KurumVeKurulusYonetmeligi",
        "Cumhurbaşkanlığı Kararnamesi": "CumhurbaskaniKararnameleri",
        "Cumhurbaşkanı Kararı": "CumhurbaskaniKararlari",
        "CB Yönetmeliği": "CumhurbaskanligiVeBakanlarKuruluYonetmelik",
        "CB Genelgesi": "CumhurbaskanligiGenelgeleri",
        "Tebliğ": "Teblig",
        # Other types remain as-is
    }

    HEADERS = {
        'Accept': 'application/json, text/javascript, */*; q=0.01',
        'Accept-Language': 'tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7',
        'Content-Type': 'application/json; charset=UTF-8',
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36',
        'X-Requested-With': 'XMLHttpRequest',
    }

    def __init__(self, timeout: float = 30.0, cache_ttl: int = 3600, enable_cache: bool = True, mistral_api_key: Optional[str] = None):
        self._http_client = httpx.AsyncClient(
            headers=self.HEADERS,
            timeout=timeout,
            follow_redirects=True
        )
        self._md_converter = MarkItDown()
        self._cache = MarkdownCache(default_ttl=cache_ttl) if enable_cache else None
        self._cache_enabled = enable_cache
        self._antiforgery_token: Optional[str] = None
        self._cookies: Optional[Dict[str, str]] = None

        # Mistral OCR client (optional, for genelge PDFs with images)
        self._mistral_client = None
        self._mistral_api_key = mistral_api_key or os.environ.get("MISTRAL_API_KEY")
        if self._mistral_api_key:
            try:
                from mistralai import Mistral
                self._mistral_client = Mistral(api_key=self._mistral_api_key)
                logger.info("Mistral OCR client initialized")
            except ImportError:
                logger.warning("mistralai package not installed, OCR will not be available")
            except Exception as e:
                logger.warning(f"Failed to initialize Mistral OCR client: {e}")

    async def close(self):
        await self._http_client.aclose()

    @classmethod
    def _normalize_mevzuat_tur_for_api(cls, mevzuat_tur: str) -> str:
        """Normalize mevzuat type for API search requests."""
        return cls.MEVZUAT_TUR_API_MAPPING.get(mevzuat_tur, mevzuat_tur)

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

    def _markdown_from_html(self, html_content: str, cache_key: Optional[str] = None) -> str:
        """Convert HTML to markdown using markitdown."""
        if not html_content:
            return ""

        # Check cache
        if self._cache_enabled and cache_key and self._cache:
            cached_result = self._cache.get(cache_key)
            if cached_result is not None:
                logger.debug("Cache hit for HTML conversion")
                return cached_result

        try:
            html_bytes = html_content.encode('utf-8')
            html_io = io.BytesIO(html_bytes)
            conv_res = self._md_converter.convert(html_io)
            markdown_result = conv_res.text_content.strip() if conv_res and conv_res.text_content else ""
        except Exception:
            # Fallback: use BeautifulSoup for text extraction
            soup = BeautifulSoup(html_content, 'lxml')
            markdown_result = soup.get_text(separator='\n', strip=True)

        # Cache result
        if self._cache_enabled and cache_key and self._cache and markdown_result:
            self._cache.put(cache_key, markdown_result)

        return markdown_result

    async def _ocr_pdf_with_mistral(self, pdf_bytes: bytes, pdf_url: str) -> Optional[str]:
        """
        Use Mistral OCR to extract text from PDF (handles images + text).
        Downloads PDF and sends as base64 data URL (avoids authentication issues).
        Returns markdown content or None if OCR fails.
        """
        if not self._mistral_client:
            logger.warning("Mistral OCR client not initialized")
            return None

        try:
            import base64

            logger.info(f"Encoding PDF to base64 for Mistral OCR (size: {len(pdf_bytes)} bytes)")

            # Encode PDF bytes to base64
            base64_pdf = base64.b64encode(pdf_bytes).decode('utf-8')

            # Send as data URL to avoid authentication issues
            ocr_response = self._mistral_client.ocr.process(
                model="mistral-ocr-latest",
                document={
                    "type": "document_url",
                    "document_url": f"data:application/pdf;base64,{base64_pdf}"
                },
                include_image_base64=False  # We don't need image data back
            )

            # Extract text from OCR response
            # Mistral OCR returns pages array, each with markdown field
            if hasattr(ocr_response, 'pages') and ocr_response.pages:
                # Combine markdown from all pages
                markdown_parts = []
                for page in ocr_response.pages:
                    if hasattr(page, 'markdown') and page.markdown:
                        markdown_parts.append(page.markdown.strip())

                if markdown_parts:
                    markdown_content = "\n\n".join(markdown_parts)
                    logger.info(f"Mistral OCR successful: {len(ocr_response.pages)} pages, {len(markdown_content)} chars")
                    return markdown_content
                else:
                    logger.warning("Mistral OCR pages have no markdown content")
                    return None
            else:
                logger.warning("Mistral OCR response has no pages")
                return None

        except Exception as e:
            logger.error(f"Mistral OCR failed: {e}")
            return None

    def _ensure_playwright_browsers(self) -> None:
        """Ensure Playwright browsers are installed."""
        try:
            import subprocess
            import sys
            logger.info("Checking Playwright browser installation...")
            subprocess.check_call(
                [sys.executable, "-m", "playwright", "install", "chromium"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
            logger.info("Playwright browsers ready")
        except Exception as e:
            logger.warning(f"Could not ensure Playwright browsers: {e}")

    async def _ensure_session(self) -> None:
        """Ensure we have a valid session with antiforgery token and cookies using Playwright."""
        if self._antiforgery_token and self._cookies:
            return

        try:
            from playwright.async_api import async_playwright

            # Ensure browsers are installed
            self._ensure_playwright_browsers()

            logger.info("Getting session with Playwright")

            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                context = await browser.new_context()
                page = await context.new_page()

                # Visit main page
                await page.goto(f"{self.BASE_URL}/", wait_until="domcontentloaded", timeout=30000)

                # Get cookies from browser
                cookies = await context.cookies()
                self._cookies = {cookie['name']: cookie['value'] for cookie in cookies}

                # Get page content to extract antiforgery token
                html_content = await page.content()

                await browser.close()

            # Parse HTML for antiforgery token
            soup = BeautifulSoup(html_content, 'lxml')
            token_input = soup.find('input', {'name': '__RequestVerificationToken'})
            if token_input and token_input.get('value'):
                self._antiforgery_token = token_input['value']
                logger.info(f"Antiforgery token acquired: {self._antiforgery_token[:20]}...")
            else:
                # Try from cookies
                token_cookie = self._cookies.get('.AspNetCore.Antiforgery.Pk46jo02iDM')
                if token_cookie:
                    self._antiforgery_token = token_cookie
                    logger.info("Using antiforgery token from cookie")
                else:
                    logger.warning("Could not find antiforgery token")

            logger.info(f"Session established with {len(self._cookies)} cookies")

        except Exception as e:
            logger.error(f"Failed to establish session with Playwright: {e}")
            # Continue anyway

    async def search_documents_with_playwright(self, request: MevzuatSearchRequestNew) -> MevzuatSearchResultNew:
        """Search using Playwright - get cookies then make fetch request from page context."""
        from playwright.async_api import async_playwright

        try:
            # Ensure browsers are installed
            self._ensure_playwright_browsers()

            logger.info(f"Searching with Playwright fetch: {request.aranacak_ifade or request.mevzuat_no}")

            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                context = await browser.new_context()
                page = await context.new_page()

                # Visit main page to get cookies/session
                await page.goto(f"{self.BASE_URL}/", wait_until="domcontentloaded", timeout=30000)

                # Get antiforgery token from cookies
                cookies = await context.cookies()
                antiforgery_token = None
                for cookie in cookies:
                    if 'Antiforgery' in cookie['name']:
                        antiforgery_token = cookie['value']
                        break

                logger.info(f"Got antiforgery token: {antiforgery_token[:20] if antiforgery_token else 'None'}...")

                # Build payload
                # Normalize mevzuat type for API
                mevzuat_tur_api = self._normalize_mevzuat_tur_for_api(request.mevzuat_tur)

                payload = {
                    "draw": 1,
                    "columns": [
                        {"data": None, "name": "", "searchable": True, "orderable": False, "search": {"value": "", "regex": False}},
                        {"data": None, "name": "", "searchable": True, "orderable": False, "search": {"value": "", "regex": False}},
                        {"data": None, "name": "", "searchable": True, "orderable": False, "search": {"value": "", "regex": False}}
                    ],
                    "order": [],
                    "start": (request.page_number - 1) * request.page_size,
                    "length": request.page_size,
                    "search": {"value": "", "regex": False},
                    "parameters": {
                        "MevzuatTur": mevzuat_tur_api,
                        "YonetmelikMevzuatTur": "OsmanliKanunu",  # Required for all searches
                        "AranacakIfade": request.aranacak_ifade or "",
                        "TamCumle": "true" if request.tam_cumle else "false",
                        "AranacakYer": str(request.aranacak_yer),
                        "MevzuatNo": request.mevzuat_no or "",
                        "KurumId": "0",
                        "AltKurumId": "0",
                        "BaslangicTarihi": request.baslangic_tarihi or "",
                        "BitisTarihi": request.bitis_tarihi or "",
                        "antiforgerytoken": antiforgery_token or ""
                    }
                }

                # Make fetch request from within page context (has cookies)
                result = await page.evaluate("""
                    async (payload) => {
                        const response = await fetch('https://www.mevzuat.gov.tr/Anasayfa/MevzuatDatatable', {
                            method: 'POST',
                            headers: {
                                'Content-Type': 'application/json',
                                'X-Requested-With': 'XMLHttpRequest'
                            },
                            body: JSON.stringify(payload)
                        });
                        const text = await response.text();
                        if (!response.ok) {
                            return { error: true, status: response.status, text: text };
                        }
                        try {
                            return JSON.parse(text);
                        } catch(e) {
                            return { error: true, parseError: e.message, text: text.substring(0, 200) };
                        }
                    }
                """, payload)

                await browser.close()

            # Check for errors
            if result.get("error"):
                logger.error(f"Search API error: {result}")
                return MevzuatSearchResultNew(
                    documents=[],
                    total_results=0,
                    current_page=request.page_number,
                    page_size=request.page_size,
                    total_pages=0,
                    query_used=request.model_dump(),
                    error_message=f"API error: {result.get('text', 'Unknown error')[:100]}"
                )

            # Parse response
            total_results = result.get("recordsTotal", 0)
            documents = []

            for item in result.get("data", []):
                doc = MevzuatDocumentNew(
                    mevzuat_no=item.get("mevzuatNo", ""),
                    mev_adi=item.get("mevAdi", ""),
                    kabul_tarih=item.get("kabulTarih", ""),
                    resmi_gazete_tarihi=item.get("resmiGazeteTarihi", ""),
                    resmi_gazete_sayisi=item.get("resmiGazeteSayisi", ""),
                    mevzuat_tertip=item.get("mevzuatTertip", ""),
                    mevzuat_tur=item.get("tur", 1),
                    url=item.get("url", "")
                )
                documents.append(doc)

            logger.info(f"Found {total_results} results via Playwright")

            return MevzuatSearchResultNew(
                documents=documents,
                total_results=total_results,
                current_page=request.page_number,
                page_size=request.page_size,
                total_pages=(total_results + request.page_size - 1) // request.page_size if request.page_size > 0 else 0,
                query_used=request.model_dump()
            )

        except Exception as e:
            logger.error(f"Playwright search failed: {e}")
            return MevzuatSearchResultNew(
                documents=[],
                total_results=0,
                current_page=request.page_number,
                page_size=request.page_size,
                total_pages=0,
                query_used=request.model_dump(),
                error_message=f"Playwright search error: {str(e)}"
            )

    async def search_documents(self, request: MevzuatSearchRequestNew) -> MevzuatSearchResultNew:
        """Search for legislation documents using httpx with Playwright cookies."""
        # Get session/cookies with Playwright first
        await self._ensure_session()

        # If session establishment failed, fallback to full Playwright method
        if not self._antiforgery_token and not self._cookies:
            logger.warning("Session establishment failed, using full Playwright search method as fallback")
            return await self.search_documents_with_playwright(request)

        # Build DataTables compatible payload
        # Normalize mevzuat type for API
        mevzuat_tur_api = self._normalize_mevzuat_tur_for_api(request.mevzuat_tur)

        payload = {
            "draw": 1,
            "columns": [
                {"data": None, "name": "", "searchable": True, "orderable": False, "search": {"value": "", "regex": False}},
                {"data": None, "name": "", "searchable": True, "orderable": False, "search": {"value": "", "regex": False}},
                {"data": None, "name": "", "searchable": True, "orderable": False, "search": {"value": "", "regex": False}}
            ],
            "order": [],
            "start": (request.page_number - 1) * request.page_size,
            "length": request.page_size,
            "search": {"value": "", "regex": False},
            "parameters": {
                "MevzuatTur": mevzuat_tur_api,
                "YonetmelikMevzuatTur": "OsmanliKanunu",  # Required for all searches
                "AranacakIfade": request.aranacak_ifade or "",
                "TamCumle": "true" if request.tam_cumle else "false",
                "AranacakYer": str(request.aranacak_yer),
                "MevzuatNo": request.mevzuat_no or "",
                "KurumId": "0",
                "AltKurumId": "0",
                "BaslangicTarihi": request.baslangic_tarihi or "",
                "BitisTarihi": request.bitis_tarihi or "",
                "antiforgerytoken": self._antiforgery_token or ""
            }
        }

        try:
            # Log payload for debugging
            logger.debug(f"Search payload: {payload}")

            # Make request with cookies properly
            response = await self._http_client.post(
                self.SEARCH_ENDPOINT,
                json=payload,
                cookies=self._cookies if self._cookies else None
            )

            # Log response for debugging
            if response.status_code != 200:
                logger.error(f"Search API error {response.status_code}: {response.text[:500]}")

            response.raise_for_status()
            data = response.json()

            total_results = data.get("recordsTotal", 0)
            documents = []

            for item in data.get("data", []):
                doc = MevzuatDocumentNew(
                    mevzuat_no=item.get("mevzuatNo", ""),
                    mev_adi=item.get("mevAdi", ""),
                    kabul_tarih=item.get("kabulTarih", ""),
                    resmi_gazete_tarihi=item.get("resmiGazeteTarihi", ""),
                    resmi_gazete_sayisi=item.get("resmiGazeteSayisi", ""),
                    mevzuat_tertip=item.get("mevzuatTertip", ""),
                    mevzuat_tur=item.get("tur", 1),
                    url=item.get("url", "")
                )
                documents.append(doc)

            return MevzuatSearchResultNew(
                documents=documents,
                total_results=total_results,
                current_page=request.page_number,
                page_size=request.page_size,
                total_pages=(total_results + request.page_size - 1) // request.page_size if request.page_size > 0 else 0,
                query_used=request.model_dump()
            )

        except httpx.HTTPStatusError as e:
            logger.error(f"Search request failed: {e.response.status_code}")
            return MevzuatSearchResultNew(
                documents=[],
                total_results=0,
                current_page=request.page_number,
                page_size=request.page_size,
                total_pages=0,
                query_used=request.model_dump(),
                error_message=f"API request failed: {e.response.status_code}"
            )
        except Exception as e:
            logger.exception("Unexpected error during search")
            return MevzuatSearchResultNew(
                documents=[],
                total_results=0,
                current_page=request.page_number,
                page_size=request.page_size,
                total_pages=0,
                query_used=request.model_dump(),
                error_message=f"An unexpected error occurred: {e}"
            )

    async def get_content(
        self,
        mevzuat_no: str,
        mevzuat_tur: int = 1,
        mevzuat_tertip: str = "3",
        resmi_gazete_tarihi: Optional[str] = None
    ) -> MevzuatArticleContent:
        """
        Download and extract content from legislation.
        Tries HTML scraping first (most reliable), then falls back to file downloads.
        For Presidential Decisions (tur=20) and Circulars (tur=22), skip HTML and go directly to PDF.

        Args:
            mevzuat_no: Legislation number
            mevzuat_tur: Legislation type code (1=Kanun, 20=CB Kararı, 22=CB Genelgesi, etc.)
            mevzuat_tertip: Series number
            resmi_gazete_tarihi: Official Gazette date (DD/MM/YYYY) - required for CB Genelgesi (tur=22)
        """
        # CB Kararları (tur=20) and CB Genelgesi (tur=22) are PDF-only, skip HTML scraping
        if mevzuat_tur not in [20, 22]:
            # Try HTML scraping first (most reliable method for other types)
            result = await self.get_content_from_html(mevzuat_no, mevzuat_tur, mevzuat_tertip)
            if result.markdown_content:
                return result

            logger.info(f"HTML scraping returned no content for {mevzuat_no}, trying file downloads")
        else:
            if mevzuat_tur == 20:
                logger.info("CB Kararı detected (tur=20), skipping HTML scraping, going directly to PDF")
            elif mevzuat_tur == 22:
                logger.info("CB Genelgesi detected (tur=22), skipping HTML scraping, going directly to PDF")

        cache_key = f"doc:{mevzuat_tur}.{mevzuat_tertip}.{mevzuat_no}" if self._cache_enabled else None

        if cache_key and self._cache:
            cached_content = self._cache.get(cache_key)
            if cached_content:
                logger.debug(f"Cache hit: {mevzuat_no}")
                return MevzuatArticleContent(
                    madde_id=mevzuat_no,
                    mevzuat_id=mevzuat_no,
                    markdown_content=cached_content
                )

        # Construct URLs based on mevzuat type
        if mevzuat_tur == 22:  # CB Genelgesi - special PDF URL format
            if not resmi_gazete_tarihi:
                return MevzuatArticleContent(
                    madde_id=mevzuat_no,
                    mevzuat_id=mevzuat_no,
                    markdown_content="",
                    error_message="resmi_gazete_tarihi is required for CB Genelgesi (tur=22)"
                )
            # Convert DD/MM/YYYY to YYYYMMDD
            parts = resmi_gazete_tarihi.split('/')
            if len(parts) == 3:
                date_str = f"{parts[2]}{parts[1].zfill(2)}{parts[0].zfill(2)}"
            else:
                return MevzuatArticleContent(
                    madde_id=mevzuat_no,
                    mevzuat_id=mevzuat_no,
                    markdown_content="",
                    error_message=f"Invalid date format: {resmi_gazete_tarihi}. Expected DD/MM/YYYY"
                )
            pdf_url = self.GENELGE_PDF_URL_TEMPLATE.format(date=date_str, no=mevzuat_no)
            doc_url = None  # Genelge has no DOC version
        elif mevzuat_tur == 20:  # CB Kararı - PDF only, no DOC
            doc_url = None  # CB Kararları have no DOC version
            pdf_url = self.PDF_URL_TEMPLATE.format(tur=mevzuat_tur, tertip=mevzuat_tertip, no=mevzuat_no)
        else:
            doc_url = self.DOC_URL_TEMPLATE.format(tur=mevzuat_tur, tertip=mevzuat_tertip, no=mevzuat_no)
            pdf_url = self.PDF_URL_TEMPLATE.format(tur=mevzuat_tur, tertip=mevzuat_tertip, no=mevzuat_no)

        # Try DOC first (skip for CB Genelgesi and CB Kararı which have no DOC version)
        if doc_url:
            try:
                logger.info(f"Trying DOC: {doc_url}")
                # Use separate headers for document download
                doc_headers = {
                    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36',
                    'Accept': 'application/msword, */*',
                }
                response = await self._http_client.get(doc_url, headers=doc_headers)
                response.raise_for_status()

                doc_bytes = response.content
                logger.info(f"Downloaded DOC: {len(doc_bytes)} bytes")

                # DOC files from mevzuat.gov.tr are actually HTML
                if len(doc_bytes) < 100:
                    logger.warning(f"DOC file too small ({len(doc_bytes)} bytes), likely empty")
                    raise Exception("DOC file is empty or too small")

                doc_stream = io.BytesIO(doc_bytes)
                result = self._md_converter.convert_stream(doc_stream, file_extension=".doc")
                markdown_content = result.text_content.strip() if result and result.text_content else ""

                if markdown_content:
                    logger.info(f"DOC conversion successful for {mevzuat_no}")
                    if cache_key and self._cache:
                        self._cache.put(cache_key, markdown_content)
                    return MevzuatArticleContent(
                        madde_id=mevzuat_no,
                        mevzuat_id=mevzuat_no,
                        markdown_content=markdown_content
                    )
            except Exception as e:
                logger.info(f"DOC failed, trying PDF: {e}")

        # Try PDF fallback
        try:
            logger.info(f"Trying PDF: {pdf_url}")

            # For CB Kararı (tur=20) and CB Genelgesi (tur=22), ensure we have session cookies
            if mevzuat_tur in [20, 22]:
                await self._ensure_session()
                # Create temporary client with cookies to avoid deprecation warning
                async with httpx.AsyncClient(
                    headers=self.HEADERS,
                    cookies=self._cookies,
                    timeout=self._http_client.timeout,
                    follow_redirects=True
                ) as temp_client:
                    response = await temp_client.get(pdf_url)
            else:
                response = await self._http_client.get(pdf_url)

            response.raise_for_status()

            pdf_bytes = response.content
            markdown_content = ""

            # For CB Kararı (tur=20) and CB Genelgesi (tur=22), use Mistral OCR (handles images + text)
            if mevzuat_tur in [20, 22] and self._mistral_client:
                doc_type = "CB Kararı" if mevzuat_tur == 20 else "CB Genelgesi"
                logger.info(f"Using Mistral OCR for {doc_type} PDF")
                markdown_content = await self._ocr_pdf_with_mistral(pdf_bytes, pdf_url)

                # Fallback to markitdown if OCR fails
                if not markdown_content:
                    logger.warning("Mistral OCR failed, falling back to markitdown")
                    pdf_stream = io.BytesIO(pdf_bytes)
                    result = self._md_converter.convert_stream(pdf_stream, file_extension=".pdf")
                    markdown_content = result.text_content.strip() if result and result.text_content else ""
            else:
                # Use markitdown for other types
                pdf_stream = io.BytesIO(pdf_bytes)
                result = self._md_converter.convert_stream(pdf_stream, file_extension=".pdf")
                markdown_content = result.text_content.strip() if result and result.text_content else ""

            if markdown_content:
                logger.info(f"PDF conversion successful for {mevzuat_no}")
                if cache_key and self._cache:
                    self._cache.put(cache_key, markdown_content)
                return MevzuatArticleContent(
                    madde_id=mevzuat_no,
                    mevzuat_id=mevzuat_no,
                    markdown_content=markdown_content
                )
        except Exception as e:
            logger.error(f"PDF also failed: {e}")

        return MevzuatArticleContent(
            madde_id=mevzuat_no,
            mevzuat_id=mevzuat_no,
            markdown_content="",
            error_message=f"Both DOC and PDF download/conversion failed for {mevzuat_tur}.{mevzuat_tertip}.{mevzuat_no}"
        )

    async def get_content_from_html(
        self,
        mevzuat_no: str,
        mevzuat_tur: int = 1,
        mevzuat_tertip: str = "3"
    ) -> MevzuatArticleContent:
        """
        Scrape legislation content from HTML page using Playwright.
        """
        cache_key = f"html:{mevzuat_tur}.{mevzuat_tertip}.{mevzuat_no}" if self._cache_enabled else None

        if cache_key and self._cache:
            cached_content = self._cache.get(cache_key)
            if cached_content:
                logger.debug(f"Cache hit (HTML): {mevzuat_no}")
                return MevzuatArticleContent(
                    madde_id=mevzuat_no,
                    mevzuat_id=mevzuat_no,
                    markdown_content=cached_content
                )

        from playwright.async_api import async_playwright

        # Content is in an iframe
        iframe_url = f"{self.BASE_URL}/anasayfa/MevzuatFihristDetayIframe?MevzuatTur={mevzuat_tur}&MevzuatNo={mevzuat_no}&MevzuatTertip={mevzuat_tertip}"

        try:
            logger.info(f"Scraping iframe: {iframe_url}")

            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                page = await browser.new_page()

                await page.goto(iframe_url, wait_until="domcontentloaded", timeout=30000)

                # Wait for content
                await page.wait_for_selector('body', timeout=10000)

                # Get HTML
                html_content = await page.content()

                await browser.close()

            # Parse with BeautifulSoup
            soup = BeautifulSoup(html_content, 'lxml')

            # Remove unwanted tags
            for tag in soup(['script', 'style', 'nav', 'header', 'footer']):
                tag.decompose()

            # Get main content
            content_div = soup.find('div', class_='mevzuat') or soup.find('body')

            if content_div:
                # Convert to markdown
                markdown_content = self._markdown_from_html(str(content_div), cache_key=f"html_parse:{hash(str(content_div))}")

                if markdown_content:
                    logger.info(f"HTML scraping successful for {mevzuat_no}: {len(markdown_content)} chars")
                    if cache_key and self._cache:
                        self._cache.put(cache_key, markdown_content)
                    return MevzuatArticleContent(
                        madde_id=mevzuat_no,
                        mevzuat_id=mevzuat_no,
                        markdown_content=markdown_content
                    )

            return MevzuatArticleContent(
                madde_id=mevzuat_no,
                mevzuat_id=mevzuat_no,
                markdown_content="",
                error_message="Could not extract content from HTML page"
            )

        except Exception as e:
            logger.error(f"HTML scraping failed: {e}")
            return MevzuatArticleContent(
                madde_id=mevzuat_no,
                mevzuat_id=mevzuat_no,
                markdown_content="",
                error_message=f"HTML scraping error: {str(e)}"
            )
