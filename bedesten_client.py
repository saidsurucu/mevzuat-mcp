"""
API client for bedesten.adalet.gov.tr/mevzuat REST API.
Pure httpx - no authentication or Playwright needed.

All endpoints use the wrapper format:
  {"data": {...}, "applicationName": "UyapMevzuat"}

Responses use:
  {"data": ..., "metadata": {"FMTY": "SUCCESS"|"ERROR", ...}}
"""
import base64
from datetime import datetime, timedelta
import html
import logging
import re
import time
from typing import Dict, List, Optional, Any

import httpx

from bedesten_models import (
    MevzuatTurEnum,
    BedMevzuatDocument,
    BedSearchResult,
    BedMaddeNode,
    BedDocumentContent,
    BedGerekceContent,
)

logger = logging.getLogger(__name__)

BASE_URL = "https://bedesten.adalet.gov.tr/mevzuat"
HEADERS = {
    "Content-Type": "application/json; charset=utf-8",
    "AdaletApplicationName": "UyapMevzuat",
    "Origin": "https://mevzuat.adalet.gov.tr",
    "Referer": "https://mevzuat.adalet.gov.tr/",
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36",
}

APP_NAME = "UyapMevzuat"


def _wrap(data: dict) -> dict:
    """Wrap payload in the required format."""
    return {"data": data, "applicationName": APP_NAME}


def _wrap_paging(data: dict) -> dict:
    """Wrap payload with paging flag."""
    return {"data": data, "applicationName": APP_NAME, "paging": True}


class _Cache:
    """Simple in-memory cache with TTL."""

    def __init__(self, ttl: int = 3600):
        self.ttl = ttl
        self._store: Dict[str, tuple[float, Any]] = {}

    def get(self, key: str) -> Optional[Any]:
        if key in self._store:
            ts, val = self._store[key]
            if time.time() - ts < self.ttl:
                return val
            del self._store[key]
        return None

    def put(self, key: str, value: Any):
        self._store[key] = (time.time(), value)


def _strip_html(html_text: str) -> str:
    """Remove HTML tags and decode entities, returning plain text."""
    text = re.sub(r'<br\s*/?>', '\n', html_text, flags=re.IGNORECASE)
    text = re.sub(r'<[^>]+>', '', text)
    text = html.unescape(text)
    lines = text.split('\n')
    lines = [line.strip() for line in lines]
    return '\n'.join(line for line in lines if line)


def _decode_base64(raw: str) -> str:
    """Decode base64 content to UTF-8 string."""
    try:
        return base64.b64decode(raw).decode("utf-8", errors="replace")
    except Exception:
        return raw


