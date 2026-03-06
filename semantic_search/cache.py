# semantic_search/cache.py

import logging
import time
import hashlib
from typing import Dict, Optional, Tuple, Any
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class CacheEntry:
    """Cached embedding data for a legislation."""
    vector_store: Any  # VectorStore instance
    chunks: list  # List of DocumentChunk
    content_hash: str
    created_at: float
    expires_at: float


class EmbeddingCache:
    """Cache for pre-built VectorStore instances with TTL and content hash validation."""

    def __init__(self, ttl: int = 3600):
        self._cache: Dict[str, CacheEntry] = {}
        self._ttl = ttl

    def _make_key(self, mevzuat_tur: int, mevzuat_tertip: str, mevzuat_no: str) -> str:
        return f"emb:{mevzuat_tur}.{mevzuat_tertip}.{mevzuat_no}"

    def _content_hash(self, content: str) -> str:
        return hashlib.md5(content.encode('utf-8')).hexdigest()

    def get(self, mevzuat_tur: int, mevzuat_tertip: str, mevzuat_no: str,
            content: str) -> Optional[Tuple[Any, list]]:
        """
        Get cached VectorStore and chunks if valid.

        Returns (vector_store, chunks) or None if cache miss/expired/content changed.
        """
        key = self._make_key(mevzuat_tur, mevzuat_tertip, mevzuat_no)

        if key not in self._cache:
            return None

        entry = self._cache[key]

        # Check TTL
        if time.time() > entry.expires_at:
            del self._cache[key]
            logger.info(f"Cache expired for {key}")
            return None

        # Check content hash
        current_hash = self._content_hash(content)
        if current_hash != entry.content_hash:
            del self._cache[key]
            logger.info(f"Content changed for {key}, invalidating cache")
            return None

        logger.info(f"Cache hit for {key}")
        return entry.vector_store, entry.chunks

    def put(self, mevzuat_tur: int, mevzuat_tertip: str, mevzuat_no: str,
            content: str, vector_store: Any, chunks: list) -> None:
        """Store VectorStore and chunks in cache."""
        key = self._make_key(mevzuat_tur, mevzuat_tertip, mevzuat_no)
        now = time.time()

        self._cache[key] = CacheEntry(
            vector_store=vector_store,
            chunks=chunks,
            content_hash=self._content_hash(content),
            created_at=now,
            expires_at=now + self._ttl,
        )
        logger.info(f"Cached embeddings for {key} ({len(chunks)} chunks)")

    def clear(self) -> None:
        self._cache.clear()

    def size(self) -> int:
        return len(self._cache)
