# semantic_search/embedder.py

import logging
import os
from typing import List, Optional
import numpy as np

logger = logging.getLogger(__name__)

# Supported models and their dimensions
EMBEDDING_MODELS = {
    "google/gemini-embedding-001": 3072,
    "intfloat/multilingual-e5-large": 1024,
}
DEFAULT_MODEL = "google/gemini-embedding-001"


def is_openrouter_available() -> bool:
    """Check if OpenRouter API key is available."""
    return bool(os.getenv("OPENROUTER_API_KEY"))


def get_embedding_model() -> str:
    """Get the embedding model from env var or default."""
    model = os.getenv("EMBEDDING_MODEL", DEFAULT_MODEL)
    if model not in EMBEDDING_MODELS:
        logger.warning(f"Unknown EMBEDDING_MODEL '{model}', falling back to {DEFAULT_MODEL}")
        return DEFAULT_MODEL
    return model


class OpenRouterEmbedder:
    """
    Embedder using OpenRouter API.
    Supports multiple models via EMBEDDING_MODEL env var:
    - google/gemini-embedding-001 (default, 3072 dim)
    - intfloat/multilingual-e5-large (1024 dim)

    Requires OPENROUTER_API_KEY environment variable.
    """

    def __init__(self):
        api_key = os.getenv("OPENROUTER_API_KEY")
        if not api_key:
            raise ValueError("OPENROUTER_API_KEY environment variable is not set")

        try:
            from openai import OpenAI
        except ImportError:
            raise ImportError("openai package is required. Install with: pip install openai")

        self.client = OpenAI(
            base_url="https://openrouter.ai/api/v1",
            api_key=api_key,
        )
        self.model = get_embedding_model()
        self.dimension = EMBEDDING_MODELS[self.model]
        self._is_e5 = "e5" in self.model

        logger.info(f"OpenRouter Embedder initialized: model={self.model}, dim={self.dimension}")

    def _format_query(self, query: str) -> str:
        """Format query text based on model requirements."""
        if self._is_e5:
            return f"query: {query}"
        return f"task: search result | query: {query}"

    def _format_document(self, text: str, title: str) -> str:
        """Format document text based on model requirements."""
        if self._is_e5:
            return f"passage: {title} {text}" if title and title != "none" else f"passage: {text}"
        return f"title: {title} | text: {text}"

    def encode_query(self, query: str) -> np.ndarray:
        """Encode a search query into an embedding vector."""
        text = self._format_query(query)

        try:
            response = self.client.embeddings.create(
                model=self.model,
                input=text,
                encoding_format="float",
                extra_headers={
                    "HTTP-Referer": "https://mevzuatmcp.com",
                    "X-Title": "Mevzuat MCP Server",
                }
            )

            embedding = np.array(response.data[0].embedding, dtype=np.float32)

            # L2 normalize for cosine similarity
            norm = np.linalg.norm(embedding)
            if norm > 0:
                embedding = embedding / norm

            logger.debug(f"Encoded query: {query[:50]}... -> shape: {embedding.shape}")
            return embedding

        except Exception as e:
            logger.error(f"Failed to encode query: {e}")
            raise

    def encode_documents(self, documents: List[str], titles: Optional[List[str]] = None,
                         batch_size: int = 50) -> np.ndarray:
        """Encode multiple documents, batching to avoid API limits."""
        if not documents:
            return np.array([])

        texts = []
        for i, doc in enumerate(documents):
            title = titles[i] if titles and i < len(titles) else "none"
            texts.append(self._format_document(doc, title))

        try:
            all_embeddings = []

            for start in range(0, len(texts), batch_size):
                batch = texts[start:start + batch_size]
                logger.info(f"Encoding batch {start // batch_size + 1}/{(len(texts) - 1) // batch_size + 1} ({len(batch)} docs)")

                response = self.client.embeddings.create(
                    model=self.model,
                    input=batch,
                    encoding_format="float",
                    extra_headers={
                        "HTTP-Referer": "https://mevzuatmcp.com",
                        "X-Title": "Mevzuat MCP Server",
                    }
                )

                batch_embeddings = np.array(
                    [d.embedding for d in sorted(response.data, key=lambda x: x.index)],
                    dtype=np.float32
                )
                all_embeddings.append(batch_embeddings)

            embeddings = np.vstack(all_embeddings) if len(all_embeddings) > 1 else all_embeddings[0]

            # L2 normalize each embedding for cosine similarity
            norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
            embeddings = embeddings / (norms + 1e-8)

            logger.info(f"Encoded {len(documents)} documents -> shape: {embeddings.shape}")
            return embeddings

        except Exception as e:
            logger.error(f"Failed to encode documents: {e}")
            raise