class BedestenClient:
    """Client for bedesten.adalet.gov.tr/mevzuat API."""

    def __init__(self, cache_ttl: int = 3600, enable_cache: bool = True):
        self._cache = _Cache(ttl=cache_ttl) if enable_cache else None
        self._client = httpx.AsyncClient(
            base_url=BASE_URL,
            headers=HEADERS,
            timeout=30.0,
        )

    async def close(self):
        await self._client.aclose()

    def _get_cached(self, key: str) -> Optional[Any]:
        return self._cache.get(key) if self._cache else None

    def _put_cached(self, key: str, value: Any):
        if self._cache:
            self._cache.put(key, value)

    # ------------------------------------------------------------------
    # 1. Search / list documents
    # ------------------------------------------------------------------
    @staticmethod
    def _to_iso8601_start(date_str: str) -> str:
        """Convert DD/MM/YYYY to ISO 8601 UTC for range start.

        Midnight of the given date in Turkey (UTC+3) = previous day 21:00 UTC.
        E.g. 18/03/2026 → 2026-03-17T21:00:00.000Z
        """
        dt = datetime.strptime(date_str.strip(), "%d/%m/%Y")
        prev = dt - timedelta(days=1)
        return prev.strftime("%Y-%m-%dT21:00:00.000Z")

    @staticmethod
    def _to_iso8601_end(date_str: str) -> str:
        """Convert DD/MM/YYYY to ISO 8601 UTC for range end.

        Midnight of the day AFTER the given date in Turkey (UTC+3) = given day 21:00 UTC.
        This ensures the entire end date is included in the range.
        E.g. 18/03/2026 → 2026-03-18T21:00:00.000Z
        """
        dt = datetime.strptime(date_str.strip(), "%d/%m/%Y")
        return dt.strftime("%Y-%m-%dT21:00:00.000Z")

    async def search_documents(
        self,
        phrase: str = "",
        mevzuat_adi: str = "",
        mevzuat_no: Optional[str] = None,
        mevzuat_tur_list: Optional[List[str]] = None,
        basliktaAra: bool = True,
        tamCumle: bool = False,
        resmi_gazete_tarihi_start: Optional[str] = None,
        resmi_gazete_tarihi_end: Optional[str] = None,
        resmi_gazete_sayisi: Optional[str] = None,
        page: int = 1,
        page_size: int = 25,
        sort_field: str = "RESMI_GAZETE_TARIHI",
        sort_direction: str = "desc",
    ) -> BedSearchResult:
        """
        Search or list legislation documents.

        Args:
            phrase: Full-text search (Solr syntax). Searches in document content.
            mevzuat_adi: Title/keyword search. Searches in legislation name.
            mevzuat_no: Legislation number filter.
            mevzuat_tur_list: Filter by types, e.g. ["KANUN", "KHK"]
            basliktaAra: Search in title only (default True).
            tamCumle: Exact phrase match (default False).
            resmi_gazete_tarihi_start: Start date filter (DD/MM/YYYY). Converted to ISO 8601 for API.
            resmi_gazete_tarihi_end: End date filter (DD/MM/YYYY). Converted to ISO 8601 for API.
            resmi_gazete_sayisi: Official Gazette number filter.
            page: Page number (1-based)
            page_size: Results per page
            sort_field: RESMI_GAZETE_TARIHI, MEVZUAT_ADI, MEVZUAT_NO, etc.
            sort_direction: asc or desc
        """
        inner: Dict[str, Any] = {
            "pageSize": page_size,
            "pageNumber": page,
            "sortFields": [sort_field],
            "sortDirection": sort_direction,
        }
        if phrase:
            inner["phrase"] = phrase
        if mevzuat_adi:
            inner["mevzuatAdi"] = mevzuat_adi
        if mevzuat_no:
            inner["mevzuatNo"] = mevzuat_no
        if mevzuat_tur_list:
            inner["mevzuatTurList"] = mevzuat_tur_list
        if not basliktaAra:
            inner["basliktaAra"] = False
        if tamCumle:
            inner["tamCumle"] = True
        if resmi_gazete_tarihi_start:
            inner["resmiGazeteTarihiStart"] = self._to_iso8601_start(resmi_gazete_tarihi_start)
        if resmi_gazete_tarihi_end:
            inner["resmiGazeteTarihiEnd"] = self._to_iso8601_end(resmi_gazete_tarihi_end)
        if resmi_gazete_sayisi:
            inner["resmiGazeteSayisi"] = resmi_gazete_sayisi

        try:
            resp = await self._client.post("/searchDocuments", json=_wrap_paging(inner))
            resp.raise_for_status()
            body = resp.json()

            meta = body.get("metadata", {})
            if meta.get("FMTY") != "SUCCESS":
                return BedSearchResult(
                    error_message=meta.get("FMTE", "Unknown error"),
                    query_used=phrase,
                )

            data = body.get("data") or {}
            documents = []
            for doc in data.get("mevzuatList", []):
                documents.append(BedMevzuatDocument.model_validate(doc))

            return BedSearchResult(
                documents=documents,
                total_results=data.get("total", 0),
                start=data.get("start", 0),
                query_used=phrase,
            )
        except Exception as e:
            logger.exception("bedesten search error")
            return BedSearchResult(error_message=str(e), query_used=phrase)

    # ------------------------------------------------------------------
    # 2. Get full document content (base64 HTML/PDF)
    # ------------------------------------------------------------------
    async def get_document_content(
        self,
        mevzuat_id: str,
    ) -> BedDocumentContent:
        """Fetch full document content (decoded from base64)."""
        cache_key = f"doc_{mevzuat_id}"
        cached = self._get_cached(cache_key)
        if cached:
            return cached

        inner = {"documentType": "MEVZUAT", "id": mevzuat_id}
        try:
            resp = await self._client.post("/getDocumentContent", json=_wrap(inner))
            resp.raise_for_status()
            body = resp.json()

            meta = body.get("metadata", {})
            if meta.get("FMTY") != "SUCCESS":
                return BedDocumentContent(error_message=meta.get("FMTE", "Unknown error"))

            data = body.get("data") or {}
            raw = data.get("content", "")
            mime = data.get("mimeType", "text/html")
            decoded = _decode_base64(raw)

            result = BedDocumentContent(content=decoded, mime_type=mime)
            self._put_cached(cache_key, result)
            return result
        except Exception as e:
            logger.exception("bedesten get_document_content error")
            return BedDocumentContent(error_message=str(e))

    # ------------------------------------------------------------------
    # 3. Get single article content
    # ------------------------------------------------------------------
    async def get_article_content(
        self,
        madde_id: str,
    ) -> BedDocumentContent:
        """Fetch a single article's content by maddeId."""
        inner = {"documentType": "MADDE", "id": madde_id}
        try:
            resp = await self._client.post("/getDocumentContent", json=_wrap(inner))
            resp.raise_for_status()
            body = resp.json()

            meta = body.get("metadata", {})
            if meta.get("FMTY") != "SUCCESS":
                return BedDocumentContent(error_message=meta.get("FMTE", "Unknown error"))

            data = body.get("data") or {}
            decoded = _decode_base64(data.get("content", ""))
            return BedDocumentContent(
                content=decoded,
                mime_type=data.get("mimeType", "text/html"),
            )
        except Exception as e:
            logger.exception("bedesten get_article_content error")
            return BedDocumentContent(error_message=str(e))

    # ------------------------------------------------------------------
    # 4. Get article tree (table of contents)
    # ------------------------------------------------------------------
    async def get_article_tree(
        self,
        mevzuat_id: str,
    ) -> tuple[List[BedMaddeNode], Optional[str]]:
        """
        Get the article tree (madde ağacı / table of contents).
        Returns (nodes, error_message).
        """
        cache_key = f"tree_{mevzuat_id}"
        cached = self._get_cached(cache_key)
        if cached:
            return cached, None

        inner = {"mevzuatId": mevzuat_id}
        try:
            resp = await self._client.post("/mevzuatMaddeTree", json=_wrap(inner))
            resp.raise_for_status()
            body = resp.json()

            meta = body.get("metadata", {})
            if meta.get("FMTY") != "SUCCESS":
                return [], meta.get("FMTE", "Unknown error")

            data = body.get("data") or {}
            # Tree response is {"children": [...]} at top level
            children_list = data.get("children", []) if isinstance(data, dict) else data
            nodes = [BedMaddeNode.model_validate(n) for n in children_list]
            self._put_cached(cache_key, nodes)
            return nodes, None
        except Exception as e:
            logger.exception("bedesten get_article_tree error")
            return [], str(e)

    # ------------------------------------------------------------------
    # 5. Get gerekçe (law rationale)
    # ------------------------------------------------------------------
    async def get_gerekce_content(
        self,
        gerekce_id: str,
    ) -> BedGerekceContent:
        """Fetch law rationale content."""
        cache_key = f"gerekce_{gerekce_id}"
        cached = self._get_cached(cache_key)
        if cached:
            return cached

        inner = {"gerekceId": gerekce_id}
        try:
            resp = await self._client.post("/getGerekceContent", json=_wrap(inner))
            resp.raise_for_status()
            body = resp.json()

            meta = body.get("metadata", {})
            if meta.get("FMTY") != "SUCCESS":
                return BedGerekceContent(error_message=meta.get("FMTE", "Unknown error"))

            data = body.get("data") or {}
            raw = data.get("content", "")
            mime = data.get("mimetype", data.get("mimeType", "text/html"))
            decoded = _decode_base64(raw)

            result = BedGerekceContent(
                gerekce_id=data.get("gerekceId"),
                mevzuat_id=data.get("mevzuatId"),
                content=decoded,
                mime_type=mime,
            )
            self._put_cached(cache_key, result)
            return result
        except Exception as e:
            logger.exception("bedesten get_gerekce_content error")
            return BedGerekceContent(error_message=str(e))

    # ------------------------------------------------------------------
    # 6. Get mevzuat types
    # ------------------------------------------------------------------
    async def get_mevzuat_types(self) -> List[dict]:
        """Get all available legislation types with counts."""
        cache_key = "mevzuat_types"
        cached = self._get_cached(cache_key)
        if cached:
            return cached

        try:
            resp = await self._client.post("/mevzuatTypes", json=_wrap({}))
            resp.raise_for_status()
            body = resp.json()

            meta = body.get("metadata", {})
            if meta.get("FMTY") != "SUCCESS":
                return []

            types = body.get("data") or []
            self._put_cached(cache_key, types)
            return types
        except Exception as e:
            logger.exception("bedesten get_mevzuat_types error")
            return []

    # ------------------------------------------------------------------
    # 7. Get full document as plain text (for search_within)
    # ------------------------------------------------------------------
    async def get_document_plain_text(
        self,
        mevzuat_id: str,
    ) -> str:
        """Fetch full document, decode base64 HTML, strip tags to plain text."""
        cache_key = f"plain_{mevzuat_id}"
        cached = self._get_cached(cache_key)
        if cached:
            return cached

        doc = await self.get_document_content(mevzuat_id)
        if doc.error_message:
            return ""
        if not doc.content:
            return ""

        plain = _strip_html(doc.content)
        self._put_cached(cache_key, plain)
        return plain
