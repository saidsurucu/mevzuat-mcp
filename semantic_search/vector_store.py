# semantic_search/vector_store.py

import logging
import numpy as np
from typing import List, Dict, Any, Tuple, Optional
from dataclasses import dataclass

logger = logging.getLogger(__name__)

@dataclass
class Document:
    """Represents a document with its embedding and metadata."""
    id: str
    text: str
    embedding: np.ndarray
    metadata: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return {
            'id': self.id,
            'text': self.text,
            'metadata': self.metadata
        }

class VectorStore:
    """In-memory vector storage with similarity search."""

    def __init__(self, dimension: int = 768):
        self.dimension = dimension
        self.documents: List[Document] = []
        self.embeddings: Optional[np.ndarray] = None
        self.index_built = False

    def add_documents(self,
                     ids: List[str],
                     texts: List[str],
                     embeddings: np.ndarray,
                     metadata: Optional[List[Dict[str, Any]]] = None) -> int:
        if len(ids) != len(texts) or len(ids) != embeddings.shape[0]:
            raise ValueError("Mismatched lengths for ids, texts, and embeddings")

        if metadata and len(metadata) != len(ids):
            raise ValueError("Metadata length doesn't match document count")

        for i in range(len(ids)):
            doc = Document(
                id=ids[i],
                text=texts[i],
                embedding=embeddings[i],
                metadata=metadata[i] if metadata else {}
            )
            self.documents.append(doc)

        self._build_index()
        logger.info(f"Added {len(ids)} documents to vector store. Total: {len(self.documents)}")
        return len(ids)

    def _build_index(self):
        if not self.documents:
            self.embeddings = None
            self.index_built = False
            return

        self.embeddings = np.vstack([doc.embedding for doc in self.documents])
        self.index_built = True

    def search(self,
              query_embedding: np.ndarray,
              top_k: int = 10,
              threshold: Optional[float] = None) -> List[Tuple[Document, float]]:
        if not self.index_built or self.embeddings is None:
            return []

        if len(query_embedding.shape) == 1:
            query_embedding = query_embedding.reshape(1, -1)

        similarities = np.dot(self.embeddings, query_embedding.T).squeeze()

        if threshold is not None:
            valid_indices = np.where(similarities >= threshold)[0]
            if len(valid_indices) == 0:
                return []
            similarities = similarities[valid_indices]
            valid_docs = [self.documents[i] for i in valid_indices]
        else:
            valid_docs = self.documents

        top_k = min(top_k, len(valid_docs))
        if top_k == 0:
            return []

        if len(similarities) > top_k:
            top_indices = np.argpartition(similarities, -top_k)[-top_k:]
            top_indices = top_indices[np.argsort(similarities[top_indices])[::-1]]
        else:
            top_indices = np.argsort(similarities)[::-1]

        results = []
        for idx in top_indices:
            doc = valid_docs[idx] if threshold else self.documents[idx]
            score = float(similarities[idx])
            results.append((doc, score))

        return results

    def clear(self):
        self.documents = []
        self.embeddings = None
        self.index_built = False

    def size(self) -> int:
        return len(self.documents)
